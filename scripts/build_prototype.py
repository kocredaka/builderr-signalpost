#!/usr/bin/env python3
"""Build the Signalpost prototype: one static HTML file, data embedded as JSON.

UX improvement #25 (AUDIT.md section 4): compare view, generic external-signal
section, change timeline, sort + jobs/family filters, honest batch header,
and research answers precomputed server-side from ``research.py``.

Usage (from the repository root)::

    python scripts/build_prototype.py --input profiles.jsonl \\
        --external-observations observations.jsonl \\
        --contract-envelopes envelopes.jsonl \\
        --output site/index.html

Inputs
------
* ``--input``: profiles JSONL in kit shape (top-level organisation_number,
  name, legal_form, employees, bankrupt, liquidating, municipality,
  industry_code/label, website, latest_submitted_accounts, evidence per
  module, run_metrics; optional ``change_history``).
* ``--external-observations`` (repeatable / multi-value): generic
  observations JSONL, any platform. Accepted fields: id,
  organisation_number, platform, signal_type, source_url (or ``url``),
  retrieved_at, content_sha256, exact_entity, identity_proof,
  evidence_span, rights_status, acquisition_mode, source_class, metrics.
  Observations are rendered generically, labelled by platform and rights
  status. Nothing platform-specific is rendered.
* ``--contract-envelopes`` (multi-value): OUTPUT_CONTRACT.md envelopes
  JSONL (organisation_number, run{run_id, started_at, completed_at,
  terminal_status}, claims, evidence, changes, errors, operations).
  Envelopes supply the batch header (run id + date) and the ``changes[]``
  shown in the per-company timeline. ``changes`` from the envelope are
  merged with the profile's ``change_history`` (deduplicated).
* ``--run-id`` / ``--run-date``: optional overrides for the header. They
  must describe the actual batch; by default both are derived from the
  supplied envelopes, and "not reported" is shown when no run metadata
  is available. Nothing is invented.

Policies enforced by this script
--------------------------------
* The prototype makes NO competition-score claim. The legacy ``--score``,
  ``--control-loop`` and ``--independent-score`` arguments are still
  accepted for backward compatibility but are IGNORED (a warning is
  printed); no score, rubric or qualification copy is rendered.
* "Missing is not zero": missing values render as "Not reported", never 0.
* Research answers ("Ask this company") are generated at build time by
  importing ``norway_company_agent.research.answer_profile`` -- the same
  Python logic as ``scripts/ask_agent.py`` -- and embedded as data. The
  browser JavaScript only selects which precomputed mode to display; it
  never recomputes facts. Hiring/activity modes have no counterpart in
  ``research.py``; they are derived in Python from the supplied
  observations and abstain explicitly when no supporting observation
  exists. Sentiment always abstains (see ``research.py``).
* Quarantined website claims stay quarantined: when the identity gate did
  not publish the site, the title/description are replaced by a quarantine
  notice and only the quarantined counts are shown.
* No network access, no credentials. Everything is built from local files.
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Acquisition modes that may be labelled publishable. This mirrors
# norway_company_agent.external_footprint.PUBLISHABLE_ACQUISITION_MODES;
# tests/test_prototype_ux25.py asserts the two stay equal. The prototype
# label is a conservative *display* label, not publishable_observation():
# an item is labelled publishable only when it is exact-entity verified,
# rights-approved, acquired via an approved mode, and carries a URL plus a
# 64-hex content hash.
PUBLISHABLE_ACQUISITION_MODES = {
    "official_api",
    "licensed_api",
    "company_authorized_export",
    "permitted_public_page",
}

JOB_SIGNAL_TYPES = {"job_posting"}

# Signal types treated as public-activity evidence for the "Activity" agent
# mode and the activity compare family. Anything else (reviews, handles,
# metrics, unknown types) still renders in the generic external section;
# it is simply not counted as activity evidence.
ACTIVITY_SIGNAL_TYPES = {
    "public_post",
    "public_mention",
    "buzz_metrics",
    "news",
    "news_article",
    "company_news",
    "activity",
}

# Questions routed through research.answer_profile, one per agent mode.
# The wording is chosen so each question selects exactly one topic branch
# in answer_profile (see research.py topic routing); the equivalence test
# in tests/test_prototype_ux25.py pins this mapping.
_RESEARCH_MODE_QUESTIONS = {
    "overview": "Tell me about this company.",
    "financials": "What are the financials, revenue and debt?",
    "leaders": "Who leads this company?",
    "locations": "Where are the company locations?",
    "social": "What social profiles are declared?",
    "sentiment": "What is the sentiment?",
}


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


_answer_profile_fn = None
_answer_profile_failed = False


def _load_answer_profile():
    """Import answer_profile from the package (same logic as ask_agent.py).

    Raises RuntimeError instead of falling back to a divergent local
    re-implementation: a silent fallback is exactly the JS/Python drift
    this improvement removes.
    """
    global _answer_profile_fn, _answer_profile_failed
    if _answer_profile_fn is not None:
        return _answer_profile_fn
    if _answer_profile_failed:
        raise RuntimeError("norway_company_agent.research.answer_profile is unavailable")
    src_dir = Path(__file__).resolve().parents[1] / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))
    try:
        from norway_company_agent.research import answer_profile
    except Exception as exc:  # pragma: no cover - import failure is environmental
        _answer_profile_failed = True
        raise RuntimeError(
            "build_prototype requires norway_company_agent.research.answer_profile; "
            "run it from the repository checkout so src/ is importable."
        ) from exc
    _answer_profile_fn = answer_profile
    return answer_profile


def normalize_independent_score(independent_score: dict | None) -> dict:
    """DEPRECATED. Kept only for backward compatibility with test_poc.py.

    It is no longer called by build(): the prototype renders no score of
    any kind. Do not use it to make a competition-score claim. It can be
    deleted once the core worker retires the matching test.
    """
    result = dict(independent_score or {})
    if not result:
        return result
    score = result.get("score") or {}
    submission = score.get("submission_1000") or {}
    identity = (result.get("qualification") or {}).get("identity_gate") or {}
    if submission:
        result["qualification_passed"] = bool((result.get("qualification") or {}).get("passed"))
        result["raw_score"] = submission.get("raw_score", score.get("raw_score", 0))
        result["category_scores"] = {
            key: value.get("score", 0) for key, value in (submission.get("categories") or {}).items()
        }
        result["identity_audit"] = {
            "published_domains_correct": identity.get("published_domains_exact", 0),
            "published_socials_correct": identity.get("published_socials_exact", 0),
        }
        result["proxy"] = result.get("proxy") or {
            "submission_1000": (result.get("proxy_results_not_adopted") or {}).get("submission_1000", 0),
            "extension_250": (result.get("proxy_results_not_adopted") or {}).get("extension_250", 0),
        }
    return result


def qualification_copy(score: dict | None, independent_score: dict | None = None) -> tuple[str, str]:
    """DEPRECATED. Kept only for backward compatibility with test_poc.py.

    It is no longer called by build(): the prototype renders no score,
    rubric or qualification copy. Do not use it to make a
    competition-score claim. It can be deleted once the core worker
    retires the matching test.
    """
    score = score or {}
    independent_score = normalize_independent_score(independent_score)
    if independent_score.get("qualification_passed"):
        independent = independent_score.get("raw_score", 0)
        proxy = (independent_score.get("proxy") or {}).get("submission_1000", score.get("raw_score", 0))
        return (
            f"Independent judge {independent}/100 · qualification passed · proxy {proxy}/100",
            "The independent score reached the 80-point target on both frozen corpora. Exact-entity publication, filing history and fresh PDF checks passed; calendar-time operation, open-ended research and broad context coverage remain unproven.",
        )
    if score.get("scorer") == "signalpost_all_source_completeness_v1":
        combined = score.get("combined") or {}
        return (
            f"Evidence completeness {combined.get('experimental_completeness_mean', 0):.2f}/100 experimental · {combined.get('strict_completeness_mean', 0):.2f}/100 strict",
            "LinkedIn discovery now enriches exact company records, but automated LinkedIn captures remain experimental. Company sites and social handles only enter the strict layer after independent identity verification.",
        )
    if "raw_score" in score:
        raw = score.get("raw_score", 0)
        if score.get("qualification_passed"):
            return (
                f"Competition proxy qualified · {raw}/100 · independent hidden score still required",
                "The corrected proxy passes its current gates. Final ranking still requires evaluator-owned hidden batches and a fresh independent review.",
            )
        failures = len(score.get("unproven_or_failed") or [])
        return (
            f"Competition proxy {raw}/100 · {failures} gate(s) unproven or failed",
            "This is an optimization measurement, not an official competition score. Open the scorecard to see the remaining hard gates.",
        )
    qualification = score.get("qualification") or {}
    weighted = score.get("weighted_score") or {}
    if qualification.get("poc_qualified"):
        points = weighted.get("verified_points")
        maximum = weighted.get("maximum_points")
        points_copy = f"{points}/{maximum} verified core points" if points is not None and maximum is not None else "core gate passed"
        return (
            f"POC qualified · {points_copy} · not production-qualified",
            "Frozen core checks passed. Search-based discovery and sentiment remain quarantined pending their own external evaluation.",
        )
    return (
        "1,000-entity frozen POC · not a production service",
        "Qualification is pending; inspect the evidence and scorecard before making a product claim.",
    )


# ---------------------------------------------------------------------------
# Generic external observations
# ---------------------------------------------------------------------------

def observation_url(item: dict) -> str | None:
    """Source URL of an observation; accepts source_url, url, profile_url."""
    return item.get("source_url") or item.get("url") or item.get("profile_url")


def observation_title(item: dict) -> str:
    metrics = item.get("metrics") or {}
    for key in ("title", "name", "role"):
        if metrics.get(key):
            return str(metrics[key])
    span = item.get("evidence_span") or item.get("text") or ""
    if span:
        text = str(span).strip()
        return text if len(text) <= 120 else text[:117] + "..."
    return f"{item.get('platform') or 'unknown platform'} {item.get('signal_type') or 'observation'}"


def normalize_observation(item: dict) -> dict:
    """Normalize one generic observation for embedding (any platform)."""
    url = observation_url(item)
    metrics = item.get("metrics") or {}
    content_hash = item.get("content_sha256")
    publishable = bool(
        item.get("exact_entity")
        and item.get("rights_status") == "approved"
        and item.get("acquisition_mode") in PUBLISHABLE_ACQUISITION_MODES
        and url
        and isinstance(content_hash, str)
        and len(content_hash) == 64
    )
    return {
        "id": item.get("id"),
        "platform": item.get("platform"),
        "signalType": item.get("signal_type"),
        "title": observation_title(item),
        "text": item.get("evidence_span") or item.get("text"),
        "url": url,
        "retrievedAt": item.get("retrieved_at"),
        "publishedAt": item.get("published_at") or item.get("date_published") or metrics.get("date_posted"),
        "hash": content_hash,
        "rightsStatus": item.get("rights_status"),
        "acquisitionMode": item.get("acquisition_mode"),
        "exactEntity": bool(item.get("exact_entity")),
        "identityProof": item.get("identity_proof"),
        "sourceClass": item.get("source_class"),
        "sentimentLabel": item.get("sentiment_label"),
        "metrics": metrics,
        "publishable": publishable,
    }


def summarize_observations(items: list[dict]) -> dict:
    return {
        "total": len(items),
        "publishable": sum(1 for item in items if item["publishable"]),
        "experimental": sum(1 for item in items if not item["publishable"]),
        "platforms": sorted({str(item["platform"]) for item in items if item.get("platform")}),
        "bySignalType": dict(sorted(Counter(str(item["signalType"]) for item in items if item.get("signalType")).items())),
        "exactJobs": sum(1 for item in items if item["signalType"] in JOB_SIGNAL_TYPES and item["exactEntity"]),
        "totalJobs": sum(1 for item in items if item["signalType"] in JOB_SIGNAL_TYPES),
        "exactActivity": sum(1 for item in items if item["signalType"] in ACTIVITY_SIGNAL_TYPES and item["exactEntity"]),
    }


# ---------------------------------------------------------------------------
# Changes (refresh timeline)
# ---------------------------------------------------------------------------

def _change_hash(change: dict, prefix: str) -> str | None:
    for key in (f"{prefix}_content_sha256", f"{prefix}_hash"):
        if change.get(key):
            return change[key]
    hashes = change.get("hashes") or {}
    if isinstance(hashes, dict):
        return hashes.get(prefix) or hashes.get(f"{prefix}_content_sha256")
    return None


def normalize_change(change: dict) -> dict:
    return {
        "field": change.get("field"),
        "oldValue": change.get("old_value"),
        "newValue": change.get("new_value"),
        "sourceUrl": change.get("source_url"),
        "retrievedAt": change.get("retrieved_at"),
        "effectiveAt": change.get("effective_at"),
        "status": change.get("status"),
        "oldHash": _change_hash(change, "old"),
        "newHash": _change_hash(change, "new"),
    }


def merge_changes(row: dict, envelope: dict | None) -> list[dict]:
    """Merge profile change_history + envelope changes[], deduplicated."""
    seen: set[str] = set()
    merged: list[dict] = []
    candidates = list(row.get("change_history") or []) + list((envelope or {}).get("changes") or [])
    for raw in candidates:
        if not isinstance(raw, dict):
            continue
        change = normalize_change(raw)
        key = json.dumps(change, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        merged.append(change)
    merged.sort(key=lambda c: (c["retrievedAt"] or "", str(c["field"])))
    return merged


# ---------------------------------------------------------------------------
# Jobs availability + group summary
# ---------------------------------------------------------------------------

def derive_jobs(envelope: dict | None, observations: list[dict]) -> dict:
    """Jobs availability state for filtering.

    "has": a contract claim says job data is available, or at least one
    exact-entity job posting observation exists.
    "none": jobs were checked and no opening was found -- either a
    contract claim with availability ``not_available``, or observations
    were supplied for this company but contain no job posting. This is
    NOT proof the company has no vacancies.
    "not_checked": no contract claim and no observations for this company.
    """
    for claim in (envelope or {}).get("claims") or []:
        field = str(claim.get("field") or "").casefold()
        if "job" in field or "hiring" in field:
            availability = claim.get("availability")
            if availability == "available":
                return {"status": "has", "source": "contract claim"}
            if availability == "not_available":
                return {"status": "none", "source": "contract claim"}
            return {"status": "not_checked", "source": "contract claim unresolved"}
    exact = [o for o in observations if o.get("signal_type") in JOB_SIGNAL_TYPES and o.get("exact_entity")]
    if exact:
        return {"status": "has", "source": "observations"}
    if observations:
        return {"status": "none", "source": "observations supplied, no job postings"}
    return {"status": "not_checked", "source": "no observations supplied"}


def summarize_group(value) -> dict:
    """Summarize the group-structure evidence value.

    Core improvement #7 normalizes the konsernstruktur body into
    ``relationships`` (only pairs with a valid 9-digit organisation number
    on both sides). When that shape is present, the normalized
    relationships are exposed (capped) for display; otherwise the raw
    body is summarized structurally only -- parent/subsidiary relations
    are never invented from raw keys.
    """
    if value is None:
        return {"kind": "none", "keys": [], "keyCount": 0, "listItems": 0}
    if isinstance(value, dict):
        relationships = value.get("relationships")
        if isinstance(relationships, list):
            return {
                "kind": "normalized",
                "keys": [],
                "keyCount": 0,
                "listItems": len(relationships),
                "relationship_count": value.get("relationship_count", len(relationships)),
                "children_returned": value.get("children_returned"),
                "skipped_without_valid_org_numbers": value.get("skipped_without_valid_org_numbers"),
                "relationships": [
                    {
                        "from_org": rel.get("from_organisation_number"),
                        "from_name": rel.get("from_name"),
                        "to_org": rel.get("to_organisation_number"),
                        "to_name": rel.get("to_name"),
                        "relationship": rel.get("relationship"),
                        "date": rel.get("date"),
                    }
                    for rel in relationships[:8]
                    if isinstance(rel, dict)
                ],
            }
        keys = list(value.keys())
        return {
            "kind": "object",
            "keys": [str(key) for key in keys[:20]],
            "keyCount": len(keys),
            "listItems": sum(len(v) for v in value.values() if isinstance(v, list)),
        }
    if isinstance(value, list):
        return {"kind": "list", "keys": [], "keyCount": 0, "listItems": len(value)}
    return {"kind": type(value).__name__, "keys": [], "keyCount": 0, "listItems": 0}


def envelope_summary(envelope: dict | None) -> dict | None:
    if not envelope:
        return None
    run = envelope.get("run") or {}
    operations = envelope.get("operations") or {}
    return {
        "runId": run.get("run_id"),
        "terminalStatus": run.get("terminal_status"),
        "errorCount": len(envelope.get("errors") or []),
        "claimCount": len(envelope.get("claims") or []),
        "evidenceCount": len(envelope.get("evidence") or []),
        "requests": operations.get("requests"),
        "runtimeMs": operations.get("runtime_ms"),
        "thirdPartyCostUsd": operations.get("third_party_cost_usd"),
    }


# ---------------------------------------------------------------------------
# Server-side research answers (from research.py)
# ---------------------------------------------------------------------------

def _observation_fact(item: dict, claim: str) -> dict:
    return {
        "claim": claim,
        "value": item["title"],
        "detail": item.get("text"),
        "classification": "external_observation",
        "source_url": item.get("url"),
        "retrieved_at": item.get("retrievedAt"),
        "source_class": item.get("sourceClass"),
        "content_sha256": item.get("hash"),
        "rights_status": item.get("rightsStatus"),
        "acquisition_mode": item.get("acquisitionMode"),
        "platform": item.get("platform"),
        "signal_type": item.get("signalType"),
        "exact_entity": item.get("exactEntity"),
    }


def _observation_mode_answer(question: str, items: list[dict], signal_types: set[str], label: str) -> dict:
    """Hiring/activity answers derived in Python from observations.

    research.py has no hiring/activity topic, so these modes are built
    here -- but at build time, in Python, from the same normalized
    observations the page renders. Only exact-entity items are listed as
    evidence; every other case abstains explicitly.
    """
    matching = [item for item in items if item["signalType"] in signal_types]
    exact = [item for item in matching if item["exactEntity"]]
    facts = [_observation_fact(item, f"{label}: {item['title']}") for item in exact]
    unsupported: list[str] = []
    if exact:
        if len(matching) > len(exact):
            unsupported.append(
                f"{len(matching) - len(exact)} additional {label.lower()} observation(s) were supplied "
                "but are not verified as this exact legal entity; see External signals."
            )
    elif matching:
        unsupported.append(
            f"No {label.lower()} observation in the supplied data is verified as this exact legal "
            "entity. Status was not established; missing is not zero."
        )
    elif items:
        unsupported.append(
            f"No {label.lower()} observations were supplied for this company in this batch. "
            "Status was not established; missing is not zero."
        )
    else:
        unsupported.append(
            f"No external observations were supplied for this company, so {label.lower()} "
            "was not checked. Missing is not zero."
        )
    return {
        "question": question,
        "facts": facts,
        "unsupported_or_uncertain": unsupported,
        "answer_policy": "Only exact-entity observations are listed as evidence; anything else abstains.",
        "counts": {"exact": len(exact), "total": len(matching)},
    }


def build_agent_answers(row: dict, observations: list[dict], answer_profile_fn=None) -> dict:
    """Precompute every "Ask this company" mode at build time.

    Research modes come straight from ``research.answer_profile``; the
    returned fact/unsupported lists are embedded verbatim, so the page
    cannot drift from the Python logic. A per-mode failure is recorded
    as an explicit unsupported note, never silently dropped.
    """
    answer_profile_fn = answer_profile_fn or _load_answer_profile()
    answers: dict[str, dict] = {}
    for mode, question in _RESEARCH_MODE_QUESTIONS.items():
        try:
            result = answer_profile_fn(row, question)
            answers[mode] = {
                "question": result.get("question", question),
                "facts": result.get("facts", []),
                "unsupported_or_uncertain": list(result.get("unsupported_or_uncertain", [])),
                "answer_policy": result.get("answer_policy"),
            }
        except Exception as exc:  # keep the build robust; the failure stays visible
            answers[mode] = {
                "question": question,
                "facts": [],
                "unsupported_or_uncertain": [f"Answer could not be generated from the Python research layer: {exc}"],
                "answer_policy": None,
            }
    sentiment_labelled = sum(1 for item in observations if item.get("sentimentLabel") is not None)
    if sentiment_labelled:
        answers["sentiment"]["unsupported_or_uncertain"].append(
            f"{sentiment_labelled} supplied observation(s) carry sentiment labels, but sentiment "
            "is not qualified and no sentiment score is shown."
        )
    answers["hiring"] = _observation_mode_answer(
        "What job postings are available?", observations, JOB_SIGNAL_TYPES, "Job posting"
    )
    answers["activity"] = _observation_mode_answer(
        "What recent public activity is available?", observations, ACTIVITY_SIGNAL_TYPES, "Public activity"
    )
    return answers


# ---------------------------------------------------------------------------
# Profile compaction (embedded payload)
# ---------------------------------------------------------------------------

def compact(row: dict, external_observations: list[dict] | None = None, envelope: dict | None = None, answer_profile_fn=None) -> dict:
    evidence = row.get("evidence", {})
    financial = evidence.get("financials", {})
    financial_history = evidence.get("financial_history", {})
    roles = evidence.get("roles", {})
    locations = evidence.get("locations", {})
    group = evidence.get("group", {})
    website = evidence.get("website", {})
    website_value = dict(website.get("value") or {})
    if website.get("status") == "available" and not website_value.get("title"):
        website_value["title"] = "Company site fetched"
    identity_assessment = website_value.get("identity_assessment") or {}
    if website.get("status") == "available" and identity_assessment and not identity_assessment.get("publishable"):
        website_value["quarantined_title"] = website_value.get("title")
        website_value["quarantined_description"] = website_value.get("description")
        website_value["quarantined_social_count"] = len(website_value.get("discovered_social_links") or [])
        website_value["title"] = "Registry-linked site — identity not verified"
        website_value["description"] = "Fetched content is retained as discovered evidence but is not attributed to this legal entity."
    live = evidence.get("registry_live", {})
    accounting = evidence.get("accounting_obligation", {})
    external_observations = external_observations or []
    normalized = [normalize_observation(item) for item in external_observations]

    verified_handles = {}
    for item in external_observations:
        if item.get("signal_type") != "profile_handle" or not item.get("exact_entity"):
            continue
        url = item.get("profile_url") or item.get("source_url") or item.get("url")
        if url:
            verified_handles[(item.get("platform"), url)] = {
                "platform": item.get("platform"),
                "url": url,
                "rightsStatus": item.get("rights_status"),
            }
    external = {
        "handles": list(verified_handles.values()),
        "items": normalized,
        "summary": summarize_observations(normalized),
    }

    def meta(record: dict) -> dict:
        return {
            "status": record.get("status", "not_run"),
            "source": record.get("source_url"),
            "sourceClass": record.get("source_class") or record.get("source_type"),
            "retrievedAt": record.get("retrieved_at"),
            "effectiveAt": record.get("effective_at") or record.get("as_of"),
            "hash": record.get("content_sha256"),
            "rowKey": record.get("source_row_key"),
        }

    jobs = derive_jobs(envelope, external_observations)
    return {
        "org": row["organisation_number"],
        "name": row["name"],
        "form": row["legal_form"],
        "employees": row["employees"],
        "municipality": row["municipality"],
        "industryCode": row["industry_code"],
        "industry": row["industry_label"],
        "website": row["website"],
        "adverse": bool(row["bankrupt"] or row["liquidating"]),
        "slice": row.get("sample_slice"),
        "split": row.get("evaluation_split"),
        "latestAccounts": row.get("latest_submitted_accounts"),
        "registrySource": evidence.get("registry", {}).get("source_url"),
        "registry": meta(evidence.get("registry", {})),
        "accounting": {**meta(accounting), "value": accounting.get("value") or {}},
        "financial": {**meta(financial), "records": (financial.get("value") or {}).get("records", [])[:3]},
        "financialHistory": {**meta(financial_history), "pdfs": (financial_history.get("value") or {}).get("pdfs", [])},
        "roles": {**meta(roles), "items": (roles.get("value") or {}).get("roles", [])[:30]},
        "locations": {**meta(locations), "items": (locations.get("value") or {}).get("locations", [])[:30]},
        "group": {**meta(group), "summary": summarize_group(group.get("value"))},
        "web": {**meta(website), "value": website_value},
        "liveStatus": live.get("status", "not_run"),
        "changes": merge_changes(row, envelope),
        "jobs": {**jobs, "exactCount": summarize_observations(normalized)["exactJobs"], "totalCount": summarize_observations(normalized)["totalJobs"]},
        "jobsStatus": jobs["status"],
        "contract": envelope_summary(envelope),
        "agentAnswers": build_agent_answers(row, normalized, answer_profile_fn),
        "external": external,
    }


# ---------------------------------------------------------------------------
# Batch header metadata
# ---------------------------------------------------------------------------

def derive_run_meta(row_count: int, envelopes_by_org: dict[str, dict] | None, run_id: str | None = None, run_date: str | None = None) -> dict:
    runs = [env.get("run") or {} for env in (envelopes_by_org or {}).values()]
    run_ids = [str(run.get("run_id")) for run in runs if run.get("run_id")]
    chosen_id = run_id or (Counter(run_ids).most_common(1)[0][0] if run_ids else None)
    matching = [run for run in runs if str(run.get("run_id")) == chosen_id] if chosen_id else []
    first = matching[0] if matching else (runs[0] if runs else {})
    return {
        "count": row_count,
        "runId": chosen_id,
        "startedAt": first.get("started_at"),
        "completedAt": run_date or first.get("completed_at"),
        "terminalStatuses": sorted({str(run.get("terminal_status")) for run in runs if run.get("terminal_status")}),
        "envelopeCount": len(envelopes_by_org or {}),
    }


def batch_snapshot_text(meta: dict) -> str:
    count = meta["count"]
    noun = "company" if count == 1 else "companies"
    run = meta["runId"] or "run not reported"
    completed = meta.get("completedAt") or ""
    date = completed[:10] if completed else "date not reported"
    return f"{count} {noun} · run {run} · {date}"

# ---------------------------------------------------------------------------
# HTML template. Tokens __DATA_JSON__, __BATCH_META_JSON__ and
# __SNAPSHOT_TEXT__ are replaced by build(); the template is a plain string
# (not an f-string) so the JavaScript/CSS braces need no escaping.
# ---------------------------------------------------------------------------

HTML_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="icon" href="data:,">
<title>Signalpost laboratory — Norway company intelligence</title><meta name="description" content="Verified external company signals grounded in official Norwegian company records.">
<style>
:root{--paper:#f5f1e8;--white:#fffdf8;--ink:#171614;--muted:#6c685f;--line:#d8d0c2;--accent:#7d2432;--good:#276044;--warn:#98620d;--linkedin:#0a66c2}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,sans-serif}button,input,select{font:inherit}a{color:var(--accent)}.mast{border-bottom:1px solid var(--line);background:var(--white);padding:18px 28px;display:flex;justify-content:space-between;align-items:center;gap:20px}.brand{font:700 18px Georgia,serif;letter-spacing:.02em}.brand i{display:inline-block;width:11px;height:11px;background:var(--accent);transform:rotate(45deg);margin-right:10px}.snapshot{color:var(--muted);font-size:12px}.hero{max-width:1500px;margin:auto;padding:38px 28px 24px}.eyebrow{font-size:11px;text-transform:uppercase;letter-spacing:.14em;color:var(--accent);font-weight:800}h1{font:600 clamp(38px,6vw,78px)/.98 Georgia,serif;letter-spacing:-.04em;max-width:980px;margin:10px 0 18px}.lede{max-width:820px;color:var(--muted);font-size:17px}.stats{display:grid;grid-template-columns:repeat(5,minmax(120px,1fr));gap:10px;margin-top:24px}.stat{background:var(--white);border:1px solid var(--line);padding:15px;border-radius:4px}.stat strong{display:block;font:600 25px Georgia,serif}.stat span{color:var(--muted);font-size:12px}.shell{max-width:1500px;margin:auto;padding:0 28px 50px;display:grid;grid-template-columns:280px minmax(0,1fr) 278px;gap:18px;align-items:start}.panel{background:var(--white);border:1px solid var(--line);border-radius:5px}.index-head{padding:15px 16px 0;display:flex;align-items:baseline;justify-content:space-between}.index-head strong{font:600 24px Georgia,serif}.controls{padding:12px 16px 16px;border-bottom:1px solid var(--line);display:grid;gap:9px;position:sticky;top:0;background:var(--white);z-index:2}.controls input,.controls select{width:100%;border:1px solid var(--line);background:white;padding:10px;border-radius:3px}.filters,.workspace-actions{display:grid;grid-template-columns:1fr 1fr;gap:8px}.filters select.full{grid-column:1/-1}.workspace-actions button,.pin-button{border:1px solid var(--line);background:var(--white);padding:8px;border-radius:3px;cursor:pointer}.workspace-actions button:hover,.pin-button:hover{border-color:var(--accent);color:var(--accent)}.result-count{font-size:12px;color:var(--muted)}.list{max-height:950px;overflow:auto}.row-wrap{display:flex;align-items:stretch;border-bottom:1px solid var(--line)}.compare-check{margin:14px 0 14px 12px;flex:none;cursor:pointer}.row{flex:1;min-width:0;border:0;background:transparent;text-align:left;padding:13px 16px 13px 8px;cursor:pointer}.row:hover,.row.active{background:#f0e8dc}.row strong,.row span{display:block}.row span{font-size:12px;color:var(--muted)}.profile{padding:26px}.profile-head{display:flex;justify-content:space-between;gap:20px;border-bottom:1px solid var(--line);padding-bottom:20px}h2{font:600 38px/1.05 Georgia,serif;margin:6px 0}.chips{display:flex;gap:6px;flex-wrap:wrap}.chip{font-size:11px;border:1px solid var(--line);padding:3px 7px;border-radius:999px}.chip.good{color:var(--good);border-color:#9db9aa}.chip.warn{color:var(--warn);border-color:#d4b77e}.chip.experimental{color:var(--warn);border-color:#d4b77e;background:#fdf6e3}.fact-grid,.signal-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin:20px 0}.fact,.signal-card{border:1px solid var(--line);padding:13px;min-width:0}.fact span,.signal-card span,.section-label{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em}.fact strong,.signal-card strong{display:block;margin-top:4px;overflow-wrap:anywhere}section{padding:20px 0;border-top:1px solid var(--line)}section:first-of-type{border-top:0}h3{font:600 22px Georgia,serif;margin:5px 0 12px}.table-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{color:var(--muted)}.empty{color:var(--muted);font-style:italic}.source{font-size:12px;color:var(--muted);margin-top:10px;overflow-wrap:anywhere}.source code{font-size:10px;word-break:break-all}.people,.locations,.social{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}.item{border:1px solid var(--line);padding:10px}.item small{display:block;color:var(--muted)}.activity-list{display:grid;gap:8px}.activity{border-left:2px solid var(--line);padding:2px 0 2px 12px}.activity p{margin:4px 0}.activity small{color:var(--muted)}.experimental-note{border-left:3px solid var(--warn);background:#fdf6e3;padding:10px 12px;color:#5c3d05;font-size:12px}.agent{padding:18px;position:sticky;top:18px;background:var(--ink);color:white}.agent .eyebrow{color:#ddb6bd}.agent h3{font-size:23px}.agent p{color:#d8d0c8}.agent-form{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:7px;margin:14px 0 8px}.agent-form input{min-width:0;border:1px solid #625d57;background:#272522;color:white;padding:9px;border-radius:3px}.agent-form input::placeholder{color:#aaa39b}.agent-buttons{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin:8px 0 14px}.agent button{border:1px solid #625d57;background:transparent;color:white;padding:7px;text-align:left;border-radius:3px;cursor:pointer}.agent button:hover,.agent button.active{background:var(--accent);border-color:var(--accent)}.agent-output{border-top:1px solid #625d57;padding-top:14px}.agent-output strong{color:white}.agent-output a{color:#f1c8cf}.agent-question{border-left:2px solid #ddb6bd;padding-left:9px}.answer-block+.answer-block{border-top:1px solid #494540;padding-top:8px}.abstain{border-left:2px solid #ddb6bd;padding-left:9px}.workspace-summary{margin-top:16px;padding-top:14px;border-top:1px solid #625d57;font-size:12px;color:#d8d0c8}.compare-wrap{max-width:1500px;margin:0 auto;padding:0 28px 18px}.compare-panel{padding:20px 22px}.compare-panel h2{font-size:28px}.compare-table{min-width:720px}.compare-table th:first-child,.compare-table td:first-child{position:sticky;left:0;background:var(--white);z-index:1;min-width:150px}.compare-table tr.family-row th{background:#f0e8dc;font-size:12px;text-transform:uppercase;letter-spacing:.08em}.compare-table .cell-meta{margin-top:6px}.compare-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.compare-actions button{border:1px solid var(--line);background:var(--white);padding:8px 12px;border-radius:3px;cursor:pointer}.timeline{list-style:none;margin:0;padding:0;display:grid;gap:12px}.change-item{border-left:3px solid var(--accent);padding:2px 0 2px 12px}.change-item .change-values{margin:4px 0;overflow-wrap:anywhere}.change-item small{color:var(--muted);overflow-wrap:anywhere}.change-item code{font-size:10px;word-break:break-all}.obs-group{margin-top:16px}.obs-item{border:1px solid var(--line);padding:12px;margin-top:8px}.obs-item p{margin:6px 0}.obs-item small{color:var(--muted);overflow-wrap:anywhere}.obs-item code{font-size:10px;word-break:break-all}.obs-labels{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
@media(max-width:1150px){.shell{grid-template-columns:280px minmax(0,1fr)}.agent{grid-column:2;position:static}}
@media(max-width:900px){.stats{grid-template-columns:repeat(2,1fr)}.shell{grid-template-columns:1fr}.list{max-height:420px}.profile{order:2}.agent{grid-column:1;order:3}}
@media(max-width:520px){.mast,.hero,.shell,.compare-wrap{padding-left:16px;padding-right:16px}.mast{align-items:flex-start}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}.stat{padding:11px}.fact-grid,.signal-grid,.people,.locations,.social{grid-template-columns:1fr}.profile{padding:18px}.compare-panel{padding:16px}h2{font-size:30px}.compare-table{min-width:640px}}
.controls input{font-size:13px}
</style></head><body>
<header class="mast"><div class="brand"><i></i>Signalpost laboratory</div><div class="snapshot">__SNAPSHOT_TEXT__</div></header>
<main><div class="hero"><div class="eyebrow">Norway company intelligence agent</div><h1>Know the company behind the registration.</h1><p class="lede">Start with official Norwegian identity and filings. Then follow the company into the public world: its website, leaders, hiring signals, public activity, locations and declared social handles — each with its source, retrieval date and evidence status.</p><p style="max-width:920px;border-left:3px solid var(--warn);padding:8px 12px;margin:18px 0 0;color:#5c3d05;background:#fdf6e3;font-size:13px">Missing is not zero. External observations are labelled by platform and rights status; experimental observations are not presented as verified facts. This prototype makes no competition-score claim.</p><div class="stats" id="stats"></div></div>
<div class="compare-wrap"><section id="compare-panel" class="panel compare-panel" hidden></section></div>
<div class="shell"><aside class="panel"><div class="index-head"><div><div class="eyebrow">Company index</div><strong>Companies</strong></div><strong id="index-total"></strong></div><div class="controls"><input id="q" type="search" placeholder="Search companies" aria-label="Search companies"><div class="filters"><select id="form" aria-label="Legal form"><option value="">All forms</option></select><select id="state" aria-label="Status"><option value="">Any status</option><option value="active">Active</option><option value="adverse">Adverse</option></select><select id="finance" aria-label="Financial availability"><option value="">Any accounts state</option><option value="available">Accounts available</option><option value="not_found">No account returned</option></select><select id="slice" aria-label="Sample slice"><option value="">Both slices</option><option value="population">Population</option><option value="stress">Stress</option><option value="extension">Extension</option></select><select id="jobs" aria-label="Jobs availability"><option value="">Any jobs state</option><option value="has">Has openings</option><option value="none">No openings in supplied observations</option><option value="not_checked">Jobs not checked</option></select><select id="group" aria-label="Group availability"><option value="">Any group state</option><option value="available">Group structure returned</option><option value="not_found">No group structure returned</option><option value="not_checked">Group not checked</option></select><select id="external" aria-label="External signals"><option value="">Any external signals</option><option value="any">Has external observations</option><option value="approved">Approved observations only</option><option value="none">No observations supplied</option></select><select id="sort" class="full" aria-label="Sort results"><option value="name-asc">Sort: name A–Z</option><option value="name-desc">Sort: name Z–A</option><option value="employees-desc">Sort: employees, high to low</option><option value="employees-asc">Sort: employees, low to high</option><option value="revenue-desc">Sort: revenue, high to low</option><option value="revenue-asc">Sort: revenue, low to high</option><option value="accounts-desc">Sort: latest accounts, newest first</option><option value="municipality-asc">Sort: municipality A–Z</option></select></div><div class="workspace-actions"><button id="save-view" type="button">Save view</button><button id="export-view" type="button">Export results</button></div><div class="result-count" id="count"></div></div><div class="list" id="list"></div></aside><article class="panel profile" id="profile"></article><aside class="panel agent"><div class="eyebrow">Research agent</div><h3>Ask this company.</h3><p>Short, sourced answers precomputed from the selected record by the Python research layer.</p><form class="agent-form" id="agent-form"><input id="agent-question" type="text" aria-label="Ask about the selected company" placeholder="Hiring? Leaders? Revenue?"><button type="submit">Ask</button></form><div class="agent-buttons" id="agent-buttons"><button data-mode="overview" class="active">Overview</button><button data-mode="financials">Financials</button><button data-mode="leaders">Leaders</button><button data-mode="locations">Locations</button><button data-mode="social">Social</button><button data-mode="hiring">Hiring</button><button data-mode="activity">Activity</button><button data-mode="sentiment">Sentiment</button></div><div class="agent-output" id="agent-output" aria-live="polite"></div><div class="workspace-summary" id="workspace-summary"></div></aside></div></main>
"""

