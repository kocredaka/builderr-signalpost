from __future__ import annotations

import hashlib
import threading
import time
import urllib.parse
from typing import Any, Callable

from .evidence import evidence
from .http import FetchResult, fetch_json

ACCOUNTING_OBLIGATION_SOURCE = "https://www.brreg.no/en/submission-of-annual-accounts/reporting-obligations-to-the-register-of-company-accounts/who-has-an-accounting-obligation/"
ACCOUNTING_RULESET_VERSION = "brreg_accounting_obligation_rules_2024-08-09_v1"
ALWAYS_ACCOUNTING_OBLIGED_FORMS = {"AS", "ASA", "BRL", "BBL", "STI", "SF", "VPFO"}
THRESHOLD_OR_ACTIVITY_FORMS = {"ENK", "ANS", "DA", "SA", "FLI", "ESEK", "NUF", "UTLA", "ORGL", "SAM", "SPA", "KS", "BO"}

BRREG_ENTITY = "https://data.brreg.no/enhetsregisteret/api/enheter/{org}"
BRREG_ROLES = BRREG_ENTITY + "/roller"
BRREG_GROUP = "https://data.brreg.no/enhetsregisteret/api/konsernstruktur/{org}"
BRREG_SUBUNITS = "https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={org}&size=1000"
BRREG_ACCOUNTS = "https://data.brreg.no/regnskapsregisteret/regnskap/{org}"
BRREG_ACCOUNT_YEARS = "https://data.brreg.no/regnskapsregisteret/regnskap/aarsregnskap/kopi/{org}/aar"
BRREG_ACCOUNT_PDF = "https://data.brreg.no/regnskapsregisteret/regnskap/aarsregnskap/kopi/{org}/{year}"

_history_lock = threading.Lock()
_history_last_request = 0.0


def accounting_obligation_assessment(profile: dict[str, Any]) -> dict[str, Any]:
    """Classify the rule path without inventing a definitive exemption.

    Brreg's public rule is categorical for some forms and threshold/activity dependent for others.
    Registry employee counts are not treated as equivalent to statutory man-years or asset/revenue tests.
    """
    legal_form = str(profile.get("legal_form") or "").upper()
    latest = profile.get("latest_submitted_accounts")
    if latest:
        classification = "filing_observed"
        reason = "The registry snapshot reports a latest submitted annual-account year."
    elif legal_form in ALWAYS_ACCOUNTING_OBLIGED_FORMS:
        classification = "required_by_legal_form"
        reason = f"Brreg lists organisation form {legal_form} in an always-obliged category."
    elif legal_form in THRESHOLD_OR_ACTIVITY_FORMS:
        classification = "threshold_or_activity_dependent"
        reason = "Obligation depends on statutory size, partner, activity, tax or supervision tests not fully present in the open entity row."
    else:
        classification = "special_rule_or_review_required"
        reason = "The open entity row is insufficient for a definitive accounting-obligation decision."
    ruleset_hash = __import__("hashlib").sha256(ACCOUNTING_RULESET_VERSION.encode()).hexdigest()
    return evidence(
        "accounting_obligation",
        "available",
        "official_rule_interpretation",
        ACCOUNTING_OBLIGATION_SOURCE,
        value={
            "classification": classification,
            "legal_form": legal_form or None,
            "latest_submitted_accounts": latest or None,
            "reason": reason,
            "ruleset_version": ACCOUNTING_RULESET_VERSION,
        },
        as_of="2024-08-09",
        content_sha256=ruleset_hash,
        source_row_key=str(profile.get("organisation_number") or "") or None,
    )


