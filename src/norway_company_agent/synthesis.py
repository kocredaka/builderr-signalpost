"""Deterministic profile synthesis: templates and rules only, no LLM.

Three sections per profile:
1. what the company does (registered activity + industry codes + verified
   website description; explicit "not known" when the sources say nothing),
2. what changed (from a refresh diff; "baseline" when no previous snapshot
   was supplied),
3. what is not known (one entry per source family that did not return data,
   with its status and reason, plus checked-but-empty collections).

Every statement carries the evidence ids it rests on, except the baseline
marker, which describes the run itself rather than a sourced claim. Numbers
are copied verbatim from evidence values only; no ratio, estimate, or
speculative wording is ever generated.
"""

from __future__ import annotations

import json
from typing import Any


def _truncate(text: str, limit: int = 500) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def _render(value: Any, limit: int = 200) -> str:
    if value is None:
        return "not reported"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (dict, list)):
        return _truncate(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str), limit)
    return _truncate(str(value), limit)


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return len(value) > 0
    return True


def _as_text(value: Any) -> str:
    if isinstance(value, list):
        return "; ".join(str(item) for item in value if _present(item))
    return str(value or "")


def _industry_text(code: Any, label: Any) -> str | None:
    code, label = str(code or "").strip(), str(label or "").strip()
    if code and label:
        return f"{code} {label}"
    return code or label or None