HTML_TEMPLATE += """<script>const DATA=__DATA_JSON__;const BATCH=__BATCH_META_JSON__;const $=s=>document.querySelector(s);const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const norm=v=>String(v??'').normalize('NFD').replace(/[\\u0300-\\u036f]/g,'').toLowerCase();const money=(v,c)=>v==null?'Not reported':new Intl.NumberFormat('en-US',{maximumFractionDigits:0}).format(v)+' '+(c||'');
const forms=[...new Set(DATA.map(x=>x.form).filter(Boolean))].sort();$('#form').innerHTML+=[...forms].map(x=>`<option>${esc(x)}</option>`).join('');
const available=(x,k)=>x[k]?.status==='available';const latestRecord=x=>x.financial.records[0]||null;const revenueOf=x=>latestRecord(x)?.revenue??null;
function filtered(){const q=norm($('#q').value),f=$('#form').value,s=$('#state').value,fin=$('#finance').value,sl=$('#slice').value,jb=$('#jobs').value,gr=$('#group').value,ex=$('#external').value,sort=$('#sort').value;const rows=DATA.filter(x=>(!q||norm([x.name,x.org,x.municipality,x.industry,x.industryCode].join(' ')).includes(q))&&(!f||x.form===f)&&(!s||(s==='adverse')===x.adverse)&&(!fin||x.financial.status===fin)&&(!sl||x.slice===sl)&&(!jb||x.jobsStatus===jb)&&(!gr||(gr==='not_checked'?['not_run','not_fetched'].includes(x.group.status):x.group.status===gr))&&(!ex||(ex==='any'?x.external.summary.total>0:ex==='approved'?x.external.summary.publishable>0:x.external.summary.total===0)));const nullsLast=(a,b)=>a==null&&b==null?0:a==null?1:b==null?-1:0;const byName=(a,b)=>a.name.localeCompare(b.name,'nb');const comparators={'name-asc':byName,'name-desc':(a,b)=>byName(b,a),'employees-desc':(a,b)=>nullsLast(a.employees,b.employees)||(b.employees-a.employees),'employees-asc':(a,b)=>nullsLast(a.employees,b.employees)||(a.employees-b.employees),'revenue-desc':(a,b)=>nullsLast(revenueOf(a),revenueOf(b))||(revenueOf(b)-revenueOf(a)),'revenue-asc':(a,b)=>nullsLast(revenueOf(a),revenueOf(b))||(revenueOf(a)-revenueOf(b)),'accounts-desc':(a,b)=>nullsLast(a.latestAccounts,b.latestAccounts)||String(b.latestAccounts).localeCompare(String(a.latestAccounts)),'municipality-asc':(a,b)=>(a.municipality||'').localeCompare(b.municipality||'','nb')||byName(a,b)};return rows.sort(comparators[sort]||byName);}
function renderStats(){const finance=DATA.filter(x=>available(x,'financial')).length,web=DATA.filter(x=>available(x,'web')).length,external=DATA.filter(x=>x.external.summary.total>0).length,jobs=DATA.reduce((n,x)=>n+(x.external.summary.exactJobs||0),0);$('#index-total').textContent=DATA.length;$('#q').placeholder=`Search the ${DATA.length} companies`;$('#stats').innerHTML=[[DATA.length,'companies in this batch'],[finance,'annual accounts returned'],[web,'company websites captured'],[external,'companies with external observations'],[jobs,'exact-entity job postings']].map(([v,l])=>`<div class="stat"><strong>${v}</strong><span>${l}</span></div>`).join('')}
const WORKSPACE_KEY='signalpost-workspace-v1';const emptyWorkspace=()=>({schemaVersion:1,pins:[],history:[]});function loadWorkspace(){try{const value=JSON.parse(localStorage.getItem(WORKSPACE_KEY)||'null');if(!value)return emptyWorkspace();if(value.schemaVersion!==1||!Array.isArray(value.pins)||!Array.isArray(value.history))throw new Error('schema');return value}catch(_error){localStorage.removeItem(WORKSPACE_KEY);return {...emptyWorkspace(),warning:'Invalid saved workspace was reset safely.'}}}let workspace=loadWorkspace();function persistWorkspace(){localStorage.setItem(WORKSPACE_KEY,JSON.stringify(workspace));renderWorkspace()}function renderWorkspace(){const pinned=workspace.pins.map(org=>DATA.find(x=>x.org===org)).filter(Boolean);$('#workspace-summary').innerHTML=`<strong>Saved workspace</strong><p>${pinned.length} pinned · ${workspace.history.length} saved view(s)</p>${workspace.warning?`<p>${esc(workspace.warning)}</p>`:''}${pinned.slice(0,5).map(x=>`<button type="button" data-workspace-org="${x.org}">${esc(x.name)}</button>`).join('')}${workspace.history.length?`<p>Recent: ${esc(workspace.history.at(-1).label)}</p>`:''}`;$('#workspace-summary').querySelectorAll('[data-workspace-org]').forEach(b=>b.onclick=()=>{selected=DATA.find(x=>x.org===b.dataset.workspaceOrg);renderList();renderProfile()})}
const COMPARE_KEY='signalpost-compare-v1';const COMPARE_MAX=4;function loadCompare(){try{const value=JSON.parse(localStorage.getItem(COMPARE_KEY)||'[]');if(!Array.isArray(value))return[];return value.filter(org=>DATA.some(x=>x.org===org)).slice(0,COMPARE_MAX)}catch(_error){return[]}}let compareOrgs=loadCompare();function persistCompare(){localStorage.setItem(COMPARE_KEY,JSON.stringify(compareOrgs))}
let selected=null,agentMode='overview',agentQuestion='';function renderList(){const rows=filtered();$('#count').textContent=`${rows.length} of ${DATA.length} entities · ${workspace.pins.length} pinned · ${compareOrgs.length} selected to compare`;$('#list').innerHTML=rows.slice(0,500).map(x=>`<div class="row-wrap"><input class="compare-check" type="checkbox" data-compare-org="${x.org}" aria-label="Compare ${esc(x.name)}" ${compareOrgs.includes(x.org)?'checked':''} ${!compareOrgs.includes(x.org)&&compareOrgs.length>=COMPARE_MAX?'disabled':''}><button class="row ${selected?.org===x.org?'active':''}" data-org="${x.org}"><strong>${workspace.pins.includes(x.org)?'◆ ':''}${esc(x.name)}</strong><span>${esc(x.org)} · ${esc(x.form)} · ${esc(x.municipality||'Location not reported')}</span></button></div>`).join('')+(rows.length>500?'<div class="row-wrap"><div class="row"><span>Showing first 500 matches. Narrow the search.</span></div></div>':'');$('#list').querySelectorAll('[data-org]').forEach(b=>b.onclick=()=>{selected=DATA.find(x=>x.org===b.dataset.org);renderList();renderProfile();});$('#list').querySelectorAll('[data-compare-org]').forEach(cb=>cb.onchange=()=>{const org=cb.dataset.compareOrg;if(cb.checked){if(compareOrgs.length>=COMPARE_MAX){cb.checked=false;return}compareOrgs=[...compareOrgs,org]}else{compareOrgs=compareOrgs.filter(o=>o!==org)}persistCompare();syncCompareUI()});if(!selected&&rows.length){selected=rows[0];renderProfile();}}
function cite(url){return url?` <a href="${esc(url)}" target="_blank" rel="noreferrer">Source ↗</a>`:''}
function statusChip(label,status){const cls=status==='available'?'good':(status==='not_found'||status==='source_error')?'warn':'';return `<span class="chip ${cls}">${esc(label)}: ${esc(String(status||'not checked').replaceAll('_',' '))}</span>`}function sourceLine(obj){return obj?.source?`<details class="source"><summary>Inspect evidence</summary><div>Source: <a href="${esc(obj.source)}" target="_blank" rel="noreferrer">${esc(obj.source)}</a></div><div>Class: ${esc(obj.sourceClass||'not reported')} · Retrieved: ${esc(obj.retrievedAt||'not reported')}</div>${obj.effectiveAt?`<div>Effective: ${esc(obj.effectiveAt)}</div>`:''}${obj.hash?`<div>SHA-256: <code>${esc(obj.hash)}</code></div>`:''}</details>`:''}
function fmtValue(v){if(v==null)return 'Not reported';if(typeof v==='object'){if(v.fraDato||v.tilDato)return `${v.fraDato||''} – ${v.tilDato||''}`.trim();if(v.name&&v.address){const addr=v.address;const parts=[addr.kommune,addr.poststed,addr.postnummer].filter(Boolean).join(', ');return parts?`${v.name} — ${parts}`:v.name}return JSON.stringify(v)}return String(v)}
function groupSummaryText(x){const g=x.group,s=g.summary||{};if(g.status==='available'&&s.kind==='normalized'){const rels=s.relationships||[];const named=rels.slice(0,3).map(r=>`${esc(r.from_name||r.from_org||'?')} → ${esc(r.to_name||r.to_org||'?')}${r.relationship?` (${esc(r.relationship)})`:''}`).join('; ');return `Group structure returned: ${s.relationship_count||0} verified relationship(s) (both sides have a valid organisation number)${s.skipped_without_valid_org_numbers?`, ${s.skipped_without_valid_org_numbers} record(s) skipped without valid organisation numbers`:''}${named?`. ${named}`:''}.`}if(g.status==='available')return `Group structure returned (${s.keyCount||0} field(s), ${s.listItems||0} list item(s))${s.keys?.length?`: ${esc(s.keys.join(', '))}`:''}. Relations are not normalized yet, so no parent or subsidiary is named here.`;if(g.status==='not_found')return 'No group structure was returned for this company.';if(g.status==='not_applicable')return 'Group structure is not applicable for this company.';if(g.status==='source_error')return 'The group source errored; group membership was not established.';return 'Group structure was not checked in this run.'}
function jobsSummaryText(x){const j=x.jobs;if(j.status==='has')return `${j.exactCount} exact-entity job posting(s) (${j.totalCount} total supplied).`;if(j.status==='none')return 'No openings in the supplied observations. This is not proof the company has no vacancies.';return 'Jobs were not checked for this company in this batch.'}
function activityItems(x){return (x.external.items||[]).filter(i=>['public_post','public_mention','buzz_metrics','news','news_article','company_news','activity'].includes(i.signalType))}
function activitySummaryText(x){const items=activityItems(x),exact=items.filter(i=>i.exactEntity).length;if(!items.length)return 'No public-activity observations were supplied for this company.';const latest=items.map(i=>i.retrievedAt).filter(Boolean).sort().at(-1);return `${items.length} activity observation(s), ${exact} exact-entity${latest?`, latest retrieved ${esc(latest)}`:''}.`}
function compareCells(x){const rec=latestRecord(x),people=x.roles.items.filter(r=>!r.inactive),locations=x.locations.items,social=x.web.value.social_links||[],exactJobs=(x.external.items||[]).filter(i=>i.signalType==='job_posting'&&i.exactEntity);const cell=(html,status,source)=>({html,status,source});return [
{family:'Identity',label:'Name',...cell(`<strong>${esc(x.name)}</strong>`,x.registry.status,x.registrySource)},
{family:'Identity',label:'Organisation number',...cell(esc(x.org),x.registry.status,x.registrySource)},
{family:'Identity',label:'Legal form',...cell(esc(x.form||'Not reported'),x.registry.status,x.registrySource)},
{family:'Identity',label:'Municipality',...cell(esc(x.municipality||'Not reported'),x.registry.status,x.registrySource)},
{family:'Identity',label:'Industry',...cell(esc([x.industryCode,x.industry].filter(Boolean).join(' ')||'Not reported'),x.registry.status,x.registrySource)},
{family:'Identity',label:'Employees',...cell(x.employees==null?'Not reported':esc(x.employees),x.registry.status,x.registrySource)},
{family:'Identity',label:'Adverse flag',...cell(x.adverse?'Adverse registry state':'No adverse flag',x.registry.status,x.registrySource)},
{family:'Identity',label:'Latest accounts',...cell(esc(x.latestAccounts||'Not reported'),x.registry.status,x.registrySource)},
{family:'Financials',label:'Revenue',...cell(money(rec?.revenue,rec?.currency),x.financial.status,x.financial.source)},
{family:'Financials',label:'Operating result',...cell(money(rec?.operating_result,rec?.currency),x.financial.status,x.financial.source)},
{family:'Financials',label:'Annual result',...cell(money(rec?.annual_result,rec?.currency),x.financial.status,x.financial.source)},
{family:'Financials',label:'Assets',...cell(money(rec?.assets,rec?.currency),x.financial.status,x.financial.source)},
{family:'Financials',label:'Debt',...cell(money(rec?.debt,rec?.currency),x.financial.status,x.financial.source)},
{family:'Financials',label:'Period',...cell(rec?`${esc(rec.period?.fraDato||'')} – ${esc(rec.period?.tilDato||'')}`:'Not reported',x.financial.status,x.financial.source)},
{family:'People',label:'Role records',...cell(`${people.length} active role record(s)${people.length?'<br>'+people.slice(0,3).map(r=>`${esc(r.name||r.organisation_number||'Unnamed')} — ${esc(r.role||r.group||'role')}`).join('<br>'):''}`,x.roles.status,x.roles.source)},
{family:'Locations',label:'Subunits',...cell(`${locations.length} subunit(s)${locations.length?'<br>'+locations.slice(0,3).map(l=>esc(l.name)).join('<br>'):''}`,x.locations.status,x.locations.source)},
{family:'Group',label:'Group structure',...cell(groupSummaryText(x),x.group.status,x.group.source)},
{family:'Website',label:'Company site',...cell(`${esc(x.web.value.title||'No fetched homepage')}${x.web.value.quarantined_social_count?`<br>${esc(x.web.value.quarantined_social_count)} social link(s) quarantined`:''}`,x.web.status,x.web.source)},
{family:'Website',label:'Declared social handles',...cell(social.length?social.map(s=>esc(s.platform)).join(', '):'None declared',x.web.status,x.web.source)},
{family:'Jobs',label:'Hiring',...cell(`${jobsSummaryText(x)}${exactJobs.length?'<br>'+exactJobs.slice(0,3).map(j=>esc(j.title)).join('<br>'):''}`,x.jobsStatus==='has'?'available':x.jobsStatus,x.external.items.find(i=>i.signalType==='job_posting')?.url)},
{family:'Activity',label:'Public activity',...cell(activitySummaryText(x),activityItems(x).length?'available':'not_found',activityItems(x)[0]?.url)}]}
function renderCompare(){const panel=$('#compare-panel');const companies=compareOrgs.map(org=>DATA.find(x=>x.org===org)).filter(Boolean);if(companies.length===0){panel.hidden=true;panel.innerHTML='';return}panel.hidden=false;if(companies.length===1){panel.innerHTML=`<div class="eyebrow">Compare</div><h2>Compare companies</h2><p><strong>${esc(companies[0].name)}</strong> is selected. Tick one to three more companies (up to ${COMPARE_MAX}) to see a side-by-side table.</p><div class="compare-actions"><button type="button" id="compare-clear">Clear selection</button></div>`;$('#compare-clear').onclick=()=>{compareOrgs=[];persistCompare();syncCompareUI()};return}const cellsByCompany=companies.map(compareCells);const labels=cellsByCompany[0];let lastFamily='';const rowsHtml=labels.map((row,i)=>{const familyHead=row.family!==lastFamily?`<tr class="family-row"><th colspan="${companies.length+1}">${esc(row.family)}</th></tr>`:'';lastFamily=row.family;const tds=companies.map((_c,j)=>{const c=cellsByCompany[j][i];return `<td><div>${c.html}</div><div class="cell-meta">${statusChip('evidence',c.status)}${cite(c.source)}</div></td>`}).join('');return familyHead+`<tr><th scope="row">${esc(row.label)}</th>${tds}</tr>`}).join('');panel.innerHTML=`<div class="eyebrow">Compare · ${companies.length} of ${COMPARE_MAX}</div><h2>Side-by-side comparison</h2><p class="empty">Values show the latest record per company. Missing is not zero; each cell carries its evidence status and source.</p><div class="table-scroll"><table class="compare-table"><thead><tr><th scope="col">Field</th>${companies.map(c=>`<th scope="col"><strong>${esc(c.name)}</strong><br><small>${esc(c.org)}</small><br><button type="button" class="pin-button" data-remove-org="${c.org}">Remove</button></th>`).join('')}</tr></thead><tbody>${rowsHtml}</tbody></table></div><div class="compare-actions"><button type="button" id="compare-clear">Clear selection</button></div>`;panel.querySelectorAll('[data-remove-org]').forEach(b=>b.onclick=()=>{compareOrgs=compareOrgs.filter(o=>o!==b.dataset.removeOrg);persistCompare();syncCompareUI()});$('#compare-clear').onclick=()=>{compareOrgs=[];persistCompare();syncCompareUI()}}
function syncCompareUI(){document.querySelectorAll('[data-compare-org]').forEach(cb=>{cb.checked=compareOrgs.includes(cb.dataset.compareOrg);cb.disabled=!compareOrgs.includes(cb.dataset.compareOrg)&&compareOrgs.length>=COMPARE_MAX});const btn=$('#compare-company');if(btn&&selected){const inSet=compareOrgs.includes(selected.org);btn.textContent=inSet?'Remove from compare':'Add to compare';btn.disabled=!inSet&&compareOrgs.length>=COMPARE_MAX;btn.title=btn.disabled?`Compare holds at most ${COMPARE_MAX} companies`:''}$('#count').textContent=`${filtered().length} of ${DATA.length} entities · ${workspace.pins.length} pinned · ${compareOrgs.length} selected to compare`;renderCompare()}
const SIGNAL_LABELS={job_posting:'Job posting',public_post:'Public post',public_mention:'Public mention',review:'Review',review_summary:'Review summary',profile_handle:'Profile handle',profile_metrics:'Profile metrics',workforce_snapshot:'Workforce snapshot',company_profile:'Company profile',place_summary:'Place summary',buzz_metrics:'Buzz metrics'};
function renderExternalSection(x){const items=x.external.items||[];if(!items.length)return `<section id="external-signals"><div class="section-label">External signals</div><h3>No external observations supplied</h3><p class="empty">No external observations were supplied for this company in this batch. That does not prove the company has no public footprint.</p></section>`;const anyExperimental=items.some(i=>!i.publishable);const groups=[];for(const item of items){let g=groups.find(g=>g.type===item.signalType);if(!g){g={type:item.signalType,items:[]};groups.push(g)}g.items.push(item)}const groupsHtml=groups.map(g=>`<div class="obs-group"><h3 style="font-size:17px">${esc(SIGNAL_LABELS[g.type]||g.type||'Other signals')} (${g.items.length})</h3>${g.items.map(i=>`<div class="obs-item"><strong>${esc(i.title||'Observation')}</strong><p>${esc(i.text||'No evidence span supplied.')}</p><div class="obs-labels"><span class="chip">${esc(i.platform||'platform not reported')}</span><span class="chip">${esc(i.signalType||'signal not reported')}</span>${i.publishable?'<span class="chip good">approved rights · verified entity</span>':'<span class="chip experimental">experimental — not a verified fact</span>'}<span class="chip">rights: ${esc(i.rightsStatus||'not reported')}</span><span class="chip">exact entity: ${i.exactEntity?'verified':'not verified'}</span></div><small>${i.url?`<a href="${esc(i.url)}" target="_blank" rel="noreferrer">Source ↗</a> · `:''}Retrieved: ${esc(i.retrievedAt||'not reported')}${i.publishedAt?` · Published: ${esc(i.publishedAt)}`:''}${i.identityProof?`<br>Identity proof: ${esc(i.identityProof)}`:''}${i.hash?`<br>SHA-256: <code>${esc(i.hash)}</code>`:''}</small></div>`).join('')}</div>`).join('');return `<section id="external-signals"><div class="section-label">External signals</div><h3>${items.length} external observation(s) · ${x.external.summary.publishable} approved</h3>${anyExperimental?'<p class="experimental-note"><strong>Experimental observations are labelled, not facts.</strong> Items without approved rights or exact-entity verification are shown for inspection only and must not be read as verified claims about this company.</p>':''}<p class="source">Grouped by signal type and labelled by platform, rights status and exact-entity verification. Platforms: ${esc(x.external.summary.platforms.join(', ')||'none reported')}.</p>${groupsHtml}</section>`}
function fmtChangeValue(v){if(v===null||v===undefined)return 'not reported';if(typeof v==='object')return JSON.stringify(v);return String(v)}
function renderChangesSection(x){const changes=x.changes||[];if(!changes.length)return `<section id="change-timeline"><div class="section-label">Change timeline</div><h3>Baseline — no recorded changes</h3><p class="empty">Baseline — belum ada snapshot sebelumnya: no previous snapshot was supplied for this company, so no change can be shown. Retrieval timestamps and hashes in the evidence sections still show the evidence version.</p></section>`;const items=changes.map(c=>`<li class="change-item"><div><strong>${esc(c.field||'field not reported')}</strong>${c.retrievedAt?` <small>· ${esc(c.retrievedAt)}</small>`:''}</div><div class="change-values">${esc(fmtChangeValue(c.oldValue))} → <strong>${esc(fmtChangeValue(c.newValue))}</strong></div><small>${c.sourceUrl?`<a href="${esc(c.sourceUrl)}" target="_blank" rel="noreferrer">Source ↗</a> · `:''}${c.effectiveAt?`Effective: ${esc(c.effectiveAt)} · `:''}${c.status?`Status: ${esc(c.status)} · `:''}${c.oldHash?`<br>Old SHA-256: <code>${esc(c.oldHash)}</code>`:''}${c.newHash?`<br>New SHA-256: <code>${esc(c.newHash)}</code>`:''}</small></li>`).join('');return `<section id="change-timeline"><div class="section-label">Change timeline</div><h3>${changes.length} recorded change(s)</h3><ol class="timeline">${items}</ol></section>`}
function renderGroupSection(x){const heading=x.group.status==='available'?'Group structure returned':x.group.status==='not_found'?'No group structure returned':x.group.status==='not_applicable'?'Group structure not applicable':x.group.status==='source_error'?'Group source error':'Group structure not checked';return `<section><div class="section-label">Group structure</div><h3>${heading}</h3><p>${groupSummaryText(x)}</p>${sourceLine(x.group)}</section>`}
function renderProfile(){const x=selected;if(!x){$('#profile').innerHTML='<p class="empty">Choose a company.</p>';renderAgent();return}const rec=latestRecord(x),people=x.roles.items.filter(r=>!r.inactive),locations=x.locations.items,social=x.web.value.social_links||[],pdfs=x.financialHistory.pdfs||[],isPinned=workspace.pins.includes(x.org),inCompare=compareOrgs.includes(x.org),accounting=x.accounting.value||{};$('#profile').innerHTML=`<div class="profile-head"><div><div class="eyebrow">${esc(x.slice||'')} sample · ${esc(x.split||'')}</div><h2>${esc(x.name)}</h2><div>${esc(x.org)} · ${esc(x.form||'Form not reported')}</div>${x.contract?`<div class="source">Contract envelope: terminal status ${esc(x.contract.terminalStatus||'not reported')} · ${esc(x.contract.errorCount)} error(s) · ${esc(x.contract.claimCount)} claim(s)</div>`:''}</div><div><button class="pin-button" id="pin-company" type="button">${isPinned?'Unpin':'Pin company'}</button> <button class="pin-button" id="compare-company" type="button" ${!inCompare&&compareOrgs.length>=COMPARE_MAX?'disabled':''}>${inCompare?'Remove from compare':'Add to compare'}</button><div class="chips" style="margin-top:8px">${statusChip('accounts',x.financial.status)}${statusChip('roles',x.roles.status)}${statusChip('website',x.web.status)}${statusChip('jobs',x.jobsStatus)}${x.adverse?'<span class="chip warn">adverse registry state</span>':'<span class="chip good">no adverse flag</span>'}</div></div></div><div class="fact-grid"><div class="fact"><span>Industry</span><strong>${esc(x.industryCode||'—')} ${esc(x.industry||'Not reported')}</strong></div><div class="fact"><span>Municipality</span><strong>${esc(x.municipality||'Not reported')}</strong></div><div class="fact"><span>Employees</span><strong>${x.employees==null?'Not reported':esc(x.employees)}</strong></div><div class="fact"><span>Accounting availability</span><strong>${esc((accounting.classification||'not classified').replaceAll('_',' '))}</strong><small>${esc(accounting.reason||'')}</small></div></div><section><div class="section-label">Official annual account</div><h3>${rec?'Latest normalized figures':'No normalized financial record returned'}</h3>${rec?`<div class="table-scroll"><table><tr><th>Period</th><td>${esc(rec.period?.fraDato||'')} – ${esc(rec.period?.tilDato||'')}</td></tr><tr><th>Revenue</th><td>${money(rec.revenue,rec.currency)}</td></tr><tr><th>Operating result</th><td>${money(rec.operating_result,rec.currency)}</td></tr><tr><th>Annual result</th><td>${money(rec.annual_result,rec.currency)}</td></tr><tr><th>Assets</th><td>${money(rec.assets,rec.currency)}</td></tr><tr><th>Debt</th><td>${money(rec.debt,rec.currency)}</td></tr></table></div>`:`<p class="empty">${esc(accounting.reason||'Missing is not zero. Filing applicability is not established from the available source.')}</p>`}${pdfs.length?`<p class="source">Historical filings: ${pdfs.slice(0,8).map(p=>`<a href="${esc(p.url)}" target="_blank" rel="noreferrer">${esc(p.year)}</a>`).join(' · ')}</p>`:''}${sourceLine(x.accounting)}${sourceLine(x.financial)}</section><section><div class="section-label">Registered people</div><h3>${people.length} public role records</h3><div class="people">${people.length?people.slice(0,16).map(r=>`<div class="item"><strong>${esc(r.name||r.organisation_number||'Unnamed holder')}</strong><small>${esc(r.role||r.group||'Role not reported')}</small></div>`).join(''):'<p class="empty">No role record returned.</p>'}</div>${sourceLine(x.roles)}</section><section><div class="section-label">Registered operating locations</div><h3>${locations.length} subunits</h3><div class="locations">${locations.length?locations.slice(0,12).map(l=>`<div class="item"><strong>${esc(l.name)}</strong><small>${esc(l.address?.kommune||l.address?.poststed||'Location not reported')} · ${l.employees??'employees not reported'}</small></div>`).join(''):'<p class="empty">Location module not run or no subunits returned.</p>'}</div>${sourceLine(x.locations)}</section>${renderGroupSection(x)}${renderChangesSection(x)}${renderExternalSection(x)}<section><div class="section-label">Company-controlled web layer</div><h3>${esc(x.web.value.title||'No fetched homepage')}</h3><p>${esc(x.web.value.description||'No company-site description captured.')}</p>${x.web.value.quarantined_social_count?`<p class="experimental-note"><strong>Quarantined:</strong> ${esc(x.web.value.quarantined_social_count)} social link(s) were found on the registry-linked site but are not published, because the site could not be verified as this exact legal entity.</p>`:''}<p class="source">${(x.web.value.pages||[]).length} bounded same-site page(s) captured.</p><div class="social">${social.map(s=>`<div class="item"><strong>${esc(s.platform)}</strong><small><a href="${esc(s.url)}" target="_blank" rel="noreferrer">${esc(s.url)}</a></small></div>`).join('')}</div>${sourceLine(x.web)}</section>`;$('#pin-company').onclick=()=>{workspace.pins=isPinned?workspace.pins.filter(org=>org!==x.org):[...new Set([...workspace.pins,x.org])].sort();persistWorkspace();renderList();renderProfile()};$('#compare-company').onclick=()=>{if(compareOrgs.includes(x.org)){compareOrgs=compareOrgs.filter(o=>o!==x.org)}else{if(compareOrgs.length>=COMPARE_MAX)return;compareOrgs=[...compareOrgs,x.org]}persistCompare();syncCompareUI();renderProfile()};renderAgent();}
function requestedModes(question){const q=norm(question),m=[];if(/financial|finance|account|revenue|income|profit|result|debt|asset|regnskap/.test(q))m.push('financials');if(/lead|role|director|chair|board|ceo|leder|styre/.test(q))m.push('leaders');if(/location|where|address|shop|office|branch|place|sted|adresse|butikk|kontor/.test(q))m.push('locations');if(/social|linkedin|facebook|instagram|youtube|tiktok|profile|handle/.test(q))m.push('social');if(/job|hiring|hire|career|vacan|stilling|recruit/.test(q))m.push('hiring');if(/post|activity|buzz|engagement|reaction|comment|news|mention/.test(q))m.push('activity');if(/sentiment|reputation|opinion|positive|negative|omtale/.test(q))m.push('sentiment');if(/employee|company|overview|know|registered|industry|kommune|ansatt/.test(q)||!m.length)m.unshift('overview');return [...new Set(m)]}
function renderAgent(){const x=selected;if(!x){$('#agent-output').innerHTML='<p>Choose a company first.</p>';return}const modes=agentQuestion?requestedModes(agentQuestion):[agentMode],labels={overview:'Company record',financials:'Financials',leaders:'Leadership',locations:'Locations',social:'Social profiles',hiring:'Hiring',activity:'Public activity',sentiment:'Sentiment'};let body=modes.map(mode=>{const a=x.agentAnswers?.[mode];if(!a)return `<div class="answer-block"><small>${esc(labels[mode]||mode)}</small><p class="abstain"><strong>Not established:</strong> No precomputed answer is available for this mode.</p></div>`;const facts=(a.facts||[]).map(f=>`<p><strong>${esc(f.claim)}</strong>: ${esc(fmtValue(f.value))}${f.detail?`<br>${esc(f.detail)}`:''}${f.rights_status?`<br><small>Rights: ${esc(f.rights_status)}${f.acquisition_mode?` · ${esc(f.acquisition_mode)}`:''}</small>`:''}${cite(f.source_url)}${f.retrieved_at?`<br><small>Retrieved: ${esc(f.retrieved_at)}</small>`:''}</p>`).join('');const unsupported=(a.unsupported_or_uncertain||[]).map(u=>`<p class="abstain"><strong>Not established:</strong> ${esc(u)}</p>`).join('');return `<div class="answer-block"><small>${esc(labels[mode]||mode)}</small>${facts||'<p>No sourced facts were returned for this mode.</p>'}${unsupported}</div>`}).join('');if(agentQuestion)body=`<p class="agent-question"><small>Your question</small><br><strong>${esc(agentQuestion)}</strong></p>`+body;$('#agent-output').innerHTML=body;$('#agent-buttons').querySelectorAll('button').forEach(b=>b.classList.toggle('active',!agentQuestion&&b.dataset.mode===agentMode));}
function currentFilters(){return {query:$('#q').value,form:$('#form').value,state:$('#state').value,finance:$('#finance').value,slice:$('#slice').value,jobs:$('#jobs').value,group:$('#group').value,external:$('#external').value,sort:$('#sort').value}}
['q','form','state','finance','slice','jobs','group','external','sort'].forEach(id=>$('#'+id).addEventListener(id==='q'?'input':'change',()=>{selected=null;renderList()}));$('#save-view').onclick=()=>{const rows=filtered();workspace.history=[...workspace.history,{createdAt:new Date().toISOString(),label:$('#q').value||'Structured filter view',filters:currentFilters(),resultCount:rows.length,resultOrganisationNumbers:rows.map(x=>x.org)}].slice(-100);persistWorkspace()};$('#export-view').onclick=()=>{const rows=filtered();const artifact={exportedAt:new Date().toISOString(),batch:BATCH,filters:currentFilters(),compareOrganisationNumbers:[...compareOrgs],profiles:rows,claimBoundary:'Source-linked frozen Signalpost evidence; missing values are not zero.'};const blob=new Blob([JSON.stringify(artifact,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='signalpost-evidence-export.json';a.click();URL.revokeObjectURL(url)};$('#agent-form').onsubmit=e=>{e.preventDefault();agentQuestion=$('#agent-question').value.trim();if(agentQuestion)renderAgent()};$('#agent-buttons').querySelectorAll('button').forEach(b=>b.onclick=()=>{agentMode=b.dataset.mode;agentQuestion='';$('#agent-question').value='';renderAgent()});renderStats();renderWorkspace();renderList();syncCompareUI();
</script></body></html>"""


