"""Adapter from internal profiles to the OUTPUT_CONTRACT.md envelope.

The adapter is generic over evidence modules: every record in
``profile["evidence"]`` becomes one flat evidence entry, and every field of
its normalized value becomes one claim. A future module needs no adapter
change to appear in the contract envelope.

Mapping rules (evaluation contract v2):
- internal ``not_found`` -> ``not_available``; ``source_error`` -> ``failed``;
  ``blocked`` -> ``blocked``; ``not_fetched`` -> ``failed`` (a terminal
  envelope must never carry an unresolved fetch); ``not_applicable`` stays.
- ``ambiguous`` comes from the website identity gate: when exact identity
  was not established, website content claims are ambiguous, never silently
  published as facts.
- Missing values become ``not_available`` claims with value None. 0 and
  False are real values and stay 0/False with availability ``available``.
- ``third_party_cost_usd`` is always 0: this submission uses only open
  registry data and public HTTP fetches, with no paid API.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .synthesis import synthesize_profile

ALLOWED_AVAILABILITY = {"available", "not_available", "blocked", "not_applicable", "ambiguous", "failed"}

STATUS_TO_AVAILABILITY = {
    "available": "available",
    "not_found": "not_available",
    "not_applicable": "not_applicable",
    "blocked": "blocked",
    "source_error": "failed",
    "not_fetched": "failed",
}

# Evidence payload duplicates, not claims: the raw body is already covered by
# the record's content_sha256 and its normalized fields carry the claims.
SKIP_CLAIM_KEYS = {"raw_body"}

THIRD_PARTY_COST_USD = 0


def map_availability(status: Any) -> str:
    return STATUS_TO_AVAILABILITY.get(str(status), "failed")


def _is_missing(value: Any) -> bool:
    return value is None or value == ""


def _claim_span(module: str, record: dict[str, Any]) -> str:
    """Short human-readable span identifying what the evidence record covers."""
    value = record.get("value")
    if module == "registry" and isinstance(value, dict):
        name = value.get("navn") or value.get("Navn") or ""
        org = value.get("organisasjonsnummer") or value.get("Organisasjonsnummer") or ""
        if name or org:
            return f"{name} ({org})".strip()
    if module == "website" and isinstance(value, dict):
        parts = [str(value.get("title") or ""), str(value.get("description") or "")]
        span = " ".join(part for part in parts if part).strip()
        if span:
            return " ".join(span.split())[:200]
        if value.get("final_url"):
            return str(value["final_url"])
    if isinstance(value, dict):
        scalars = []
        for key in sorted(value):
            item = value[key]
            if isinstance(item, (str, int, float, bool)) and item not in (None, ""):
                scalars.append(f"{key}={item}")
            if len(scalars) >= 3:
                break
        if scalars:
            return "; ".join(scalars)[:200]
    if isinstance(value, list):
        return f"{len(value)} item(s)"
    note = record.get("note")
    if note:
        return str(note)[:200]
    return f"{module} evidence ({record.get('status')})"


def build_evidence_list(
    profile: dict[str, Any], modules: list[str] | None = None
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Flat contract evidence list + module->id map, in deterministic order."""
    records = profile.get("evidence", {}) or {}
    names = [name for name in (modules or sorted(records)) if name in records]
    if modules is None:
        names = sorted(records)
    evidence_list: list[dict[str, Any]] = []
    id_by_module: dict[str, str] = {}
    for index, module in enumerate(names, 1):
        record = records[module] or {}
        evidence_id = f"ev-{index}"
        id_by_module[module] = evidence_id
        evidence_list.append({
            "id": evidence_id,
            "source_url": record.get("source_url"),
            "source_class": record.get("source_class") or record.get("source_type") or module,
            "retrieved_at": record.get("retrieved_at"),
            "content_sha256": record.get("content_sha256"),
            "claim_span": _claim_span(module, record),
        })
    return evidence_list, id_by_module


def _confidence_for(module: str, record: dict[str, Any]) -> float:
    if module == "website":
        assessment = ((record.get("value") or {}).get("identity_assessment") or {})
        score = assessment.get("score")
        # No gate assessment at all means identity was never verified: stay
        # conservative rather than implying a verified 0.9.
        return float(score) if isinstance(score, (int, float)) else 0.3
    if module == "accounting_obligation":
        return 0.9
    source_class = str(record.get("source_class") or record.get("source_type") or "")
    return 0.99 if source_class.startswith("official") else 0.9


def _claims_for_value(
    field: str,
    value: Any,
    *,
    evidence_id: str,
    availability: str,
    confidence: float,
) -> list[dict[str, Any]]:
    """One claim per field; lists of records expand to one claim per record."""
    if _is_missing(value):
        return [{
            "field": field,
            "value": None,
            "availability": "not_available",
            "confidence": 0.9,
            "evidence_ids": [evidence_id],
        }]
    if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
        return [{
            "field": f"{field}[{index}]",
            "value": item,
            "availability": availability,
            "confidence": confidence,
            "evidence_ids": [evidence_id],
        } for index, item in enumerate(value)]
    return [{
        "field": field,
        "value": value,
        "availability": availability,
        "confidence": confidence,
        "evidence_ids": [evidence_id],
    }]


