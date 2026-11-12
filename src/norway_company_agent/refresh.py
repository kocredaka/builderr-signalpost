from __future__ import annotations

from typing import Any


TRACKED_FIELDS: dict[str, tuple[str, ...]] = {
    "registry.name": ("name",),
    "registry.legal_form": ("legal_form",),
    "registry.legal_form_description": ("legal_form_description",),
    "registry.employees": ("employees",),
    "registry.municipality": ("municipality",),
    "registry.business_address": ("business_address",),
    "registry.postal_address": ("postal_address",),
    "registry.website": ("website",),
    "registry.email": ("email",),
    "registry.phone": ("phone",),
    "registry.mobile": ("mobile",),
    "registry.activity": ("activity",),
    "registry.statutory_purpose": ("statutory_purpose",),
    "registry.founded_date": ("founded_date",),
    "registry.registration_date": ("registration_date",),
    "registry.vat_registered": ("vat_registered",),
    "registry.capital": ("capital",),
    "registry.bankrupt": ("bankrupt",),
    "registry.bankruptcy_date": ("bankruptcy_date",),
    "registry.liquidating": ("liquidating",),
    "registry.liquidation_date": ("liquidation_date",),
    "registry.industry_code_2": ("industry_code_2",),
    "registry.industry_code_3": ("industry_code_3",),
    "registry.parent_unit": ("parent_unit",),
    "registry.in_group": ("in_group",),
    "registry.annotations": ("annotations",),
    "registry.latest_submitted_accounts": ("latest_submitted_accounts",),
    "financials.records": ("evidence", "financials", "value", "records"),
    "financial_history.years": ("evidence", "financial_history", "value", "years"),
    "roles.roles": ("evidence", "roles", "value", "roles"),
    "group.relationships": ("evidence", "group", "value", "relationships"),
    "locations.locations": ("evidence", "locations", "value", "locations"),
    "website.title": ("evidence", "website", "value", "title"),
    "website.description": ("evidence", "website", "value", "description"),
    "website.social_links": ("evidence", "website", "value", "social_links"),
    "website.content_sha256": ("evidence", "website", "value", "content_sha256"),
}


def _read(value: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def _same(old: Any, new: Any) -> bool:
    """Absence-aware equality for refresh diffs.

    None, "" and an empty dict all mean "no value reported" and are equal to
    each other (so profiles produced before a normalizer expansion do not
    generate spurious changes for newly tracked keys). An empty LIST is not
    absence: it means "checked, zero items", and differs from None. 0 and
    False are real values and differ from absence.
    """
    if old == new:
        return True
    if isinstance(old, dict) and isinstance(new, dict):
        keys = set(old) | set(new)
        return all(_same(old.get(key), new.get(key)) for key in keys)
    if isinstance(old, list) and isinstance(new, list):
        return len(old) == len(new) and all(_same(a, b) for a, b in zip(old, new))
    if old is None:
        return new == "" or (isinstance(new, dict) and not new)
    if new is None:
        return old == "" or (isinstance(old, dict) and not old)
    return False


def _evidence_for(profile: dict[str, Any], field: str) -> dict[str, Any]:
    module = field.split(".", 1)[0]
    records = profile.get("evidence", {})
    if module == "registry":
        return records.get("registry_live") or records.get("registry", {})
    return records.get(module, {})


def diff_profile(previous: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    old_org = previous.get("organisation_number")
    new_org = current.get("organisation_number")
    if not old_org or old_org != new_org:
        raise ValueError("Refresh comparison requires the same exact organisation number")
    changes = []
    for field, path in TRACKED_FIELDS.items():
        old_value = _read(previous, path)
        new_value = _read(current, path)
        if _same(old_value, new_value):
            continue
        record = _evidence_for(current, field)
        previous_record = _evidence_for(previous, field)
        changes.append({
            "organisation_number": new_org,
            "field": field,
            "old_value": old_value,
            "new_value": new_value,
            "source_url": record.get("source_url"),
            "retrieved_at": record.get("retrieved_at"),
            "effective_at": record.get("effective_at") or record.get("as_of"),
            "source_class": record.get("source_class") or record.get("source_type"),
            "old_content_sha256": previous_record.get("content_sha256"),
            "new_content_sha256": record.get("content_sha256"),
            "status": record.get("status"),
        })
    return changes


def diff_datasets(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> list[dict[str, Any]]:
    old_by_org = {row["organisation_number"]: row for row in previous}
    new_by_org = {row["organisation_number"]: row for row in current}
    if set(old_by_org) != set(new_by_org):
        raise ValueError("Refresh datasets must have identical organisation-number membership")
    return [
        change
        for org in sorted(old_by_org)
        for change in diff_profile(old_by_org[org], new_by_org[org])
    ]