# ---------------------------------------------------------------------------
# Build + CLI
# ---------------------------------------------------------------------------

def build(
    rows: list[dict],
    score: dict | None = None,
    control_loop: dict | None = None,
    independent_score: dict | None = None,
    external_by_org: dict[str, list[dict]] | None = None,
    envelopes_by_org: dict[str, dict] | None = None,
    run_id: str | None = None,
    run_date: str | None = None,
) -> str:
    """Render the prototype HTML.

    ``score`` / ``control_loop`` / ``independent_score`` are accepted for
    backward compatibility and deliberately IGNORED: the prototype makes
    no competition-score claim and renders no scorecard.
    """
    external_by_org = external_by_org or {}
    envelopes_by_org = envelopes_by_org or {}
    answer_profile_fn = _load_answer_profile()
    compacted = [
        compact(
            row,
            external_by_org.get(str(row["organisation_number"]), []),
            envelopes_by_org.get(str(row["organisation_number"])),
            answer_profile_fn,
        )
        for row in rows
    ]
    payload = json.dumps(compacted, ensure_ascii=False).replace("</", "<\\/")
    meta = derive_run_meta(len(rows), envelopes_by_org, run_id, run_date)
    meta_payload = json.dumps(meta, ensure_ascii=False).replace("</", "<\\/")
    snapshot = html.escape(batch_snapshot_text(meta))
    return (
        HTML_TEMPLATE.replace("__DATA_JSON__", payload)
        .replace("__BATCH_META_JSON__", meta_payload)
        .replace("__SNAPSHOT_TEXT__", snapshot)
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build the Signalpost prototype (one static HTML file).")
    parser.add_argument("--input", required=True, help="Profiles JSONL (kit shape).")
    parser.add_argument("--score", help="IGNORED: accepted for compatibility; the prototype makes no score claim.")
    parser.add_argument("--control-loop", help="IGNORED: accepted for compatibility.")
    parser.add_argument("--independent-score", help="IGNORED: accepted for compatibility; the prototype makes no score claim.")
    parser.add_argument("--external-observations", nargs="*", default=[], help="Generic observations JSONL files (any platform).")
    parser.add_argument("--contract-envelopes", nargs="*", default=[], help="OUTPUT_CONTRACT.md envelopes JSONL (run metadata + changes[]).")
    parser.add_argument("--run-id", help="Override the header run id (must describe the actual batch).")
    parser.add_argument("--run-date", help="Override the header date, YYYY-MM-DD (must describe the actual batch).")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.score or args.control_loop or args.independent_score:
        print(
            "warning: --score/--control-loop/--independent-score are ignored; "
            "the prototype makes no competition-score claim.",
            file=sys.stderr,
        )
    rows = read_jsonl(Path(args.input))
    if args.limit:
        rows = rows[: args.limit]
    external_by_org: dict[str, list[dict]] = defaultdict(list)
    seen_observations: set[str] = set()
    for source in args.external_observations or []:
        for observation in read_jsonl(Path(source)):
            observation_id = str(observation.get("id") or json.dumps(observation, sort_keys=True, ensure_ascii=False))
            if observation_id in seen_observations:
                continue
            seen_observations.add(observation_id)
            organisation_number = str(observation.get("organisation_number") or "")
            if organisation_number:
                external_by_org[organisation_number].append(observation)
    envelopes_by_org: dict[str, dict] = {}
    for source in args.contract_envelopes or []:
        for envelope in read_jsonl(Path(source)):
            organisation_number = str(envelope.get("organisation_number") or "")
            if organisation_number:
                envelopes_by_org[organisation_number] = envelope
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(build(rows, None, None, None, external_by_org, envelopes_by_org, args.run_id, args.run_date), encoding="utf-8")
    print(f"Wrote {output} with {len(rows)} profiles")
