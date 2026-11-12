"""Deep extraction from company-owned websites (AUDIT §4 #12–#16).

Pure parsing helpers: no network access here. Fetching, robots checks, the
SSRF guard and byte/page budgets live in :mod:`website` / :mod:`crawl_events`,
which call into this module. Every claim these helpers produce is
company-reported: the website identity gate (``identity.py``, publishable at
score >= 0.9) still decides whether any of it may be published, and the
generic contract adapter marks all website claims ``ambiguous`` when the
gate is not publishable. Registry values are never overwritten here; site
contacts are compared against registry contacts, never merged into them.

Evidence-value keys produced by the callers (see ``docs/website-evidence-schema.md``):

- ``site_contacts`` / ``site_contact_comparison`` (#13)
- ``site_jobs`` (verified) / ``site_jobs_unverified`` (ambiguous, quarantined) (#14)
- ``site_news_items`` (#15, pages + feeds)
- ``site_declared_facts`` (#16)
- ``sitemap_discovery`` / ``feed_discovery`` (#12)
- per-page ``published_date`` / ``date_source`` / ``page_kind`` (#15)
"""

from __future__ import annotations

import datetime as _datetime
import gzip
import hashlib
import re
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any

import tldextract
from bs4 import BeautifulSoup

# Priority terms for secondary-page selection (shared with website.py, which
# re-exports this tuple). Order is rank: earlier terms win. The original kit
# terms keep their positions; WAVE-3A appends career/news/staff terms.
PRIORITY_TERMS = (
    "om-oss", "om_oss", "about", "kontakt", "contact", "ledelse", "management",
    "team", "people", "locations", "lokasjoner", "avdelinger", "butikker",
    "news", "press", "aktuelt", "nyheter",
    "karriere", "ledige-stillinger", "stillinger", "jobb", "presse", "blog",
    "ansatte", "apningstider", "åpningstider", "artikler", "careers",
)

CAREER_PATH_TERMS = ("karriere", "ledige-stillinger", "stillinger", "jobb", "careers", "vacancies")
NEWS_PATH_TERMS = ("news", "press", "presse", "aktuelt", "nyheter", "blog", "artikler")

# schema.org organisation types accepted as structured organisations (#16):
# Organization/Corporation plus LocalBusiness and its common subtypes, plus
# other organisation families. A type not listed here is ignored as an
# organisation (it may still carry a date or a JobPosting, handled elsewhere).
_LOCAL_BUSINESS_SUBTYPES = {
    "AnimalShelter", "AutomotiveBusiness", "ChildCare", "DryCleaningOrLaundry",
    "EmergencyService", "EmploymentAgency", "EntertainmentBusiness",
    "FinancialService", "FoodEstablishment", "GovernmentOffice",
    "HealthAndBeautyBusiness", "HomeAndConstructionBusiness", "InternetCafe",
    "LegalService", "Library", "LodgingBusiness", "MedicalBusiness",
    "ProfessionalService", "RadioStation", "RealEstateAgent", "RecyclingCenter",
    "SelfStorage", "ShoppingCenter", "SportsActivityLocation", "Store",
    "TelevisionStation", "TouristInformationCenter", "TravelAgency",
    "AutoBodyShop", "AutoDealer", "AutoPartsStore", "AutoRental", "AutoRepair",
    "AutoWash", "GasStation", "MotorcycleDealer", "MotorcycleRepair",
    "Bakery", "BarOrPub", "Brewery", "CafeOrCoffeeShop", "FastFoodRestaurant",
    "IceCreamShop", "Restaurant", "Winery", "DaySpa", "HairSalon", "HealthClub",
    "NailSalon", "TattooParlor", "Electrician", "GeneralContractor",
    "HousePainter", "HVACBusiness", "Plumber", "RoofingContractor", "Hospital",
    "MedicalClinic", "Pharmacy", "Dentist", "Optician", "Physician", "Hotel",
    "Motel", "BedAndBreakfast", "Resort", "Hostel", "BookStore", "ClothingStore",
    "ComputerStore", "ConvenienceStore", "DepartmentStore", "ElectronicsStore",
    "Florist", "FurnitureStore", "GardenStore", "GroceryStore", "HardwareStore",
    "HobbyShop", "HomeGoodsStore", "JewelryStore", "LiquorStore",
    "MensClothingStore", "MobilePhoneStore", "MovieRentalStore", "MusicStore",
    "OfficeEquipmentStore", "OutletStore", "PawnShop", "PetStore", "ShoeStore",
    "SportingGoodsStore", "TireShop", "ToyStore", "WholesaleStore", "Casino",
    "ComedyClub", "MovieTheater", "NightClub", "BowlingAlley", "ExerciseGym",
    "GolfCourse", "PublicSwimmingPool", "SkiResort", "SportsClub",
    "StadiumOrArena", "TennisComplex", "AmusementPark", "Aquarium", "ArtGallery",
    "Campground", "Museum", "Park", "TouristAttraction", "Zoo",
    "AccountingService", "InsuranceAgency", "Notary", "Attorney",
}
ORGANISATION_TYPES = {
    "Organization", "Corporation", "LocalBusiness", "NGO",
    "GovernmentOrganization", "EducationalOrganization", "SportsOrganization",
    "PerformingGroup", "MusicGroup", "DanceGroup", "TheaterGroup",
    "SportsTeam", "Airline", "Consortium", "Cooperative",
} | _LOCAL_BUSINESS_SUBTYPES

ARTICLE_TYPES = {"Article", "NewsArticle", "BlogPosting", "Report", "PressRelease"}

LEGAL_AND_GENERIC_TOKENS = {
    "as", "asa", "ans", "da", "enk", "iks", "sa", "sam", "sti", "stiftelsen",
    "nuf", "ab", "b", "v", "limited", "ltd", "inc", "plc", "the", "og", "and",
}

_BINARY_PATH_SUFFIXES = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".css",
    ".js", ".xml", ".zip", ".gz", ".mp3", ".mp4", ".avi", ".mov", ".doc",
    ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
)

_GENERIC_CAREER_LINK_TEXTS = {
    "les mer", "read more", "søk", "søk her", "apply", "apply now",
    "se stillingen", "se alle", "se alle stillinger", "tilbake", "les mer om stillingen",
    "søk på stillingen", "apply for this job", "view job", "view all jobs",
}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_FOOTER_SELECTOR = (
    'footer, address, [itemprop="legalName"], [itemprop="address"], '
    '[itemprop="telephone"], [itemprop="email"]'
)