def _get(value: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _pick(body: Any, *names: str) -> Any:
    """First non-None value under any of the given keys (False/0 are real values)."""
    if not isinstance(body, dict):
        return None
    for name in names:
        value = body.get(name)
        if value is not None:
            return value
    return None


def _strip_links(value: Any) -> Any:
    """Minimal raw-body copy without HAL navigation links."""
    if isinstance(value, dict):
        return {key: _strip_links(child) for key, child in value.items() if key != "_links"}
    if isinstance(value, list):
        return [_strip_links(child) for child in value]
    return value


def normalize_financials(body: Any) -> dict[str, Any]:
    records = body if isinstance(body, list) else []
    normalized = []
    for item in records[:3]:
        if not isinstance(item, dict):
            continue
        income = _get(item, "resultatregnskapResultat", "driftsresultat", "driftsinntekter") or {}
        costs = _get(item, "resultatregnskapResultat", "driftsresultat", "driftskostnad") or {}
        finance = _get(item, "resultatregnskapResultat", "finansresultat") or {}
        equity = _get(item, "egenkapitalGjeld", "egenkapital") or {}
        contributed = _get(equity, "innskuttEgenkapital") or {}
        debt_overview = _get(item, "egenkapitalGjeld", "gjeldOversikt") or {}
        assets = _get(item, "eiendeler") or {}
        normalized.append({
            "record_id": item.get("id"),
            "journal_number": item.get("journalnr"),
            "account_type": item.get("regnskapstype"),
            "organisation_number": _get(item, "virksomhet", "organisasjonsnummer"),
            "organisation_form": _get(item, "virksomhet", "organisasjonsform"),
            "parent_company": _get(item, "virksomhet", "morselskap"),
            "period": item.get("regnskapsperiode"),
            "currency": item.get("valuta"),
            "winding_up_accounts": item.get("avviklingsregnskap"),
            "presentation_plan": item.get("oppstillingsplan"),
            "audit": item.get("revisjon"),
            "accounting_principles": _pick(item, "regnkapsprinsipper", "regnskapsprinsipper"),
            "revenue": _get(item, "resultatregnskapResultat", "driftsresultat", "driftsinntekter", "sumDriftsinntekter"),
            "sales_revenue": income.get("salgsinntekter"),
            "total_operating_cost": costs.get("sumDriftskostnad"),
            "operating_result": _get(item, "resultatregnskapResultat", "driftsresultat", "driftsresultat"),
            "finance_income": _get(finance, "finansinntekt", "sumFinansinntekter"),
            "finance_cost": _get(finance, "finanskostnad", "sumFinanskostnad"),
            "net_finance": finance.get("nettoFinans"),
            "profit_before_tax": _get(item, "resultatregnskapResultat", "ordinaertResultatFoerSkattekostnad"),
            "tax_cost": _get(item, "resultatregnskapResultat", "ordinaertResultatSkattekostnad"),
            "annual_result": _get(item, "resultatregnskapResultat", "aarsresultat"),
            "assets": _get(item, "eiendeler", "sumEiendeler"),
            "current_assets": _get(assets, "omloepsmidler", "sumOmloepsmidler"),
            "fixed_assets": _get(assets, "anleggsmidler", "sumAnleggsmidler"),
            "inventory": assets.get("sumVarer"),
            "receivables": assets.get("sumFordringer"),
            "investments": assets.get("sumInvesteringer"),
            "cash": assets.get("sumBankinnskuddOgKontanter"),
            "equity": _get(item, "egenkapitalGjeld", "egenkapital", "sumEgenkapital"),
            "contributed_equity": _pick(contributed, "sumInnskuttEgenkaptial", "sumInnskuttEgenkapital"),
            "retained_equity": equity.get("opptjentEgenkapital"),
            "total_equity_and_debt": _get(item, "egenkapitalGjeld", "sumEgenkapitalGjeld"),
            "debt": _get(item, "egenkapitalGjeld", "gjeldOversikt", "sumGjeld"),
            "current_debt": _get(debt_overview, "kortsiktigGjeld", "sumKortsiktigGjeld"),
            "long_term_debt": _get(debt_overview, "langsiktigGjeld", "sumLangsiktigGjeld"),
        })
    # NOTE: salary/depreciation breakdowns were NOT present in the verified live
    # response (driftskostnad carried only sumDriftskostnad); no paths are guessed.
    # The untouched source records are preserved alongside the mapped ones so no
    # reported figure is ever silently dropped. Change tracking compares only
    # the mapped "records" list, so raw-only churn (e.g. journal numbers) is not
    # reported as a financial change.
    return {
        "records": normalized,
        "source_records": [item for item in records[:3] if isinstance(item, dict)],
        "total_records": len(records),
    }


def normalize_financial_history(body: Any, org: str) -> dict[str, Any]:
    years = sorted({str(year) for year in body if str(year).isdigit()}) if isinstance(body, list) else []
    return {
        "years": years,
        "pdfs": [
            {"year": year, "url": BRREG_ACCOUNT_PDF.format(org=org, year=year)}
            for year in reversed(years)
        ],
    }


def _reserve_history_slot(clock: Callable[[], float] = time.monotonic, sleeper: Callable[[float], None] = time.sleep) -> None:
    """Reserve starts at least 2.1 seconds apart without serializing response time."""
    global _history_last_request
    with _history_lock:
        delay = 2.1 - (clock() - _history_last_request)
        if delay > 0:
            sleeper(delay)
        _history_last_request = clock()


def _fetch_history(url: str) -> FetchResult:
    """Keep this endpoint below its observed 30-request-starts/minute allowance."""
    _reserve_history_slot()
    return fetch_json(url)


def normalize_roles(body: Any) -> dict[str, Any]:
    roles = []
    for group in body.get("rollegrupper", []) if isinstance(body, dict) else []:
        changed = group.get("sistEndret")
        for item in group.get("roller", []):
            person = item.get("person") or {}
            name = person.get("navn") or {}
            entity = item.get("enhet") or {}
            display_name = " ".join(filter(None, [name.get("fornavn"), name.get("mellomnavn"), name.get("etternavn")])) or entity.get("navn")
            roles.append({
                "name": display_name or None,
                "organisation_number": entity.get("organisasjonsnummer"),
                "role_code": _get(item, "type", "kode"),
                "role": _get(item, "type", "beskrivelse"),
                "group_code": _get(group, "type", "kode"),
                "group": _get(group, "type", "beskrivelse"),
                "last_changed": changed,
                "inactive": bool(item.get("avregistrert")),
            })
    return {"roles": roles}


def normalize_locations(body: Any, pagination: dict[str, Any] | None = None) -> dict[str, Any]:
    rows = _get(body, "_embedded", "underenheter") or []
    locations = []
    for item in rows:
        business_address = item.get("beliggenhetsadresse")
        postal_address = item.get("postadresse")
        locations.append({
            "organisation_number": item.get("organisasjonsnummer"),
            "name": item.get("navn"),
            "business_address": business_address,
            "postal_address": postal_address,
            "address": business_address or postal_address,
            "industry": item.get("naeringskode1"),
            "employees": item.get("antallAnsatte"),
            "phone": item.get("telefon"),
            "parent_unit": item.get("overordnetEnhet"),
            "auxiliary_unit": item.get("hjelpeenhetskode"),
        })
    page = body.get("page") if isinstance(body, dict) else None
    total = page.get("totalElements") if isinstance(page, dict) else None
    pagination = pagination or {}
    fetched = len(locations)
    return {
        "locations": locations,
        "total_elements": total,
        "fetched": fetched,
        "pages_fetched": pagination.get("pages_fetched", 1),
        "truncated": bool(pagination.get("truncated", total is not None and fetched < total)),
        "excluded_mismatched": pagination.get("excluded_mismatched", 0),
        "pagination_note": pagination.get("note"),
    }


def _valid_org_number(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 9 and value.isdigit()


def normalize_group(body: Any, org: str | None = None) -> dict[str, Any]:
    """Normalize konsernstruktur into parent/subsidiary relationships.

    Only relationships with a valid 9-digit organisation number on BOTH sides
    are published; anything else is counted as skipped, never claimed.
    """
    if isinstance(body, list):
        root_org, root_name, children = org, None, body
        root_form = None
    elif isinstance(body, dict):
        root_org, root_name, children = body.get("organisasjonsnummer"), body.get("navn"), body.get("children") or []
        root_form = _get(body, "organisasjonsform", "kode")
    else:
        return {
            "organisation_number": org,
            "name": None,
            "legal_form": None,
            "relationships": [],
            "relationship_count": 0,
            "children_returned": 0,
            "skipped_without_valid_org_numbers": 0,
        }
    relationships: list[dict[str, Any]] = []
    skipped = 0

    def walk(nodes: Any, default_parent_org: Any, default_parent_name: Any) -> None:
        nonlocal skipped
        for child in nodes if isinstance(nodes, list) else []:
            if not isinstance(child, dict):
                skipped += 1
                continue
            child_org = child.get("organisasjonsnummer")
            parent_org = child.get("parentOrganisasjonsnummer") or default_parent_org
            parent_name = child.get("parentNavn") or default_parent_name
            if _valid_org_number(child_org) and _valid_org_number(parent_org):
                relationships.append({
                    "from_organisation_number": parent_org,
                    "from_name": parent_name,
                    "to_organisation_number": child_org,
                    "to_name": child.get("navn"),
                    "to_legal_form": _get(child, "organisasjonsform", "kode"),
                    "relationship": _get(child, "knytningsform", "beskrivelse"),
                    "relationship_code": _get(child, "knytningsform", "kode"),
                    "basis": child.get("grunnlag"),
                    "date": child.get("dato"),
                    "level": child.get("nivaa"),
                })
            else:
                skipped += 1
            walk(child.get("children"), child_org, child.get("navn"))

    walk(children, root_org, root_name)
    return {
        "organisation_number": root_org,
        "name": root_name,
        "legal_form": root_form,
        "relationships": relationships,
        "relationship_count": len(relationships),
        "children_returned": len(children) if isinstance(children, list) else 0,
        "skipped_without_valid_org_numbers": skipped,
    }


def normalize_entity(body: Any) -> dict[str, Any]:
    body = body if isinstance(body, dict) else {}
    return {
        "organisation_number": body.get("organisasjonsnummer"),
        "name": body.get("navn"),
        "legal_form": _get(body, "organisasjonsform", "kode"),
        "legal_form_description": _get(body, "organisasjonsform", "beskrivelse"),
        "employees": body.get("antallAnsatte"),
        "has_registered_employees": body.get("harRegistrertAntallAnsatte"),
        "employee_count_registered_date": body.get("registreringsdatoAntallAnsatteEnhetsregisteret"),
        "employee_count_aaregister_date": _pick(body, "registreringsdatoAntallAnsatteNAVAaregisteret", "registreringsdatoantallansatteNAVAaregisteret"),
        "bankrupt": body.get("konkurs"),
        "bankruptcy_date": body.get("konkursdato"),
        "liquidating": body.get("underAvvikling"),
        "liquidation_date": body.get("underAvviklingDato"),
        "forced_liquidation": body.get("underTvangsavviklingEllerTvangsopplosning"),
        "website": body.get("hjemmeside"),
        "email": body.get("epostadresse"),
        "phone": body.get("telefon"),
        "mobile": body.get("mobil"),
        "industry": body.get("naeringskode1"),
        "industry_2": body.get("naeringskode2"),
        "industry_3": body.get("naeringskode3"),
        "sector": body.get("institusjonellSektorkode"),
        "auxiliary_unit": body.get("hjelpeenhetskode"),
        "business_address": body.get("forretningsadresse"),
        "postal_address": body.get("postadresse"),
        "activity": body.get("aktivitet"),
        "statutory_purpose": body.get("vedtektsfestetFormaal"),
        "founded_date": body.get("stiftelsesdato"),
        "registration_date": _pick(body, "registreringsdatoEnhetsregisteret", "registreringsdatoenhetsregisteret"),
        "statutes_date": body.get("vedtektsdato"),
        # Live JSON spells the VAT flag "registrertIMvaregisteret"; the bulk CSV
        # spells it "registrertIMvaRegisteret". Both are accepted.
        "vat_registered": _pick(body, "registrertIMvaregisteret", "registrertIMvaRegisteret"),
        "vat_registration_date": body.get("registreringsdatoMerverdiavgiftsregisteret"),
        "vat_registration_date_registry": body.get("registreringsdatoMerverdiavgiftsregisteretEnhetsregisteret"),
        "voluntary_vat_descriptions": body.get("frivilligMvaRegistrertBeskrivelser"),
        "registered_in_business_register": body.get("registrertIForetaksregisteret"),
        "business_register_date": body.get("registreringsdatoForetaksregisteret"),
        "registered_in_voluntary_register": body.get("registrertIFrivillighetsregisteret"),
        "voluntary_register_date": body.get("registreringsdatoFrivillighetsregisteret"),
        "registered_in_foundation_register": body.get("registrertIStiftelsesregisteret"),
        "registered_in_party_register": body.get("registrertIPartiregisteret"),
        "party_register_date": body.get("registreringsdatoPartiregisteret"),
        "capital": body.get("kapital"),
        "parent_unit": body.get("overordnetEnhet"),
        "in_group": body.get("erIKonsern"),
        "language_form": body.get("maalform"),
        "annotations": body.get("paategninger"),
        "historical_names": body.get("historiskeNavn"),
        "latest_submitted_accounts": body.get("sisteInnsendteAarsregnskap"),
        # Minimal normalized raw body (HAL links stripped) so no reported field
        # is silently lost if the API adds fields this mapping does not name.
        # The contract adapter skips "raw_body" when generating claims: it is
        # evidence payload, and the record's content_sha256 covers the raw bytes.
        "raw_body": _strip_links(body),
    }


def _classified(field: str, source_type: str, result: FetchResult, value: Any = None) -> dict[str, Any]:
    if result.status == 200:
        return evidence(field, "available", source_type, result.url, value=result.body if value is None else value, content_sha256=result.content_sha256, retrieved_at=result.retrieved_at, effective_at=result.effective_at)
    if result.status in {404, 410}:
        return evidence(field, "not_found", source_type, result.url, note=result.error, content_sha256=result.content_sha256, retrieved_at=result.retrieved_at, effective_at=result.effective_at)
    return evidence(field, "source_error", source_type, result.url, note=result.error, content_sha256=result.content_sha256, retrieved_at=result.retrieved_at, effective_at=result.effective_at)


def _all_org_numbers(body: Any) -> list[str]:
    """Every organisation number named anywhere in a response body.

    Used for the konsernstruktur tree, which is rooted at the ultimate
    parent: the queried company may appear only as a child (verified live
    2026-11-12: konsernstruktur/930132764 is rooted at 919371943 with
    930132764 as a child). Collects both ``organisasjonsnummer`` and
    ``parentOrganisasjonsnummer`` at any depth.
    """
    found: list[str] = []
    stack: list[Any] = [body]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"organisasjonsnummer", "parentOrganisasjonsnummer"} and value not in (None, ""):
                    found.append(str(value))
                stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    return found


def _response_org_numbers(module: str, body: Any) -> list[Any]:
    """Organisation numbers asserted inside a 200 response body, if any."""
    if module == "group" and isinstance(body, (dict, list)):
        return _all_org_numbers(body)
    if module == "registry_live" and isinstance(body, dict):
        return [body.get("organisasjonsnummer")]
    if module == "financials" and isinstance(body, list):
        return [_get(item, "virksomhet", "organisasjonsnummer") for item in body if isinstance(item, dict)]
    return []


def _with_page_param(url: str, page: int) -> str:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key != "page"] + [("page", str(page))]
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query)))


