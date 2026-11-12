#!/usr/bin/env python3
"""Evaluator-owned Signalpost batch contract.

NOTE on financial_history: it stays OUTSIDE the default --modules string on
purpose. Its endpoint throttles globally to one request start per 2.1 seconds,
so 1,000 companies carry a ~35-minute serial floor for that module alone,
which risks an official-run timeout. Enable it explicitly via --modules when
the run budget allows; the module itself is unchanged and its years/PDF links
then flow into claims, refresh tracking, and synthesis like any other module.

WAVE-3B external modules (default ON; opt out by naming --modules without
them):

- ``wikidata`` (AUDIT #17): SPARQL queries chunked at 200 organisation
  numbers per query, run serially before enrichment (never per-profile,
  so worker parallelism cannot multiply WDQS load). Each profile gets
  its own evidence record from its chunk's response; a WDQS failure
  degrades to per-company ``blocked``/``source_error`` records and never
  kills the run. Cheap (one request per 200 companies) and exact-keyed
  (P2333), which is why it is default-on.
- ``youtube_activity`` (AUDIT #19): runs per profile AFTER the website
  identity gate, and only when the gated website publishes a verified
  YouTube handle. Companies without a handle cost zero requests. Default-on
  because the no-handle path is free and the handle path is bounded (one
  channel-page fetch for @handles plus one RSS fetch, at most 3 handles).
- Website-candidate flow: when a profile has no registry website, or the
  registry website fetch fails (status != available), and ``wikidata`` is
  requested, each official website from the Wikidata record (max 3, in
  order) is fetched with the normal website fetcher and identity gate.
  Only a candidate that passes the gate replaces the website evidence,
  annotated with ``website_candidate_source="wikidata"`` plus the QID.
  A fetched-but-gate-failed registry website is deliberately NOT replaced:
  it is evidence about the registry URL itself, not a dead link. Failed
  candidate attempts are recorded on the Wikidata record copy
  (``website_candidate_attempt``); the original website record is kept.

Tradeoffs: the single Wikidata query adds one serial request (60s timeout,
one retry) before enrichment; candidate fetches add bounded website
fetches only for companies whose registry website is missing/dead. Both
modules ride the generic contract adapter (no adapter change) and the
existing checkpoint/resume mechanism: profiles resumed from a checkpoint
made before these modules existed are re-enriched because
``profile_complete_for_modules`` requires every requested module.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.batch import profile_complete_for_modules, profiles_from_bulk, read_organisation_inputs, terminal_envelope, validate_envelopes  # noqa: E402
from norway_company_agent.contract import contract_envelope, validate_contract_envelope  # noqa: E402
from norway_company_agent.evidence import evidence, utc_now  # noqa: E402
from norway_company_agent.identity import apply_website_identity_gate  # noqa: E402
from norway_company_agent.official import fetch_official_modules  # noqa: E402
from norway_company_agent.refresh import diff_profile  # noqa: E402
from norway_company_agent.site_extract import registry_contacts_from_profile  # noqa: E402
from norway_company_agent.website import fetch_website  # noqa: E402
from norway_company_agent.wikidata import WDQS_ENDPOINT, fetch_wikidata_batch, wikidata_website_candidates  # noqa: E402
from norway_company_agent.youtube_rss import fetch_youtube_activity  # noqa: E402


def read_previous_profiles(path: Path) -> dict[str, dict]:
    """Read a previous profiles JSONL snapshot (profile dicts, or internal envelopes)."""
    previous: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if isinstance(row.get("profile"), dict):
            row = row["profile"]
        org = row.get("organisation_number")
        if org:
            previous[str(org)] = row
    return previous


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluator-owned Signalpost batch contract")
    parser.add_argument("--organisations", required=True, help="JSON, JSONL, or text organisation-number list")
    parser.add_argument("--bulk", required=True, help="Frozen Brreg entity snapshot")
    parser.add_argument("--output", required=True, help="Terminal envelope JSONL")
    parser.add_argument("--profiles-output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expected-count", type=int, default=100)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--checkpoint-every", type=int, default=25)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--modules", default="registry,accounting_obligation,registry_live,financials,roles,group,locations,website,wikidata,youtube_activity")
    parser.add_argument("--previous-profiles", default=None, help="Previous profiles JSONL to diff against (refresh changes[] + report summary)")
    parser.add_argument("--contract-output", default=None, help="Write OUTPUT_CONTRACT.md envelopes (JSONL) alongside the internal envelopes")
    args = parser.parse_args()

    started_at = utc_now()
    organisation_inputs = read_organisation_inputs(args.organisations)
    orgs = [item["organisation_number"] for item in organisation_inputs]
    if len(orgs) != args.expected_count:
        raise SystemExit(f"Expected {args.expected_count} organisations, received {len(orgs)}")
    profiles, registry_metadata = profiles_from_bulk(args.bulk, orgs)
    annotations = {item["organisation_number"]: item for item in organisation_inputs}
    for profile in profiles:
        for key in ("evaluation_split", "sample_slice"):
            if key in annotations[profile["organisation_number"]]:
                profile[key] = annotations[profile["organisation_number"]][key]
    requested_modules = [item.strip() for item in args.modules.split(",") if item.strip()]
    fetch_modules = set(requested_modules) - {"registry", "accounting_obligation", "website", "wikidata", "youtube_activity"}
    operations = {"requests": 0, "bytes": 0, "latencies_ms": []}

    # WAVE-3B: one Wikidata query for the whole batch, run once, serially,
    # before enrichment. fetch_wikidata_batch never raises; the try/except
    # below is belt and braces so an unexpected failure still yields
    # per-company source_error records instead of killing the run.
    wikidata_records: dict[str, dict] = {}
    wikidata_metrics: dict = {"requests": 0, "bytes": 0, "latencies_ms": []}
    if "wikidata" in requested_modules:
        try:
            wikidata_records, wikidata_metrics = fetch_wikidata_batch(profiles)
        except Exception as exc:  # pragma: no cover - defensive, module never raises
            failed_at = utc_now()
            wikidata_records = {
                profile["organisation_number"]: evidence(
                    "wikidata", "source_error", "wikidata_sparql", WDQS_ENDPOINT,
                    note=f"Wikidata batch lookup failed unexpectedly ({type(exc).__name__}); no claims published.",
                    retrieved_at=failed_at, source_row_key=profile["organisation_number"],
                )
                for profile in profiles
            }

    def enrich(profile: dict) -> tuple[dict, dict]:
        org = profile["organisation_number"]
        if "wikidata" in requested_modules:
            record = wikidata_records.get(org)
            if record is None:
                record = evidence(
                    "wikidata", "source_error", "wikidata_sparql", WDQS_ENDPOINT,
                    note="No Wikidata record was produced for this company; no claims published.",
                    source_row_key=org,
                )
            # Deep copy: the candidate flow below annotates this profile's
            # record, and the batch dict is shared across worker threads.
            profile["evidence"]["wikidata"] = copy.deepcopy(record)
        records, metrics = fetch_official_modules(profile["organisation_number"], fetch_modules)
        profile["evidence"].update(records)
        website_metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
        if "website" in requested_modules:
            # Identity context (W3A #13/#14): enables hiring-organisation
            # verification for site jobs and the site-vs-registry contact
            # comparison. Without it both features stay quarantined.
            website_context = {
                "legal_name": str(profile.get("name") or "") or None,
                "organisation_number": org,
                "registry_contacts": registry_contacts_from_profile(profile),
            }
            website_record, website_metrics = fetch_website(profile.get("website"), **website_context)
            website_record = apply_website_identity_gate(profile, website_record)["website"]
            profile["evidence"]["website"] = website_record
            # WAVE-3B candidate flow: registry website missing or dead, and
            # Wikidata supplied an official-website candidate. Each candidate
            # must pass the same fetch + identity gate as a registry URL.
            if website_record.get("status") != "available" and "wikidata" in requested_modules:
                candidates = wikidata_website_candidates(profile["evidence"].get("wikidata"))
                attempted: list[dict] = []
                published = False
                for candidate_url in candidates[:3]:
                    candidate_record, candidate_metrics = fetch_website(candidate_url, **website_context)
                    website_metrics["requests"] += candidate_metrics["requests"]
                    website_metrics["bytes"] += candidate_metrics["bytes"]
                    website_metrics["latencies_ms"].extend(candidate_metrics["latencies_ms"])
                    gated = apply_website_identity_gate(profile, candidate_record)
                    candidate_record = gated["website"]
                    publishable = bool((gated.get("assessment") or {}).get("publishable"))
                    attempted.append({
                        "candidate_url": candidate_url,
                        "fetch_status": candidate_record.get("status"),
                        "publishable": publishable,
                    })
                    if candidate_record.get("status") == "available" and publishable:
                        wikidata_value = (profile["evidence"].get("wikidata") or {}).get("value") or {}
                        candidate_value = candidate_record.get("value") or {}
                        candidate_value["website_candidate_source"] = "wikidata"
                        candidate_value["wikidata_qid"] = wikidata_value.get("qid")
                        candidate_value["registry_website"] = profile.get("website") or None
                        candidate_record["value"] = candidate_value
                        candidate_record["note"] = (
                            f"{candidate_record.get('note') or ''}; website URL supplied by Wikidata "
                            f"candidate ({wikidata_value.get('qid')}), registry URL missing/unreachable, "
                            "identity gate passed"
                        ).strip("; ")
                        profile["evidence"]["website"] = candidate_record
                        published = True
                        break
                if attempted and not published:
                    wikidata_record = profile["evidence"].get("wikidata") or {}
                    wikidata_value = wikidata_record.get("value")
                    if isinstance(wikidata_value, dict):
                        wikidata_value["website_candidate_attempt"] = {
                            "candidate_urls": [item["candidate_url"] for item in attempted],
                            "outcomes": attempted,
                            "published": False,
                            "registry_website": profile.get("website") or None,
                        }
        youtube_metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
        if "youtube_activity" in requested_modules:
            youtube_record, youtube_metrics = fetch_youtube_activity(profile)
            profile["evidence"]["youtube_activity"] = youtube_record
        metric = {
            "requests": len(metrics) + website_metrics["requests"] + youtube_metrics["requests"],
            "bytes": sum(item.bytes_received for item in metrics) + website_metrics["bytes"] + youtube_metrics["bytes"],
            "latencies_ms": [item.elapsed_ms for item in metrics] + website_metrics["latencies_ms"] + youtube_metrics["latencies_ms"],
        }
        profile["run_metrics"] = metric
        return profile, metric

    state: dict[str, dict] = {}
    resumed_profiles = 0
    profiles_output = Path(args.profiles_output)
    if args.resume and profiles_output.exists():
        prior = [json.loads(line) for line in profiles_output.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not set(item["organisation_number"] for item in prior).issubset(set(orgs)):
            raise SystemExit("Resume profile membership is not a subset of this batch")
        state = {
            item["organisation_number"]: item
            for item in prior
            if profile_complete_for_modules(item, requested_modules)
        }
        resumed_profiles = len(state)
    pending_profiles = [profile for profile in profiles if profile["organisation_number"] not in state]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(enrich, profile): profile["organisation_number"] for profile in pending_profiles}
        for index, future in enumerate(as_completed(futures), 1):
            profile, metric = future.result()
            state[profile["organisation_number"]] = profile
            operations["requests"] += metric["requests"]
            operations["bytes"] += metric["bytes"]
            operations["latencies_ms"].extend(metric["latencies_ms"])
            if index % args.checkpoint_every == 0 or index == len(pending_profiles):
                checkpoint = [state[org] for org in orgs if org in state]
                write_jsonl(profiles_output, checkpoint)

    completed_at = utc_now()
    ordered_profiles = [state[org] for org in orgs]
    # The single batch Wikidata query is run-level, not per-profile: fold its
    # one request into the run operations so the report accounts for it.
    operations["requests"] += int(wikidata_metrics.get("requests") or 0)
    operations["bytes"] += int(wikidata_metrics.get("bytes") or 0)
    operations["latencies_ms"].extend(wikidata_metrics.get("latencies_ms") or [])
    envelopes = [
        terminal_envelope(profile, run_id=args.run_id, modules=requested_modules, started_at=started_at, completed_at=completed_at)
        for profile in ordered_profiles
    ]
    validation = validate_envelopes(envelopes, args.expected_count)
    write_jsonl(profiles_output, ordered_profiles)
    write_jsonl(Path(args.output), envelopes)

    # Integrated refresh: diff each profile against the previous snapshot when
    # one was supplied. A diff failure for one company must not kill the run;
    # it is recorded in the report and that company keeps a baseline synthesis.
    previous_profiles: dict[str, dict] = {}
    if args.previous_profiles:
        previous_profiles = read_previous_profiles(Path(args.previous_profiles))
    changes_by_org: dict[str, list[dict]] = {}
    refresh_errors: list[dict] = []
    field_counter: Counter = Counter()
    for profile in ordered_profiles:
        org = profile["organisation_number"]
        if org not in previous_profiles:
            continue
        try:
            changes = diff_profile(previous_profiles[org], profile)
        except ValueError as exc:
            refresh_errors.append({"organisation_number": org, "error": str(exc)})
            continue
        changes_by_org[org] = changes
        field_counter.update(change["field"] for change in changes)
    refresh_summary = {
        "previous_profiles": args.previous_profiles,
        "compared": len(changes_by_org),
        "not_in_previous": len(ordered_profiles) - len(changes_by_org) - len(refresh_errors) if args.previous_profiles else len(ordered_profiles),
        "changed_companies": sum(1 for changes in changes_by_org.values() if changes),
        "total_changes": sum(len(changes) for changes in changes_by_org.values()),
        "changes_by_field": dict(sorted(field_counter.items())),
        "errors": refresh_errors,
    }

    contract_summary = None
    if args.contract_output:
        state_by_org = {item["organisation_number"]: item["state"] for item in envelopes}
        contract_envelopes = []
        contract_problems: list[dict] = []
        for profile in ordered_profiles:
            org = profile["organisation_number"]
            changes = changes_by_org.get(org) if org in previous_profiles else None
            envelope = contract_envelope(
                profile,
                run_id=args.run_id,
                started_at=started_at,
                completed_at=completed_at,
                modules=requested_modules,
                changes=changes,
                terminal_state=state_by_org.get(org),
            )
            for problem in validate_contract_envelope(envelope):
                contract_problems.append({"organisation_number": org, "problem": problem})
            contract_envelopes.append(envelope)
        write_jsonl(Path(args.contract_output), contract_envelopes)
        contract_summary = {
            "path": args.contract_output,
            "envelopes": len(contract_envelopes),
            "validation_problems": len(contract_problems),
            "problems": contract_problems[:20],
        }
    latencies = sorted(operations.pop("latencies_ms"))
    operations["p50_ms"] = latencies[len(latencies) // 2] if latencies else None
    operations["p95_ms"] = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else None
    wikidata_summary = None
    if "wikidata" in requested_modules:
        wikidata_statuses = Counter(
            str((profile.get("evidence") or {}).get("wikidata", {}).get("status"))
            for profile in ordered_profiles
        )
        wikidata_summary = {
            "batch_requests": int(wikidata_metrics.get("requests") or 0),
            "status_counts": dict(sorted(wikidata_statuses.items())),
            "website_candidates_published": sum(
                1 for profile in ordered_profiles
                if (((profile.get("evidence") or {}).get("website") or {}).get("value") or {}).get("website_candidate_source") == "wikidata"
            ),
        }
    youtube_summary = None
    if "youtube_activity" in requested_modules:
        youtube_statuses = Counter(
            str((profile.get("evidence") or {}).get("youtube_activity", {}).get("status"))
            for profile in ordered_profiles
        )
        youtube_summary = {"status_counts": dict(sorted(youtube_statuses.items()))}
    report = {
        "run_id": args.run_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "expected_count": args.expected_count,
        "emitted_envelopes": len(envelopes),
        "resumed_profiles": resumed_profiles,
        "profiles_fetched_this_run": len(pending_profiles),
        "modules": requested_modules,
        "registry": registry_metadata,
        "operations": operations,
        "refresh": refresh_summary,
        "contract_output": contract_summary,
        "wikidata": wikidata_summary,
        "youtube_activity": youtube_summary,
        "validation": validation,
    }
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if validation["passed"] else 1)


if __name__ == "__main__":
    main()