def _claims_for_module(module: str, record: dict[str, Any], evidence_id: str) -> list[dict[str, Any]]:
    status = record.get("status")
    availability = map_availability(status)
    if status != "available":
        confidence = 0.9 if availability in {"not_available", "not_applicable"} else 0.0
        return [{
            "field": module,
            "value": None,
            "availability": availability,
            "confidence": confidence,
            "evidence_ids": [evidence_id],
        }]
    value = record.get("value")
    confidence = _confidence_for(module, record)
    ambiguous = False
    if module == "website":
        assessment = (value or {}).get("identity_assessment") or {}
        # Only an explicit publishable gate verdict makes website content
        # available; a missing assessment means identity was never verified.
        ambiguous = assessment.get("publishable") is not True
    if isinstance(value, dict):
        claims: list[dict[str, Any]] = []
        for key in sorted(value):
            if key in SKIP_CLAIM_KEYS:
                continue
            field_availability = availability
            if ambiguous and key != "identity_assessment":
                field_availability = "ambiguous"
            claims.extend(_claims_for_value(
                f"{module}.{key}", value[key],
                evidence_id=evidence_id,
                availability=field_availability,
                confidence=confidence,
            ))
        return claims
    if isinstance(value, list):
        return _claims_for_value(module, value, evidence_id=evidence_id, availability=availability, confidence=confidence)
    if _is_missing(value):
        return [{
            "field": module,
            "value": None,
            "availability": "not_available",
            "confidence": 0.9,
            "evidence_ids": [evidence_id],
        }]
    return [{
        "field": module,
        "value": value,
        "availability": availability,
        "confidence": confidence,
        "evidence_ids": [evidence_id],
    }]


def build_errors(profile: dict[str, Any], modules: list[str] | None = None) -> list[dict[str, Any]]:
    records = profile.get("evidence", {}) or {}
    names = modules if modules is not None else sorted(records)
    errors = []
    for module in names:
        record = records.get(module) or {}
        if record.get("status") in {"source_error", "blocked", "not_fetched"}:
            errors.append({
                "module": module,
                "status": record.get("status"),
                "message": record.get("note") or str(record.get("status")),
                "source_url": record.get("source_url"),
            })
    return errors


def _runtime_ms(started_at: str | None, completed_at: str | None, profile: dict[str, Any]) -> int:
    try:
        start = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(completed_at).replace("Z", "+00:00"))
        return max(0, int((end - start).total_seconds() * 1000))
    except (TypeError, ValueError):
        latencies = (profile.get("run_metrics", {}) or {}).get("latencies_ms") or []
        return int(sum(latencies))


def contract_envelope(
    profile: dict[str, Any],
    *,
    run_id: str,
    started_at: str,
    completed_at: str,
    modules: list[str] | None = None,
    changes: list[dict[str, Any]] | None = None,
    include_synthesis: bool = True,
    terminal_state: str | None = None,
) -> dict[str, Any]:
    """Build one OUTPUT_CONTRACT.md envelope for one organisation number.

    ``changes`` is None when no previous snapshot was compared (the synthesis
    then reports a baseline); pass a list (possibly empty) when a refresh
    comparison ran.
    """
    evidence_list, id_by_module = build_evidence_list(profile, modules)
    claims: list[dict[str, Any]] = []
    records = profile.get("evidence", {}) or {}
    for module in (modules if modules is not None else sorted(records)):
        if module not in id_by_module:
            continue
        claims.extend(_claims_for_module(module, records[module] or {}, id_by_module[module]))
    if include_synthesis and id_by_module:
        synthesis = synthesize_profile(profile, changes, evidence_ids=id_by_module)
        if synthesis["evidence_ids"]:
            claims.append({
                "field": "profile_synthesis",
                "value": synthesis,
                "availability": "available",
                "confidence": 0.99,
                "evidence_ids": synthesis["evidence_ids"],
            })
    if terminal_state is not None:
        terminal_status = "completed" if terminal_state == "complete" else "failed"
    else:
        unresolved = any(
            (records.get(module) or {}).get("status") in {None, "not_fetched"}
            for module in id_by_module
        )
        terminal_status = "failed" if unresolved else "completed"
    metrics = profile.get("run_metrics", {}) or {}
    return {
        "organisation_number": profile.get("organisation_number"),
        "run": {
            "run_id": run_id,
            "started_at": started_at,
            "completed_at": completed_at,
            "terminal_status": terminal_status,
        },
        "claims": claims,
        "evidence": evidence_list,
        "changes": list(changes) if changes else [],
        "errors": build_errors(profile, modules),
        "operations": {
            "requests": int(metrics.get("requests") or len(evidence_list)),
            "runtime_ms": _runtime_ms(started_at, completed_at, profile),
            "third_party_cost_usd": THIRD_PARTY_COST_USD,
        },
    }


def validate_contract_envelope(envelope: dict[str, Any]) -> list[str]:
    """Round-trip checks: every claim points at real evidence, no orphans."""
    problems: list[str] = []
    evidence_ids = {item.get("id") for item in envelope.get("evidence", [])}
    for claim in envelope.get("claims", []):
        if claim.get("availability") not in ALLOWED_AVAILABILITY:
            problems.append(f"claim {claim.get('field')}: bad availability {claim.get('availability')!r}")
        confidence = claim.get("confidence")
        if not isinstance(confidence, (int, float)) or not 0.0 <= confidence <= 1.0:
            problems.append(f"claim {claim.get('field')}: bad confidence {confidence!r}")
        ids = claim.get("evidence_ids") or []
        if not ids:
            problems.append(f"claim {claim.get('field')}: no evidence")
        for evidence_id in ids:
            if evidence_id not in evidence_ids:
                problems.append(f"claim {claim.get('field')}: unknown evidence {evidence_id!r}")
        if claim.get("availability") == "available" and claim.get("value") is None:
            problems.append(f"claim {claim.get('field')}: available with null value")
    operations = envelope.get("operations", {})
    if operations.get("third_party_cost_usd") != 0:
        problems.append("operations.third_party_cost_usd must be 0 for this submission")
    if not envelope.get("organisation_number"):
        problems.append("missing organisation_number")
    if not isinstance(envelope.get("changes"), list) or not isinstance(envelope.get("errors"), list):
        problems.append("changes/errors must be lists")
    return problems