def _fetch_subunit_pages(
    org: str, url: str, fetcher: Callable[[str], FetchResult]
) -> tuple[dict[str, Any] | None, list[FetchResult], dict[str, Any]]:
    """Follow Spring-Data paging (page.number/size/totalPages, 0-based `page`).

    Stops cleanly when a follow-up page is unavailable (e.g. a byte snapshot
    that only carries page 0) and flags the result truncated rather than
    silently presenting a partial subunit list as complete.
    """
    results: list[FetchResult] = []
    items: list[Any] = []
    first_page_meta: dict[str, Any] | None = None
    pagination: dict[str, Any] = {"pages_fetched": 0, "truncated": False, "note": None, "excluded_mismatched": 0}
    page_number = 0
    while True:
        result = fetcher(url if page_number == 0 else _with_page_param(url, page_number))
        results.append(result)
        if result.status != 200:
            if page_number == 0:
                return None, results, pagination
            pagination["truncated"] = True
            pagination["note"] = f"Stopped after page {page_number - 1}: follow-up page fetch failed ({result.error or result.status})"
            break
        body = result.body if isinstance(result.body, dict) else {}
        page_meta = body.get("page") if isinstance(body.get("page"), dict) else {}
        if page_number == 0:
            first_page_meta = page_meta
        items.extend(_get(body, "_embedded", "underenheter") or [])
        pagination["pages_fetched"] += 1
        total_pages = page_meta.get("totalPages")
        if not isinstance(total_pages, int) or page_number + 1 >= total_pages:
            break
        page_number += 1
        if page_number > 50:
            pagination["truncated"] = True
            pagination["note"] = "Stopped after 50 subunit pages (safety cap)"
            break
    merged = {"_embedded": {"underenheter": items}, "page": first_page_meta or {}}
    return merged, results, pagination