_MONTHS = {
    "jan": 1, "januar": 1, "january": 1,
    "feb": 2, "februar": 2, "february": 2,
    "mar": 3, "mars": 3, "march": 3,
    "apr": 4, "april": 4,
    "mai": 5, "may": 5,
    "jun": 6, "juni": 6, "june": 6,
    "jul": 7, "juli": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "okt": 10, "oct": 10, "oktober": 10, "october": 10,
    "nov": 11, "november": 11,
    "des": 12, "dec": 12, "desember": 12, "december": 12,
}


def _registered_domain(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    ext = tldextract.extract(parsed.hostname or "")
    return ext.top_domain_under_public_suffix


def _clean_text(value: Any, limit: int = 2000) -> str:
    return " ".join(str(value or "").split())[:limit]


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _first_str(value: Any) -> str:
    for item in _as_list(value):
        if isinstance(item, str) and item.strip():
            return item.strip()
        if isinstance(item, (int, float)):
            return str(item)
    return ""


def _dedupe_strs(values: list[str], limit: int) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = value.strip().casefold()
        if value.strip() and key not in seen:
            seen.add(key)
            out.append(value.strip())
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Structured-data nodes: JSON-LD (incl. @graph), microdata, RDFa (#16)
# ---------------------------------------------------------------------------

def _short_type_name(raw: Any) -> str | None:
    """'http://schema.org/LocalBusiness' -> 'LocalBusiness'; plain names pass through."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    name = raw.strip().rsplit("/", 1)[-1].rsplit("#", 1)[-1].strip()
    return name or None


def type_names(node: Any) -> list[str]:
    """All schema.org type names declared on a node, in short form."""
    if not isinstance(node, dict):
        return []
    names: list[str] = []
    for key in ("@type", "type"):
        for raw in _as_list(node.get(key)):
            name = _short_type_name(raw)
            if name:
                names.append(name)
    return names


def _walk_jsonld(value: Any, out: list[dict[str, Any]], budget: list[int]) -> None:
    if budget[0] <= 0:
        return
    if isinstance(value, dict):
        budget[0] -= 1
        out.append(value)
        graph = value.get("@graph")
        if graph is not None:
            _walk_jsonld(graph, out, budget)
        for key, child in value.items():
            if key == "@graph":
                continue
            _walk_jsonld(child, out, budget)
    elif isinstance(value, list):
        for child in value:
            _walk_jsonld(child, out, budget)


def _flatten_microdata_item(item: Any) -> dict[str, Any] | None:
    """extruct microdata item -> JSON-LD-shaped dict.

    extruct microdata items look like
    ``{"type": "http://schema.org/Organization", "properties": {"name": ["X"], ...}}``
    with nested items in the same shape.
    """
    if not isinstance(item, dict) or "properties" not in item:
        return None
    node: dict[str, Any] = {"_source_syntax": "microdata"}
    names = [_short_type_name(raw) for raw in _as_list(item.get("type"))]
    names = [name for name in names if name]
    if names:
        node["@type"] = names[0] if len(names) == 1 else names
    properties = item.get("properties") or {}
    if not isinstance(properties, dict):
        return node
    for key, values in properties.items():
        flattened: list[Any] = []
        for entry in _as_list(values):
            nested = _flatten_microdata_item(entry)
            flattened.append(nested if nested is not None else entry)
        node[key] = flattened[0] if len(flattened) == 1 else flattened
    return node


def _flatten_rdfa_node(node: Any) -> dict[str, Any] | None:
    """extruct RDFa node -> JSON-LD-shaped dict.

    extruct RDFa nodes use expanded IRIs as keys:
    ``{"@type": ["http://schema.org/Organization"],
    "http://schema.org/name": [{"@value": "X"}], ...}``.
    """
    if not isinstance(node, dict):
        return None
    out: dict[str, Any] = {"_source_syntax": "rdfa"}
    for key, values in node.items():
        if key == "@type":
            names = [_short_type_name(raw) for raw in _as_list(values)]
            names = [name for name in names if name]
            if names:
                out["@type"] = names[0] if len(names) == 1 else names
            continue
        if key in {"@id", "@context"}:
            out[key] = values
            continue
        short = key.rsplit("/", 1)[-1].rsplit("#", 1)[-1] if isinstance(key, str) else str(key)
        flattened: list[Any] = []
        for entry in _as_list(values):
            if isinstance(entry, dict) and "@value" in entry and len(entry) == 1:
                flattened.append(entry["@value"])
            elif isinstance(entry, dict):
                nested = _flatten_rdfa_node(entry)
                flattened.append(nested if nested is not None else entry)
            else:
                flattened.append(entry)
        out[short] = flattened[0] if len(flattened) == 1 else flattened
    return out


def all_structured_nodes(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """Every structured-data node from extruct output, all syntaxes.

    JSON-LD nodes are returned as parsed (including ``@graph`` members and
    nested nodes); microdata/RDFa nodes are flattened to the same shape and
    tagged with ``_source_syntax``.
    """
    nodes: list[dict[str, Any]] = []
    budget = [500]
    _walk_jsonld(metadata.get("json-ld", []), nodes, budget)
    for item in _as_list(metadata.get("microdata")):
        flat = _flatten_microdata_item(item)
        if flat is not None:
            nodes.append(flat)
    for item in _as_list(metadata.get("rdfa")):
        flat = _flatten_rdfa_node(item)
        if flat is not None:
            nodes.append(flat)
    return nodes


def extract_structured_organisations(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """Organisation nodes: JSON-LD incl. @graph + microdata + RDFa (#16).

    Deduplicated, capped at 20, JSON-LD first (preserving the historical
    behaviour for JSON-LD-only pages exactly: original dicts, original order).
    """
    seen: set[str] = set()
    organisations: list[dict[str, Any]] = []
    for node in all_structured_nodes(metadata):
        if not (set(type_names(node)) & ORGANISATION_TYPES):
            continue
        key = hashlib.sha256(repr(sorted(node.items(), key=lambda kv: str(kv[0]))).encode("utf-8", "ignore")).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        organisations.append(node)
        if len(organisations) >= 20:
            break
    return organisations


def _parse_postal_address(value: Any) -> dict[str, str] | None:
    """PostalAddress node or string -> normalized address dict (schema.org keys)."""
    if isinstance(value, str):
        text = _clean_text(value, 500)
        if not text:
            return None
        address: dict[str, str] = {"formatted": text}
        postcode = re.search(r"\b(\d{4})\b", text)
        if postcode:
            address["postalCode"] = postcode.group(1)
        return address
    if not isinstance(value, dict):
        return None
    address = {}
    for key in ("streetAddress", "postalCode", "addressLocality", "addressRegion", "addressCountry", "postOfficeBoxNumber"):
        text = _first_str(value.get(key))
        if text:
            address[key] = text[:300]
    if not address:
        return None
    parts = [address.get("streetAddress", ""), f"{address.get('postalCode', '')} {address.get('addressLocality', '')}".strip(), address.get("addressCountry", "")]
    address["formatted"] = _clean_text(", ".join(part for part in parts if part), 500)
    return address


def _parse_contact_point(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    point: dict[str, Any] = {}
    for key, out_key in (("contactType", "contact_type"), ("telephone", "telephone"), ("email", "email"),
                         ("areaServed", "area_served"), ("availableLanguage", "available_language"),
                         ("hoursAvailable", "hours_available")):
        raw = value.get(key)
        if raw is None:
            continue
        if isinstance(raw, list):
            texts = [_clean_text(item, 200) for item in raw if _clean_text(item, 200)]
            if texts:
                point[out_key] = texts[0] if len(texts) == 1 else texts
        else:
            text = _clean_text(raw, 200)
            if text:
                point[out_key] = text
    return point or None


def _valid_email(value: str) -> str | None:
    text = value.strip()
    return text if _EMAIL_RE.match(text) and len(text) <= 254 else None


def _valid_phone(value: str) -> str | None:
    text = " ".join(str(value).split()).strip()
    digits = re.sub(r"\D", "", text)
    if len(digits) < 6 or len(text) > 40:
        return None
    return text


def extract_footer_contacts(soup: BeautifulSoup) -> dict[str, Any]:
    """tel:/mailto: links and address nodes from footer/address regions."""
    telephones: list[str] = []
    emails: list[str] = []
    addresses: list[dict[str, str]] = []
    for node in soup.select(_FOOTER_SELECTOR):
        for anchor in node.select("a[href]"):
            href = str(anchor.get("href") or "").strip()
            if href.lower().startswith("mailto:"):
                email = _valid_email(urllib.parse.unquote(href[7:].split("?", 1)[0]))
                if email:
                    emails.append(email)
            elif href.lower().startswith("tel:"):
                phone = _valid_phone(urllib.parse.unquote(href[4:].split("?", 1)[0]))
                if phone:
                    telephones.append(phone)
        if node.name == "address" or node.get("itemprop") == "address":
            address = _parse_postal_address(node.get_text(" ", strip=True))
            if address:
                addresses.append(address)
        for prop in ("telephone", "email"):
            for item in node.select(f'[itemprop="{prop}"]'):
                text = item.get_text(" ", strip=True)
                if prop == "telephone":
                    phone = _valid_phone(text)
                    if phone:
                        telephones.append(phone)
                else:
                    email = _valid_email(text)
                    if email:
                        emails.append(email)
    return {
        "telephone": _dedupe_strs(telephones, 10),
        "email": _dedupe_strs(emails, 10),
        "addresses": addresses[:5],
    }


def extract_site_contacts(
    organisations: list[dict[str, Any]],
    soup: BeautifulSoup | None = None,
    *,
    source_url: str | None = None,
) -> dict[str, Any]:
    """Site contacts from structured data + footer (#13).

    Company-reported only. Values are kept separate from registry contacts;
    see :func:`compare_site_contacts` for the side-by-side comparison.
    """
    telephones: list[str] = []
    emails: list[str] = []
    addresses: list[dict[str, str]] = []
    opening_hours: list[str] = []
    opening_specs: list[dict[str, Any]] = []
    contact_points: list[dict[str, Any]] = []
    provenance: dict[str, list[str]] = {}

    def note(field: str, source: str) -> None:
        provenance.setdefault(field, [])
        if source not in provenance[field]:
            provenance[field].append(source)

    for org in organisations:
        for raw in _as_list(org.get("telephone")):
            phone = _valid_phone(raw) if isinstance(raw, str) else None
            if phone:
                telephones.append(phone)
                note("telephone", "structured_data")
        for raw in _as_list(org.get("email")):
            email = _valid_email(raw) if isinstance(raw, str) else None
            if email:
                emails.append(email)
                note("email", "structured_data")
        for raw in _as_list(org.get("address")):
            address = _parse_postal_address(raw)
            if address:
                addresses.append(address)
                note("addresses", "structured_data")
        for raw in _as_list(org.get("location")):
            if isinstance(raw, dict):
                address = _parse_postal_address(raw.get("address"))
                if address:
                    addresses.append(address)
                    note("addresses", "structured_data")
        for raw in _as_list(org.get("contactPoint")):
            point = _parse_contact_point(raw)
            if point:
                contact_points.append(point)
                note("contact_points", "structured_data")
                if point.get("telephone"):
                    phone = _valid_phone(str(point["telephone"]))
                    if phone:
                        telephones.append(phone)
                if point.get("email"):
                    email = _valid_email(str(point["email"]))
                    if email:
                        emails.append(email)
        for raw in _as_list(org.get("openingHours")):
            text = _clean_text(raw, 200)
            if text:
                opening_hours.append(text)
                note("opening_hours", "structured_data")
        for raw in _as_list(org.get("openingHoursSpecification")):
            if isinstance(raw, dict):
                spec: dict[str, Any] = {}
                days = [_clean_text(day, 30) for day in _as_list(raw.get("dayOfWeek")) if _clean_text(day, 30)]
                if days:
                    spec["day_of_week"] = days
                for key in ("opens", "closes", "validFrom", "validThrough"):
                    text = _clean_text(raw.get(key), 60)
                    if text:
                        spec[key] = text
                if spec:
                    opening_specs.append(spec)
                    note("opening_hours_specifications", "structured_data")

    if soup is not None:
        footer = extract_footer_contacts(soup)
        if footer["telephone"]:
            telephones.extend(footer["telephone"])
            note("telephone", "footer")
        if footer["email"]:
            emails.extend(footer["email"])
            note("email", "footer")
        if footer["addresses"]:
            addresses.extend(footer["addresses"])
            note("addresses", "footer")

    seen_addresses: set[str] = set()
    unique_addresses: list[dict[str, str]] = []
    for address in addresses:
        key = address.get("formatted", "").casefold()
        if key and key not in seen_addresses:
            seen_addresses.add(key)
            unique_addresses.append(address)

    return {
        "source": "company_owned_website",
        "source_url": source_url,
        "telephone": _dedupe_strs(telephones, 10),
        "email": _dedupe_strs(emails, 10),
        "addresses": unique_addresses[:5],
        "opening_hours": _dedupe_strs(opening_hours, 20),
        "opening_hours_specifications": opening_specs[:10],
        "contact_points": contact_points[:10],
        "provenance": provenance,
    }


# ---------------------------------------------------------------------------
# Site-declared facts (#16): foundingDate, numberOfEmployees, aggregateRating
# ---------------------------------------------------------------------------

def _parse_employee_count(value: Any) -> dict[str, Any] | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return {"value": int(value)}
    if isinstance(value, dict):
        if value.get("value") is not None:
            try:
                return {"value": int(value["value"])}
            except (TypeError, ValueError):
                pass
        out: dict[str, Any] = {}
        for key in ("minValue", "maxValue"):
            try:
                if value.get(key) is not None:
                    out[{"minValue": "min_value", "maxValue": "max_value"}[key]] = int(value[key])
            except (TypeError, ValueError):
                pass
        return out or None
    if isinstance(value, str):
        digits = re.sub(r"[^\d]", "", value)
        if digits:
            try:
                return {"value": int(digits)}
            except ValueError:
                return None
    return None


def extract_site_declared_facts(organisations: list[dict[str, Any]]) -> dict[str, Any]:
    """Company-reported facts from structured data (#16).

    Displayed side by side with registry values, never equated with them:
    a site-declared employee count is not the registry count, and a
    site-declared aggregate rating is not an independent review.
    """
    facts: dict[str, Any] = {
        "source": "company_owned_structured_data",
        "source_label": "company-reported; registry values remain authoritative",
    }
    vat_ids: list[str] = []
    for org in organisations:
        for key in ("vatID", "taxID", "vatId", "taxId"):
            for raw in _as_list(org.get(key)):
                text = _clean_text(raw, 60)
                if text:
                    vat_ids.append(text)
        if "founding_date" not in facts:
            raw = _first_str(org.get("foundingDate") or org.get("founding_date"))
            parsed = parse_explicit_date(raw) if raw else None
            if parsed:
                facts["founding_date"] = parsed
                facts["founding_date_raw"] = raw[:60]
        if "number_of_employees_site_declared" not in facts:
            count = _parse_employee_count(org.get("numberOfEmployees"))
            if count:
                facts["number_of_employees_site_declared"] = count
        if "aggregate_rating_site_declared" not in facts:
            rating = org.get("aggregateRating")
            if isinstance(rating, dict):
                parsed_rating: dict[str, Any] = {
                    "label": "site-declared aggregate rating; not an independent review",
                }
                try:
                    if rating.get("ratingValue") is not None:
                        parsed_rating["rating_value"] = float(rating["ratingValue"])
                except (TypeError, ValueError):
                    pass
                try:
                    if rating.get("reviewCount") is not None:
                        parsed_rating["review_count"] = int(rating["reviewCount"])
                except (TypeError, ValueError):
                    pass
                try:
                    if rating.get("bestRating") is not None:
                        parsed_rating["best_rating"] = float(rating["bestRating"])
                except (TypeError, ValueError):
                    pass
                if len(parsed_rating) > 1:
                    facts["aggregate_rating_site_declared"] = parsed_rating
    if vat_ids:
        facts["vat_ids"] = _dedupe_strs(vat_ids, 5)
    return facts


# ---------------------------------------------------------------------------
# Contact comparison vs registry (#13)
# ---------------------------------------------------------------------------

def normalize_phone_for_comparison(value: Any) -> str:
    """Digits only, Norwegian country code stripped: '+47 22 41 12 00' -> '22411200'."""
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 12 and digits.startswith("0047"):
        digits = digits[4:]
    elif len(digits) == 10 and digits.startswith("47"):
        digits = digits[2:]
    return digits


def normalize_email_for_comparison(value: Any) -> str:
    return str(value or "").strip().casefold()


def _address_parts(address: Any) -> dict[str, str]:
    """Postcode/locality/street from any address shape we carry.

    Registry bulk (normalized): address/post_town/postcode/municipality.
    Registry live (raw API): adresse/poststed/postnummer/kommune.
    Site structured: streetAddress/addressLocality/postalCode.
    Plain strings are scanned for a 4-digit postcode.
    """
    if isinstance(address, str):
        text = address.strip()
        postcode = re.search(r"\b(\d{4})\b", text)
        return {"postcode": postcode.group(1) if postcode else "", "locality": "", "street": text.casefold()}
    if not isinstance(address, dict):
        return {"postcode": "", "locality": "", "street": ""}
    postcode = _first_str(address.get("postcode") or address.get("postalCode") or address.get("postnummer"))
    locality = _first_str(address.get("post_town") or address.get("addressLocality") or address.get("poststed") or address.get("municipality") or address.get("kommune"))
    street = _first_str(address.get("address") or address.get("streetAddress") or address.get("adresse") or address.get("formatted"))
    return {"postcode": postcode, "locality": locality.casefold(), "street": street.casefold()}


def _addresses_match(site: dict[str, str], registry: dict[str, str]) -> bool:
    if site["postcode"] and registry["postcode"] and site["postcode"] != registry["postcode"]:
        return False
    if site["postcode"] and site["postcode"] == registry["postcode"]:
        if site["locality"] and registry["locality"] and site["locality"] != registry["locality"]:
            return False
        return True
    if site["locality"] and site["locality"] == registry["locality"]:
        site_tokens = {token for token in re.findall(r"[a-zæøå0-9]+", site["street"]) if len(token) > 2}
        registry_tokens = {token for token in re.findall(r"[a-zæøå0-9]+", registry["street"]) if len(token) > 2}
        return bool(site_tokens & registry_tokens)
    return False


def registry_contacts_from_profile(profile: dict[str, Any]) -> dict[str, Any] | None:
    """Registry contacts from a profile, bulk top-level first, live API second.

    Returns None when the profile carries no registry contact at all, so the
    comparison can say so explicitly instead of comparing against nothing.
    """
    contacts: dict[str, Any] = {
        "email": profile.get("email"),
        "phone": profile.get("phone"),
        "mobile": profile.get("mobile"),
        "business_address": profile.get("business_address"),
        "postal_address": profile.get("postal_address"),
    }
    if not any(contacts.values()):
        live = ((profile.get("evidence") or {}).get("registry_live") or {}).get("value") or {}
        if isinstance(live, dict):
            contacts = {
                "email": live.get("email"),
                "phone": live.get("phone"),
                "mobile": live.get("mobile"),
                "business_address": live.get("business_address"),
                "postal_address": live.get("postal_address"),
            }
    if not any(contacts.values()):
        raw = ((profile.get("evidence") or {}).get("registry") or {}).get("value") or {}
        if isinstance(raw, dict):
            contacts = {
                "email": raw.get("epostadresse"),
                "phone": raw.get("telefon"),
                "mobile": raw.get("mobil"),
                "business_address": {
                    "address": raw.get("forretningsadresse.adresse"),
                    "post_town": raw.get("forretningsadresse.poststed"),
                    "postcode": raw.get("forretningsadresse.postnummer"),
                },
                "postal_address": {
                    "address": raw.get("postadresse.adresse"),
                    "post_town": raw.get("postadresse.poststed"),
                    "postcode": raw.get("postadresse.postnummer"),
                },
            }
    if not any(value for key, value in contacts.items() if key in {"email", "phone", "mobile"}):
        addresses = [contacts.get("business_address"), contacts.get("postal_address")]
        if not any(isinstance(addr, dict) and any(addr.values()) for addr in addresses):
            return None
    return contacts


def compare_site_contacts(
    site_contacts: dict[str, Any],
    registry_contacts: dict[str, Any] | None,
) -> dict[str, Any]:
    """Side-by-side site vs registry contacts (#13).

    A match corroborates; a difference shows BOTH values labelled by source.
    Registry values are authoritative and are never overwritten by this
    comparison — it is a read-only report.
    """
    base = {
        "note": "Registry values are authoritative; site values are company-reported and never overwrite registry values.",
    }
    if not registry_contacts:
        return {**base, "registry_contacts_supplied": False, "comparisons": []}
    site_emails = [_clean_text(item, 254) for item in site_contacts.get("email") or []]
    registry_email = _clean_text(registry_contacts.get("email"), 254)
    site_phones = [_clean_text(item, 40) for item in site_contacts.get("telephone") or []]
    registry_phones = [
        _clean_text(item, 40)
        for item in (registry_contacts.get("phone"), registry_contacts.get("mobile"))
        if _clean_text(item, 40)
    ]
    site_addresses = list(site_contacts.get("addresses") or [])
    registry_addresses = [
        item for item in (registry_contacts.get("business_address"), registry_contacts.get("postal_address"))
        if item
    ]

    def verdict(site_values: list[Any], registry_values: list[Any], matched: bool) -> str:
        if matched:
            return "match"
        if site_values and registry_values:
            return "differs"
        if site_values:
            return "site_only"
        if registry_values:
            return "registry_only"
        return "both_missing"

    email_match = bool(
        registry_email
        and any(normalize_email_for_comparison(email) == normalize_email_for_comparison(registry_email) for email in site_emails)
    )
    registry_phone_digits = {normalize_phone_for_comparison(phone) for phone in registry_phones if normalize_phone_for_comparison(phone)}
    phone_match = bool(registry_phone_digits) and any(
        normalize_phone_for_comparison(phone) in registry_phone_digits for phone in site_phones
    )
    address_match = any(
        _addresses_match(_address_parts(site), _address_parts(registry))
        for site in site_addresses for registry in registry_addresses
    )
    return {
        **base,
        "registry_contacts_supplied": True,
        "comparisons": [
            {
                "field": "email",
                "site_values": site_emails,
                "registry_values": [registry_email] if registry_email else [],
                "verdict": verdict(site_emails, [registry_email] if registry_email else [], email_match),
            },
            {
                "field": "telephone",
                "site_values": site_phones,
                "registry_values": registry_phones,
                "verdict": verdict(site_phones, registry_phones, phone_match),
            },
            {
                "field": "address",
                "site_values": site_addresses,
                "registry_values": registry_addresses,
                "verdict": verdict(site_addresses, registry_addresses, address_match),
            },
        ],
    }


# ---------------------------------------------------------------------------
# Explicit dates (#15) — unknown stays None, never guessed from retrieval time
# ---------------------------------------------------------------------------

def _valid_iso_date(year: int, month: int, day: int) -> str | None:
    if not 1900 <= year <= 2100:
        return None
    try:
        return _datetime.date(year, month, day).isoformat()
    except ValueError:
        return None


def parse_explicit_date(text: Any) -> str | None:
    """Parse an explicitly written calendar date to ISO YYYY-MM-DD, else None.

    Accepted: ISO dates/datetimes, YYYY/MM/DD, DD.MM.YYYY and DD/MM/YYYY
    (day-first, Norwegian convention), and day + month name + year in
    Norwegian or English. A bare year, a bare month, or an unparseable string
    returns None — callers must not substitute the retrieval date.
    """
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text:
        return None
    match = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
    if match:
        parsed = _valid_iso_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if parsed:
            return parsed
    match = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", text)
    if match:
        parsed = _valid_iso_date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
        if parsed:
            return parsed
    month_pattern = "|".join(sorted(_MONTHS, key=len, reverse=True))
    match = re.search(rf"(\d{{1,2}})\.?\s+({month_pattern})\.?\s+(\d{{4}})", text, re.I)
    if match:
        month = _MONTHS[match.group(2).lower().rstrip(".")]
        return _valid_iso_date(int(match.group(3)), month, int(match.group(1)))
    match = re.search(rf"({month_pattern})\.?\s+(\d{{1,2}}),?\s+(\d{{4}})", text, re.I)
    if match:
        month = _MONTHS[match.group(1).lower().rstrip(".")]
        return _valid_iso_date(int(match.group(3)), month, int(match.group(2)))
    return None


def extract_page_date(
    soup: BeautifulSoup,
    structured_nodes: list[dict[str, Any]],
    url: str,
    text_excerpt: str = "",
) -> tuple[str | None, str | None]:
    """Best explicit publication date for a page, with its source.

    Priority: JSON-LD datePublished > dateModified > meta article/date >
    date in the URL path > explicit date in title/leading text. Returns
    ``(None, None)`` when no explicit date exists.
    """
    for key, source in (("datePublished", "json_ld_date_published"), ("dateModified", "json_ld_date_modified")):
        for node in structured_nodes:
            parsed = parse_explicit_date(_first_str(node.get(key)))
            if parsed:
                return parsed, source
    for selector, source in (
        ('meta[property="article:published_time"]', "meta_article_published_time"),
        ('meta[name="datePublished"]', "meta_date_published"),
        ('meta[name="date"]', "meta_date"),
        ('meta[property="article:modified_time"]', "meta_article_modified_time"),
        ('meta[property="og:updated_time"]', "meta_og_updated_time"),
    ):
        tag = soup.select_one(selector)
        if tag is not None:
            parsed = parse_explicit_date(str(tag.get("content") or ""))
            if parsed:
                return parsed, source
    path = urllib.parse.urlparse(url).path
    parsed = parse_explicit_date(path)
    if parsed:
        return parsed, "url_path"
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    for snippet in (title, text_excerpt[:1500]):
        parsed = parse_explicit_date(snippet)
        if parsed:
            return parsed, "page_text"
    return None, None


def classify_page_kind(
    url: str,
    *,
    structured_nodes: list[dict[str, Any]] | None = None,
    is_homepage: bool = False,
) -> str:
    """Heuristic page kind: homepage / career / article / archive / contact / about / other.

    Archive vs article: a news-ish path with at most 2 segments is treated as
    a listing (archive); a deeper news-ish path, or a page declaring an
    Article/NewsArticle/BlogPosting type, is treated as an individual article.
    The heuristic is documented here because it is a guess about URL shape,
    not a fact about the page.
    """
    if is_homepage:
        return "homepage"
    path = urllib.parse.urlparse(url).path.casefold()
    segments = [part for part in path.split("/") if part]
    if any(term in path for term in CAREER_PATH_TERMS):
        return "career"
    types = set()
    for node in structured_nodes or []:
        types.update(type_names(node))
    if types & ARTICLE_TYPES:
        return "article"
    if any(term in path for term in NEWS_PATH_TERMS):
        return "archive" if len(segments) <= 2 else "article"
    if "kontakt" in path or "contact" in path:
        return "contact"
    if "om-oss" in path or "om_oss" in path or "about" in path:
        return "about"
    return "other"


def build_news_items(
    pages: list[dict[str, Any]],
    *,
    retrieved_at: str,
    feed_items: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Dated site-activity items (#15): one per news page / feed entry.

    Items with no explicit date keep ``date_published: null``. Every item is
    labelled company-owned; site activity is never independent sentiment.
    """
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in pages:
        kind = page.get("page_kind") or "other"
        if kind not in {"article", "archive"}:
            continue
        url = str(page.get("url") or "")
        if not url or url in seen:
            continue
        seen.add(url)
        items.append({
            "url": url,
            "content_sha256": page.get("content_sha256"),
            "hash_scope": "page_bytes",
            "title": _clean_text(page.get("title"), 300),
            "date_published": page.get("published_date"),
            "date_source": page.get("date_source"),
            "page_kind": kind,
            "origin": "page",
            "source_label": "company_owned",
            "interpretation": "Company-owned activity; not independent sentiment.",
            "retrieved_at": retrieved_at,
        })
    for item in feed_items or []:
        url = str(item.get("url") or "")
        if not url or url in seen:
            continue
        seen.add(url)
        items.append(item)
    # Newest first; undated last; stable within equal dates by URL.
    dated = sorted([item for item in items if item.get("date_published")], key=lambda item: (item["date_published"], item["url"]), reverse=True)
    undated = sorted([item for item in items if not item.get("date_published")], key=lambda item: item["url"])
    return (dated + undated)[:25]


# ---------------------------------------------------------------------------
# Sitemap + feed discovery (#12)
# ---------------------------------------------------------------------------

def parse_robots_sitemaps(robots_text: str | None) -> list[str]:
    """``Sitemap:`` directives from robots.txt, HTTP(S) only, capped at 10."""
    urls: list[str] = []
    for line in (robots_text or "").splitlines():
        if line.strip().lower().startswith("sitemap:"):
            candidate = line.split(":", 1)[1].strip()
            if candidate.lower().startswith(("http://", "https://")) and candidate not in urls:
                urls.append(candidate)
        if len(urls) >= 10:
            break
    return urls


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def parse_sitemap_xml(
    data: bytes,
    *,
    homepage_domain: str,
    max_urls: int = 200,
) -> dict[str, Any]:
    """Parse a sitemap or sitemap index.

    URLs outside the homepage registered domain are rejected and counted,
    never returned. Handles gzip-compressed sitemaps. Malformed XML yields an
    ``error`` and empty lists rather than raising.
    """
    result: dict[str, Any] = {"page_urls": [], "sitemap_urls": [], "rejected_out_of_domain": 0, "error": None}
    payload = data
    if payload[:2] == b"\x1f\x8b":
        try:
            payload = gzip.decompress(payload[:5_000_000])
        except (OSError, EOFError, ValueError) as exc:
            result["error"] = f"gzip error: {exc}"
            return result
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        result["error"] = f"XML parse error: {exc}"
        return result
    kind = _local_name(root.tag)
    if kind == "sitemapindex":
        for entry in root:
            for child in entry:
                if _local_name(child.tag) == "loc" and (child.text or "").strip():
                    url = (child.text or "").strip()
                    if _registered_domain(url) != homepage_domain:
                        result["rejected_out_of_domain"] += 1
                    elif url not in result["sitemap_urls"]:
                        result["sitemap_urls"].append(url)
        result["sitemap_urls"] = result["sitemap_urls"][:20]
        return result
    if kind != "urlset":
        result["error"] = f"unexpected sitemap root: {kind or 'empty'}"
        return result
    for entry in root:
        for child in entry:
            if _local_name(child.tag) == "loc" and (child.text or "").strip():
                url = (child.text or "").strip()
                if not url.lower().startswith(("http://", "https://")):
                    continue
                if _registered_domain(url) != homepage_domain:
                    result["rejected_out_of_domain"] += 1
                elif url not in result["page_urls"]:
                    result["page_urls"].append(url)
        if len(result["page_urls"]) >= max_urls:
            break
    return result


def select_sitemap_candidates(
    urls: list[str],
    *,
    existing: list[str],
    limit: int,
) -> list[str]:
    """Rank sitemap URLs by priority term, skipping binaries and known pages.

    Only URLs matching a priority term are selected: the bounded crawl does
    not chase every sitemap URL.
    """
    seen = {url.rstrip("/") for url in existing}
    ranked: list[tuple[int, str]] = []
    for url in urls:
        clean = url.split("?", 1)[0].split("#", 1)[0]
        if clean.rstrip("/") in seen:
            continue
        path = urllib.parse.urlparse(clean).path.casefold()
        if path.casefold().endswith(_BINARY_PATH_SUFFIXES):
            continue
        rank = next((index for index, term in enumerate(PRIORITY_TERMS) if term in path), None)
        if rank is None:
            continue
        ranked.append((rank, clean))
        seen.add(clean.rstrip("/"))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [url for _, url in ranked[:limit]]


def discover_feed_links(soup: BeautifulSoup, base_url: str, homepage_domain: str) -> list[str]:
    """``<link rel="alternate" type="application/rss+xml|atom+xml">`` on the page.

    Only same-registered-domain feeds are returned; third-party feeds are
    rejected even when linked from the homepage.
    """
    feeds: list[str] = []
    for tag in soup.select('link[rel~="alternate"]'):
        feed_type = str(tag.get("type") or "").lower()
        if feed_type not in {"application/rss+xml", "application/atom+xml"}:
            continue
        href = str(tag.get("href") or "").strip()
        if not href:
            continue
        url = urllib.parse.urljoin(base_url, href)
        if _registered_domain(url) != homepage_domain:
            continue
        if url not in feeds:
            feeds.append(url)
        if len(feeds) >= 5:
            break
    return feeds


def _feed_entry_date(raw: str) -> tuple[str | None, str | None]:
    """RSS pubDate / Atom updated|published -> (ISO date, source label)."""
    text = (raw or "").strip()
    if not text:
        return None, None
    try:
        parsed = parsedate_to_datetime(text)
        return parsed.date().isoformat(), "feed_pub_date"
    except (TypeError, ValueError, OverflowError):
        pass
    parsed = parse_explicit_date(text)
    return (parsed, "feed_pub_date") if parsed else (None, None)


def parse_feed(
    data: bytes,
    feed_url: str,
    *,
    homepage_domain: str,
    retrieved_at: str,
    max_items: int = 25,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Parse an RSS 2.0 or Atom feed into dated news items (#12/#15).

    Entry links outside the registered domain are dropped. The item hash
    covers the feed entry (feed URL + link + title), NOT fetched article
    bytes — ``hash_scope`` says so explicitly.
    """
    stats: dict[str, Any] = {"feed_url": feed_url, "items_found": 0, "items_dropped_out_of_domain": 0, "error": None}
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        stats["error"] = f"XML parse error: {exc}"
        return [], stats
    entries: list[dict[str, str]] = []
    kind = _local_name(root.tag)
    if kind == "rss":
        for item in root.iter():
            if _local_name(item.tag) != "item":
                continue
            entry: dict[str, str] = {}
            for child in item:
                name = _local_name(child.tag)
                if name in {"title", "link", "pubDate", "guid"} and (child.text or "").strip():
                    entry[name] = (child.text or "").strip()
            entries.append(entry)
    elif kind == "feed":
        for item in root.iter():
            if _local_name(item.tag) != "entry":
                continue
            entry = {}
            for child in item:
                name = _local_name(child.tag)
                if name == "title" and (child.text or "").strip():
                    entry["title"] = (child.text or "").strip()
                elif name == "link":
                    rel = (child.get("rel") or "alternate").strip()
                    href = (child.get("href") or "").strip()
                    if href and (rel == "alternate" or "link" not in entry):
                        entry["link"] = href
                elif name in {"updated", "published"} and (child.text or "").strip():
                    entry.setdefault("pubDate", (child.text or "").strip())
            entries.append(entry)
    else:
        stats["error"] = f"unexpected feed root: {kind or 'empty'}"
        return [], stats
    items: list[dict[str, Any]] = []
    for entry in entries:
        link = entry.get("link", "")
        title = entry.get("title", "")
        if not link or not link.lower().startswith(("http://", "https://")):
            continue
        if _registered_domain(link) != homepage_domain:
            stats["items_dropped_out_of_domain"] += 1
            continue
        date, date_source = _feed_entry_date(entry.get("pubDate", ""))
        digest = hashlib.sha256(f"{feed_url}|{link}|{title}".encode("utf-8")).hexdigest()
        items.append({
            "url": link,
            "content_sha256": digest,
            "hash_scope": "feed_entry",
            "title": _clean_text(title, 300),
            "date_published": date,
            "date_source": date_source,
            "page_kind": "article",
            "origin": "feed",
            "source_label": "company_owned",
            "interpretation": "Company-owned activity; not independent sentiment.",
            "retrieved_at": retrieved_at,
        })
        if len(items) >= max_items:
            break
    stats["items_found"] = len(items)
    return items, stats


# ---------------------------------------------------------------------------
# Jobs from the company site (#14)
# ---------------------------------------------------------------------------

def _name_tokens(value: Any) -> list[str]:
    """Legal-name tokenisation mirroring identity.py (kept local: no import cycle)."""
    text = str(value or "").translate(str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    return [token for token in re.findall(r"[a-z0-9]+", text) if token not in LEGAL_AND_GENERIC_TOKENS and len(token) > 1]


def _digit_runs_match(text: Any, org_digits: str) -> bool:
    """Mirror of identity._digit_runs_match: digit groups joined by spaces/dots/dashes."""
    if not org_digits:
        return False
    text = str(text or "")
    groups = [(match.group(), match.start(), match.end()) for match in re.finditer(r"\d+", text)]
    chains: list[list] = []
    for digits, start, end in groups:
        if chains and re.fullmatch(r"[\s.\-]*", text[chains[-1][1]:start]) is not None:
            chains[-1][0].append(digits)
            chains[-1][1] = end
        else:
            chains.append([[digits], end])
    for chain, _ in chains:
        for size in range(1, len(chain) + 1):
            for offset in range(len(chain) - size + 1):
                if "".join(chain[offset:offset + size]) == org_digits:
                    return True
    return False


def _hiring_name_matches(hiring_name: str, legal_name: str) -> bool:
    hiring_tokens = _name_tokens(hiring_name)
    legal_tokens = _name_tokens(legal_name)
    if not hiring_tokens or not legal_tokens:
        return False
    if set(legal_tokens).issubset(set(hiring_tokens)):
        return True
    return "".join(legal_tokens) in "".join(hiring_tokens)


def _parse_job_location(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        text = _clean_text(value, 300)
        return {"formatted": text} if text else None
    if isinstance(value, list):
        for item in value:
            parsed = _parse_job_location(item)
            if parsed:
                return parsed
        return None
    if isinstance(value, dict):
        address = _parse_postal_address(value.get("address"))
        name = _clean_text(value.get("name"), 200)
        out: dict[str, Any] = {}
        if name:
            out["name"] = name
        if address:
            out["address"] = address
        if out:
            out["formatted"] = _clean_text(", ".join(filter(None, [name, address.get("formatted", "") if address else ""])), 300)
            return out
    return None


def _parse_hiring_organisation(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        name = _clean_text(value, 300)
        return {"name": name} if name else None
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {}
    name = _clean_text(value.get("name") or value.get("legalName"), 300)
    if name:
        out["name"] = name
    url = _clean_text(value.get("url") or value.get("sameAs"), 500)
    if url:
        out["url"] = url
    identifier = value.get("identifier")
    if isinstance(identifier, dict):
        ident = _clean_text(identifier.get("value"), 60)
    else:
        ident = _clean_text(identifier, 60)
    if ident:
        out["identifier"] = ident
    return out or None


def _strip_html(text: str, limit: int = 500) -> str:
    return _clean_text(re.sub(r"<[^>]+>", " ", text), limit)


def extract_job_postings_from_nodes(
    nodes: list[dict[str, Any]],
    *,
    source_url: str,
    content_sha256: str | None,
    retrieved_at: str,
) -> list[dict[str, Any]]:
    """JSON-LD/microdata JobPosting nodes -> job-posting candidates (#14)."""
    candidates: list[dict[str, Any]] = []
    for node in nodes:
        if "JobPosting" not in type_names(node):
            continue
        title = _clean_text(node.get("title"), 300)
        if not title:
            continue
        date_posted_raw = _first_str(node.get("datePosted"))
        valid_raw = _first_str(node.get("validThrough"))
        employment = [_clean_text(item, 60) for item in _as_list(node.get("employmentType")) if _clean_text(item, 60)]
        candidates.append({
            "signal_type": "job_posting",
            "platform": "company_site",
            "source_url": source_url,
            "content_sha256": content_sha256,
            "retrieved_at": retrieved_at,
            "acquisition_mode": "permitted_public_page",
            "rights_status": "approved",
            "source_class": "company_owned",
            "title": title,
            "description_excerpt": _strip_html(_first_str(node.get("description"))),
            "date_posted": parse_explicit_date(date_posted_raw),
            "date_posted_raw": date_posted_raw[:60] or None,
            "valid_through": parse_explicit_date(valid_raw),
            "valid_through_raw": valid_raw[:60] or None,
            "employment_type": employment[:5],
            "job_location": _parse_job_location(node.get("jobLocation")),
            "hiring_organization": _parse_hiring_organisation(node.get("hiringOrganization")),
            "extraction": "json_ld_job_posting",
            "organisation_number": None,
            "exact_entity": False,
            "identity_proof": "",
            "expired": None,
            "status": "unknown",
        })
        if len(candidates) >= 10:
            break
    return candidates


def extract_career_page_links(
    soup: BeautifulSoup,
    page_url: str,
    *,
    content_sha256: str | None,
    retrieved_at: str,
) -> list[dict[str, Any]]:
    """Individual posting links on a career listing page (#14).

    Link-only candidates carry no hiring organisation and no dates; they can
    only be verified by an organisation number on the listing page (see
    :func:`verify_job_postings`). Generic navigation text is excluded.
    """
    base = urllib.parse.urlparse(page_url)
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for anchor in soup.select("a[href]"):
        text = _clean_text(anchor.get_text(" ", strip=True), 120)
        if len(text) < 3 or text.casefold().strip(" .!") in _GENERIC_CAREER_LINK_TEXTS:
            continue
        url = urllib.parse.urljoin(page_url, str(anchor.get("href") or "").strip())
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != base.netloc.lower():
            continue
        clean = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))
        if clean.rstrip("/") == page_url.rstrip("/") or clean in seen:
            continue
        path = parsed.path.casefold()
        segments = [part for part in parsed.path.split("/") if part]
        if not any(term in path for term in CAREER_PATH_TERMS) or len(segments) < 2:
            continue
        seen.add(clean)
        candidates.append({
            "signal_type": "job_posting",
            "platform": "company_site",
            "source_url": clean,
            "content_sha256": content_sha256,
            "retrieved_at": retrieved_at,
            "acquisition_mode": "permitted_public_page",
            "rights_status": "approved",
            "source_class": "company_owned",
            "title": text,
            "description_excerpt": "",
            "date_posted": None,
            "date_posted_raw": None,
            "valid_through": None,
            "valid_through_raw": None,
            "employment_type": [],
            "job_location": None,
            "hiring_organization": None,
            "extraction": "career_page_link",
            "organisation_number": None,
            "exact_entity": False,
            "identity_proof": "",
            "expired": None,
            "status": "unknown",
        })
        if len(candidates) >= 10:
            break
    return candidates


def verify_job_postings(
    candidates: list[dict[str, Any]],
    *,
    legal_name: str | None = None,
    organisation_number: str | None = None,
    registered_domain: str | None = None,
    page_texts: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split job candidates into verified vs ambiguous (#14).

    Verified when the hiring organisation matches by name, organisation
    number, or domain, OR the organisation number appears on the source
    page. Ambiguous candidates are returned separately (quarantined, like
    social links) and must not be published as job postings. Expired
    postings (``validThrough`` before the retrieval date) are flagged with
    ``expired: true`` / ``status: "expired"`` — never silently dropped.
    """
    org_digits = re.sub(r"\D", "", str(organisation_number or ""))
    page_texts = page_texts or {}
    context_supplied = bool(legal_name or org_digits or registered_domain)
    verified: list[dict[str, Any]] = []
    unverified: list[dict[str, Any]] = []
    for candidate in candidates:
        item = dict(candidate)
        retrieved_date = parse_explicit_date(str(item.get("retrieved_at") or "")[:10]) or str(item.get("retrieved_at") or "")[:10]
        valid_through = item.get("valid_through")
        if valid_through and retrieved_date:
            try:
                expired = _datetime.date.fromisoformat(valid_through) < _datetime.date.fromisoformat(retrieved_date)
            except ValueError:
                expired = None
            item["expired"] = expired
            item["status"] = "expired" if expired else "open"
        elif valid_through:
            item["status"] = "unknown"
        hiring = item.get("hiring_organization") or {}
        proofs: list[str] = []
        if legal_name and hiring.get("name") and _hiring_name_matches(str(hiring["name"]), legal_name):
            proofs.append("hiringOrganization name matches the legal name")
        if org_digits and hiring.get("identifier") and _digit_runs_match(hiring["identifier"], org_digits):
            proofs.append("hiringOrganization identifier matches the organisation number")
        if org_digits and hiring.get("name") and _digit_runs_match(hiring["name"], org_digits):
            proofs.append("organisation number appears in the hiringOrganization name")
        if registered_domain and hiring.get("url") and _registered_domain(str(hiring["url"])) == registered_domain:
            proofs.append("hiringOrganization URL is on the verified registered domain")
        if org_digits and _digit_runs_match(page_texts.get(item["source_url"], ""), org_digits):
            proofs.append("organisation number appears on the posting page")
        if proofs:
            item["exact_entity"] = True
            item["identity_proof"] = "; ".join(proofs)
            item["verification_status"] = "verified"
            item["organisation_number"] = organisation_number
            verified.append(item)
        else:
            item["exact_entity"] = False
            item["identity_proof"] = ""
            item["verification_status"] = "ambiguous"
            item["verification_reason"] = (
                "identity_context_not_supplied" if not context_supplied else "hiring_organization_unmatched"
            )
            unverified.append(item)
    return verified, unverified