def synthesize_profile(
    profile: dict[str, Any],
    changes: list[dict[str, Any]] | None,
    *,
    evidence_ids: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build the three synthesis sections for one profile.

    ``changes`` is None when no previous snapshot was supplied (baseline) and
    a (possibly empty) list when a previous snapshot was compared.
    ``evidence_ids`` maps evidence-module names to contract evidence ids;
    when omitted, module names are used as ids.
    """
    mapping = evidence_ids or {}
    records = profile.get("evidence", {}) or {}

    def eid(module: str) -> str:
        return mapping.get(module, module)

    def all_ids() -> list[str]:
        return [eid(module) for module in sorted(records)]

    what_it_does: list[dict[str, Any]] = []
    live = (records.get("registry_live", {}) or {}).get("value") or {}
    name = profile.get("name") or live.get("name")
    legal_form = profile.get("legal_form") or live.get("legal_form")
    municipality = profile.get("municipality") or ""
    org = profile.get("organisation_number") or ""
    if _present(name):
        parts = [f"{name} is registered"]
        if _present(legal_form):
            parts.append(f"as {legal_form}")
        if _present(org):
            parts.append(f"under organisation number {org}")
        if _present(municipality):
            parts.append(f"in {municipality}")
        what_it_does.append({
            "text": " ".join(parts) + ".",
            "evidence_ids": [eid("registry_live" if records.get("registry_live") else "registry")],
        })

    activity = profile.get("activity") if _present(profile.get("activity")) else live.get("activity")
    activity_module = "registry" if _present(profile.get("activity")) else "registry_live"
    if _present(activity):
        what_it_does.append({
            "text": f"Registered activity: {_truncate(_as_text(activity))}",
            "evidence_ids": [eid(activity_module)],
        })

    industries = []
    for code_key, label_key in (
        ("industry_code", "industry_label"),
        ("industry_code_2", "industry_label_2"),
        ("industry_code_3", "industry_label_3"),
    ):
        text = _industry_text(profile.get(code_key), profile.get(label_key))
        if text:
            industries.append(text)
    for key in ("industry", "industry_2", "industry_3"):
        item = live.get(key)
        if isinstance(item, dict):
            text = _industry_text(item.get("kode"), item.get("beskrivelse"))
            if text and text not in industries:
                industries.append(text)
    if industries:
        what_it_does.append({
            "text": "Industry codes: " + "; ".join(industries) + ".",
            "evidence_ids": [eid("registry")],
        })

    purpose = profile.get("statutory_purpose") if _present(profile.get("statutory_purpose")) else live.get("statutory_purpose")
    purpose_module = "registry" if _present(profile.get("statutory_purpose")) else "registry_live"
    if _present(purpose):
        what_it_does.append({
            "text": f"Statutory purpose: {_truncate(_as_text(purpose))}",
            "evidence_ids": [eid(purpose_module)],
        })

    employees = profile.get("employees")
    if employees is None:
        employees = live.get("employees")
    if employees is not None:
        what_it_does.append({
            "text": f"Registry employee count: {employees}.",
            "evidence_ids": [eid("registry")],
        })
    founded = profile.get("founded_date") or live.get("founded_date")
    if _present(founded):
        what_it_does.append({
            "text": f"Founded: {founded}.",
            "evidence_ids": [eid("registry")],
        })

    website_record = records.get("website", {}) or {}
    website_value = website_record.get("value") or {}
    publishable = (website_value.get("identity_assessment") or {}).get("publishable", False)
    description = website_value.get("description")
    if website_record.get("status") == "available" and publishable and _present(description):
        what_it_does.append({
            "text": f"The company website describes the company as: {_truncate(description)}",
            "evidence_ids": [eid("website")],
        })

    if not _present(activity) and not industries and not _present(purpose) and not (
        website_record.get("status") == "available" and publishable and _present(description)
    ):
        what_it_does.append({
            "text": "What the company does is not known from the checked sources.",
            "evidence_ids": all_ids(),
        })

    what_changed: list[dict[str, Any]] = []
    if changes is None:
        what_changed.append({
            "text": "Baseline snapshot: no previous snapshot was supplied, so no changes can be reported.",
            "evidence_ids": [],
        })
    elif not changes:
        what_changed.append({
            "text": "No tracked field changed since the previous snapshot.",
            "evidence_ids": all_ids(),
        })
    else:
        for change in changes:
            field = str(change.get("field") or "")
            module = field.split(".", 1)[0] or "registry"
            if module == "registry" and records.get("registry_live"):
                module = "registry_live"
            what_changed.append({
                "text": (
                    f"Field {field} changed from {_render(change.get('old_value'))} "
                    f"to {_render(change.get('new_value'))}."
                ),
                "evidence_ids": [eid(module)],
            })

    unknowns: list[dict[str, Any]] = []
    status_reasons = {
        "not_found": "checked source returned no record",
        "source_error": "source request failed",
        "blocked": "access blocked",
        "not_fetched": "source was not fetched in this run",
        "not_applicable": "not applicable to this company",
    }
    for module in sorted(records):
        record = records[module] or {}
        status = record.get("status")
        if status != "available":
            reason = status_reasons.get(str(status), f"status {status}")
            note = record.get("note")
            if status in {"source_error", "blocked"} and _present(note):
                reason = f"{reason}: {note}"
            unknowns.append({
                "family": module,
                "status": status,
                "reason": reason,
                "text": f"{module}: {reason}.",
                "evidence_ids": [eid(module)],
            })
    if website_record.get("status") == "available" and not publishable:
        unknowns.append({
            "family": "website",
            "status": "identity_not_established",
            "reason": "website fetched but exact legal-entity identity was not established; its claims are quarantined",
            "text": "website: fetched but exact legal-entity identity was not established; its claims are quarantined.",
            "evidence_ids": [eid("website")],
        })
    empty_collections = (
        ("roles", "roles", "roles", "no role records were returned"),
        ("locations", "locations", "locations", "no subunit records were returned"),
        ("financials", "records", "records", "no annual-account records were returned"),
        ("group", "relationships", "relationships", "no group relationships were returned"),
    )
    for module, key, _, message in empty_collections:
        record = records.get(module, {}) or {}
        value = record.get("value") or {}
        if record.get("status") == "available" and isinstance(value, dict) and value.get(key) == []:
            unknowns.append({
                "family": module,
                "status": "available",
                "reason": f"checked; zero items returned: {message}",
                "text": f"{module}: {message}.",
                "evidence_ids": [eid(module)],
            })
    if not unknowns:
        unknowns.append({
            "family": "all",
            "status": "available",
            "reason": "all checked source families returned data",
            "text": "All checked source families returned data.",
            "evidence_ids": all_ids(),
        })

    used_ids = sorted({
        evidence_id
        for section in (what_it_does, what_changed)
        for statement in section
        for evidence_id in statement["evidence_ids"]
    } | {
        evidence_id
        for entry in unknowns
        for evidence_id in entry["evidence_ids"]
    })
    return {
        "what_it_does": what_it_does,
        "what_changed": what_changed,
        "unknowns": unknowns,
        "evidence_ids": used_ids,
    }