def fetch_official_modules(org: str, modules: set[str], fetcher: Callable[[str], FetchResult] = fetch_json) -> tuple[dict[str, Any], list[FetchResult]]:
    records: dict[str, Any] = {}
    metrics: list[FetchResult] = []
    endpoints = {
        "registry_live": (BRREG_ENTITY.format(org=org), "official_registry_live"),
        "financials": (BRREG_ACCOUNTS.format(org=org), "official_annual_accounts"),
        "financial_history": (BRREG_ACCOUNT_YEARS.format(org=org), "official_annual_account_copies"),
        "roles": (BRREG_ROLES.format(org=org), "official_roles"),
        "group": (BRREG_GROUP.format(org=org), "official_group_structure"),
        "locations": (BRREG_SUBUNITS.format(org=org), "official_subunits"),
    }
    for module, (url, source_type) in endpoints.items():
        if module not in modules:
            continue
        if module == "locations":
            merged, page_results, pagination = _fetch_subunit_pages(org, url, fetcher)
            metrics.extend(page_results)
            first = page_results[0]
            if merged is None:
                records[module] = _classified(module, source_type, first)
                continue
            raw_items = _get(merged, "_embedded", "underenheter") or []
            kept = []
            for item in raw_items:
                parent = item.get("overordnetEnhet") if isinstance(item, dict) else None
                if parent not in (None, "") and str(parent) != org:
                    pagination["excluded_mismatched"] += 1
                    continue
                kept.append(item)
            merged = {"_embedded": {"underenheter": kept}, "page": merged.get("page") or {}}
            normalized = normalize_locations(merged, pagination=pagination)
            record = _classified(module, source_type, first, value=normalized)
            if len(page_results) > 1:
                record["content_sha256"] = hashlib.sha256(
                    "".join(item.content_sha256 or "" for item in page_results).encode()
                ).hexdigest()
            if pagination["excluded_mismatched"]:
                record["note"] = (
                    f"Excluded {pagination['excluded_mismatched']} subunit(s) whose "
                    f"overordnetEnhet did not match {org}"
                )
            records[module] = record
            continue
        result = _fetch_history(url) if module == "financial_history" and fetcher is fetch_json else fetcher(url)
        metrics.append(result)
        normalized = None
        if result.status == 200:
            if module == "group":
                # A konsernstruktur tree legitimately names many companies
                # (parent, siblings); it is a mismatch only when the queried
                # company appears NOWHERE in the tree.
                tree_numbers = sorted({str(value) for value in _response_org_numbers(module, result.body) if value not in (None, "")})
                if tree_numbers and org not in tree_numbers:
                    records[module] = evidence(
                        module, "source_error", source_type, result.url,
                        note=f"Organisation-number mismatch: expected {org} somewhere in the group tree, response named {tree_numbers}",
                        content_sha256=result.content_sha256, retrieved_at=result.retrieved_at, effective_at=result.effective_at,
                    )
                    continue
            else:
                mismatched = [value for value in _response_org_numbers(module, result.body) if value not in (None, "") and str(value) != org]
                if mismatched:
                    # A 200 whose body names a different company is rejected, not
                    # trusted: a systemic join error must never become a claim.
                    records[module] = evidence(
                        module, "source_error", source_type, result.url,
                        note=f"Organisation-number mismatch: expected {org}, response reported {sorted({str(value) for value in mismatched})}",
                        content_sha256=result.content_sha256, retrieved_at=result.retrieved_at, effective_at=result.effective_at,
                    )
                    continue
            normalized = normalize_entity(result.body) if module == "registry_live" else normalize_financials(result.body) if module == "financials" else normalize_financial_history(result.body, org) if module == "financial_history" else normalize_roles(result.body) if module == "roles" else normalize_group(result.body, org) if module == "group" else result.body
        records[module] = _classified(module, source_type, result, value=normalized)
    return records, metrics
