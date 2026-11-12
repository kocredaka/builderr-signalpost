"""Wikidata lookup by Norwegian organisation number (AUDIT #17, WAVE-3B).

SPARQL queries against the Wikidata Query Service (WDQS), keyed on
property P2333 ("Norwegian organisation number"). Each query uses
``VALUES`` over up to 200 organisation numbers (chunked, so the GET URL
stays short); a 100-company run costs exactly one WDQS request. Data is
CC0; no API key is used.

What is fetched per matched item: QID, label/description, official website
(P856), inception (P571), headquarters location (P159), industry (P452),
social-platform IDs where present (YouTube P2397, X P2002, Instagram P2003,
Facebook P2013, LinkedIn P4264, TikTok P7085), and Wikipedia sitelinks.

Precision rules (wrong-company matches are disqualifying, so every rule
fails closed):

- Exact P2333 match only. No name search is ever performed.
- More than one distinct QID for one organisation number -> ambiguous:
  rejected, no Wikidata claims are published for that company.
- The Wikidata label must share at least one distinctive normalized token
  with the registry legal name; otherwise the match is rejected. Absence of
  a usable label is also a rejection, not an acceptance on P2333 alone.
- An official website from Wikidata is only a *candidate*. It is never
  published as website evidence on its own: the batch runner fetches it
  with the normal website fetcher and it must pass the identity gate
  before it replaces a missing/dead registry website (see
  ``scripts/run_competition_batch.py``).
- Absence in Wikidata is abstention, not evidence the company does not
  exist: most small Norwegian companies have no Wikidata item.

Evidence module name: ``wikidata``. The generic contract adapter maps it
to claims without any adapter change; each available record carries the
QID, its chunk's query URL, ``retrieved_at``, and the SHA-256 of its
chunk's raw WDQS response bytes.

Ambiguity reporting limit: the internal evidence vocabulary has no
``ambiguous`` status, so ambiguous and name-rejected matches are recorded
as ``not_found`` with the reason and the QID(s) in the record note. The
contract adapter therefore emits a single ``not_available`` claim and no
QID-bearing claim leaks. A faithful ``ambiguous`` status would need a
change to ``contract.py``/``batch.py`` (outside WAVE-3B ownership);
see the coordinator notes in the final report.

Degradation: any WDQS failure (rate limiting, HTTP error, timeout, bad
payload) produces per-company ``blocked``/``source_error`` records and the
run continues. ``fetch_wikidata_batch`` never raises. No claim is ever
emitted without a successful fetch behind it.

WDQS etiquette: one query per run, an identifying User-Agent (a generic
browser UA is rejected with 403), ``Accept: application/sparql-results+json``,
a 60s timeout, at most one retry with a polite delay that honours
``Retry-After`` (capped at 30s).

Batch size: the query travels as one GET URL per chunk of at most 200
organisation numbers, so arbitrarily large batches degrade to several
sequential queries instead of one over-long URL. A chunk that fails
degrades only its own companies.
"""

from __future__ import annotations

import email.utils
import hashlib
import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import timezone
from typing import Any, Callable

from .evidence import evidence, utc_now

WDQS_ENDPOINT = "https://query.wikidata.org/sparql"
USER_AGENT = "builderr-signalpost-poc/0.1 (+https://builderr.ai)"
CONCAT_SEPARATOR = "|||"
PAIR_SEPARATOR = "|"
MAX_SITELINKS = 10
MAX_ORGS_PER_QUERY = 200
DEFAULT_TIMEOUT = 60.0
DEFAULT_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 5.0
MAX_RETRY_AFTER_SECONDS = 30.0

# Mirrors the legal/generic stripping in identity._tokens (duplicated here so
# this module does not depend on a private helper in another module).
_LEGAL_AND_GENERIC = {
    "as", "asa", "ans", "da", "enk", "iks", "sa", "sam", "sti", "stiftelsen",
    "nuf", "ab", "b", "v", "limited", "ltd", "inc", "plc", "the", "og", "and",
}

_SOCIAL_PROPERTIES = (
    ("youtube_channel_id", "P2397"),
    ("x_username", "P2002"),
    ("instagram_username", "P2003"),
    ("facebook_id", "P2013"),
    ("linkedin_company_id", "P4264"),
    ("tiktok_username", "P7085"),
)


def _name_tokens(value: Any) -> list[str]:
    text = str(value or "").translate(str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    return [token for token in re.findall(r"[a-z0-9]+", text) if token not in _LEGAL_AND_GENERIC and len(token) > 1]


def verify_name(legal_name: str | None, wikidata_label: str | None) -> tuple[bool, dict[str, Any]]:
    """Cross-check a Wikidata label against the registry legal name.

    Accepts when at least one distinctive normalized token is shared. This
    is a sanity guard on top of the exact P2333 key, meant to catch gross
    Wikidata data errors (a P2333 value attached to the wrong item), not a
    full identity proof; the P2333 key itself carries the match.
    """
    legal_tokens = sorted(set(_name_tokens(legal_name)))
    label_tokens = sorted(set(_name_tokens(wikidata_label)))
    matched = sorted(set(legal_tokens) & set(label_tokens))
    detail = {
        "legal_name": legal_name,
        "wikidata_label": wikidata_label,
        "matched_tokens": matched,
        "method": "normalized_token_overlap_v1",
    }
    if not legal_tokens or not label_tokens:
        return False, {**detail, "reason": "no distinctive tokens to compare"}
    if not matched:
        return False, {**detail, "reason": "no shared distinctive token"}
    return True, detail


def build_sparql_query(organisation_numbers: list[str]) -> str:
    """One SPARQL query for the whole batch (VALUES over P2333 keys)."""
    orgs = [str(org) for org in organisation_numbers]
    for org in orgs:
        if not re.fullmatch(r"\d{9}", org):
            raise ValueError(f"Invalid Norwegian organisation number: {org!r}")
    if not orgs:
        raise ValueError("At least one organisation number is required")
    values = " ".join(f'"{org}"' for org in sorted(set(orgs)))
    social_selects = " ".join(
        f'(GROUP_CONCAT(DISTINCT STR(?{name}); separator="{CONCAT_SEPARATOR}") AS ?{name})'
        for name, _ in _SOCIAL_PROPERTIES
    )
    social_optionals = "\n  ".join(
        f"OPTIONAL {{ ?item wdt:{prop} ?{name}. }}"
        for name, prop in _SOCIAL_PROPERTIES
    )
    return f"""SELECT ?org ?item (SAMPLE(?itemLabel) AS ?label) (SAMPLE(?itemDescription) AS ?description)
  (GROUP_CONCAT(DISTINCT STR(?site); separator="{CONCAT_SEPARATOR}") AS ?sites)
  (GROUP_CONCAT(DISTINCT STR(?inception); separator="{CONCAT_SEPARATOR}") AS ?inceptions)
  (GROUP_CONCAT(DISTINCT CONCAT(STR(?hq), "{PAIR_SEPARATOR}", STR(?hqLabel)); separator="{CONCAT_SEPARATOR}") AS ?hqs)
  (GROUP_CONCAT(DISTINCT CONCAT(STR(?ind), "{PAIR_SEPARATOR}", STR(?indLabel)); separator="{CONCAT_SEPARATOR}") AS ?industries)
  {social_selects}
  (GROUP_CONCAT(DISTINCT STR(?sitelink); separator="{CONCAT_SEPARATOR}") AS ?sitelinks)
WHERE {{
  VALUES ?org {{ {values} }}
  ?item wdt:P2333 ?org.
  OPTIONAL {{ ?item wdt:P856 ?site. }}
  OPTIONAL {{ ?item wdt:P571 ?inception. }}
  OPTIONAL {{ ?item wdt:P159 ?hq. }}
  OPTIONAL {{ ?item wdt:P452 ?ind. }}
  {social_optionals}
  OPTIONAL {{ ?sitelink schema:about ?item. FILTER(CONTAINS(STR(?sitelink), "wikipedia.org")) }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "nb,nn,en". }}
}}
GROUP BY ?org ?item"""


def build_query_url(query: str) -> str:
    params = urllib.parse.urlencode({"query": query, "format": "json"})
    return f"{WDQS_ENDPOINT}?{params}"


def _qid_from_uri(uri: str) -> str | None:
    match = re.search(r"(Q\d+)$", uri.strip())
    return match.group(1) if match else None


def _split_concat(raw: str | None) -> list[str]:
    if not raw:
        return []
    return sorted({part.strip() for part in raw.split(CONCAT_SEPARATOR) if part.strip()})


def _pairs_to_items(raw: str | None) -> list[dict[str, str | None]]:
    """'QID|label' pairs -> [{qid, label}], deduplicated by QID (first label wins)."""
    by_qid: dict[str, str | None] = {}
    for part in _split_concat(raw):
        uri, _, label = part.partition(PAIR_SEPARATOR)
        qid = _qid_from_uri(uri)
        if qid is None:
            continue
        by_qid.setdefault(qid, label.strip() or None)
    return [{"qid": qid, "label": by_qid[qid]} for qid in sorted(by_qid)]


def _binding_value(binding: dict[str, Any], var: str) -> str | None:
    cell = binding.get(var) or {}
    value = cell.get("value")
    return str(value) if value not in (None, "") else None


def _sitelinks_sorted(urls: list[str]) -> list[str]:
    def rank(url: str) -> tuple[int, str]:
        host = urllib.parse.urlparse(url).hostname or ""
        if host.startswith("nb."):
            return (0, url)
        if host.startswith("nn."):
            return (1, url)
        if host.startswith("en."):
            return (2, url)
        return (3, url)

    return sorted(urls, key=rank)


def parse_sparql_response(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Group WDQS JSON bindings by organisation number (one item per row).

    The query groups by (?org, ?item), so each binding is one distinct item.
    Returns {org_number: [item, ...]} with items sorted by QID.
    """
    bindings = ((payload.get("results") or {}).get("bindings") or [])
    if not isinstance(bindings, list):
        raise ValueError("Unexpected SPARQL response shape: results.bindings is not a list")
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        org = _binding_value(binding, "org")
        item_uri = _binding_value(binding, "item")
        if not org or not item_uri:
            continue
        qid = _qid_from_uri(item_uri)
        if qid is None:
            continue
        sitelinks = _split_concat(_binding_value(binding, "sitelinks"))
        social_ids = {
            name: _split_concat(_binding_value(binding, name)) or None
            for name, _ in _SOCIAL_PROPERTIES
        }
        item = {
            "qid": qid,
            "item_url": f"https://www.wikidata.org/wiki/{qid}",
            "label": _binding_value(binding, "label"),
            "description": _binding_value(binding, "description"),
            "official_websites": _split_concat(_binding_value(binding, "sites")) or None,
            # Inception strings are verbatim WDQS time values; day/month may
            # reflect precision padding (e.g. year-only precision renders as
            # 1 January). They are reported as returned, never reinterpreted.
            "inception": _split_concat(_binding_value(binding, "inceptions")) or None,
            "headquarters": _pairs_to_items(_binding_value(binding, "hqs")) or None,
            "industries": _pairs_to_items(_binding_value(binding, "industries")) or None,
            "social_ids": {key: value for key, value in social_ids.items() if value} or None,
            "wikipedia_urls": _sitelinks_sorted(sitelinks)[:MAX_SITELINKS] or None,
            "wikipedia_url_count": len(sitelinks),
        }
        grouped.setdefault(org, {})[qid] = item
    return {org: [items[qid] for qid in sorted(items)] for org, items in sorted(grouped.items())}


def _header_lookup(headers: dict[str, str], name: str) -> str | None:
    lowered = {str(key).lower(): value for key, value in (headers or {}).items()}
    value = lowered.get(name.lower())
    return str(value) if value not in (None, "") else None


def _effective_at_from_headers(headers: dict[str, str]) -> str | None:
    raw = _header_lookup(headers, "last-modified") or _header_lookup(headers, "date")
    if not raw:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _http_get(url: str, headers: dict[str, str], *, timeout: float, max_bytes: int) -> tuple[int, bytes, dict[str, str]]:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(max_bytes + 1)
            response_headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
            return response.status, raw, response_headers
    except urllib.error.HTTPError as exc:
        raw = exc.read(max_bytes + 1)
        response_headers = {str(key).lower(): str(value) for key, value in (exc.headers or {}).items()}
        return exc.code, raw, response_headers


def _retry_delay(headers: dict[str, str]) -> float:
    raw = (_header_lookup(headers, "retry-after") or "").strip()
    if raw.isdigit():
        return min(float(raw), MAX_RETRY_AFTER_SECONDS)
    return RETRY_DELAY_SECONDS


def _record(
    org: str,
    status: str,
    query_url: str,
    *,
    value: Any = None,
    note: str | None = None,
    content_sha256: str | None = None,
    retrieved_at: str | None = None,
    effective_at: str | None = None,
) -> dict[str, Any]:
    return evidence(
        "wikidata",
        status,  # type: ignore[arg-type]
        "wikidata_sparql",
        query_url,
        value=value,
        note=note,
        content_sha256=content_sha256,
        retrieved_at=retrieved_at,
        effective_at=effective_at,
        source_row_key=org,
    )


def _failure_records(
    orgs: list[str],
    query_url: str,
    status: str,
    note: str,
    retrieved_at: str,
) -> dict[str, dict[str, Any]]:
    return {org: _record(org, status, query_url, note=note, retrieved_at=retrieved_at) for org in orgs}


def _fetch_wikidata_chunk(
    profiles: list[dict[str, Any]],
    *,
    fetcher: Callable[..., tuple[int, bytes, dict[str, str]]] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    attempts: int = DEFAULT_ATTEMPTS,
    max_bytes: int = 10_000_000,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Run one WDQS query for a chunk of profiles; build one record each.

    ``profiles`` supplies organisation numbers and registry legal names (for
    the name cross-check). Returns ({org: evidence record}, metrics).
    Never raises: every failure mode yields per-company ``blocked`` or
    ``source_error`` records so the batch run continues.
    """
    orgs = [str(profile.get("organisation_number") or "") for profile in profiles]
    names = {str(profile.get("organisation_number") or ""): profile.get("name") for profile in profiles}
    metrics: dict[str, Any] = {"requests": 0, "bytes": 0, "latencies_ms": [], "query_url": None}
    try:
        query = build_sparql_query(orgs)
    except ValueError as exc:
        retrieved_at = utc_now()
        return _failure_records(orgs, WDQS_ENDPOINT, "source_error", str(exc), retrieved_at), metrics
    query_url = build_query_url(query)
    metrics["query_url"] = query_url
    headers = {"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"}
    fetch = fetcher or _http_get
    last_error = "request failed"
    last_status: int | None = None
    raw: bytes = b""
    response_headers: dict[str, str] = {}
    retrieved_at = utc_now()
    for attempt in range(max(1, attempts)):
        started = time.monotonic()
        try:
            last_status, raw, response_headers = fetch(query_url, headers, timeout=timeout, max_bytes=max_bytes)
            metrics["requests"] += 1
            metrics["bytes"] += len(raw)
            metrics["latencies_ms"].append(int((time.monotonic() - started) * 1000))
            retrieved_at = utc_now()
        except Exception as exc:  # network error, timeout, DNS: retry once, then fail soft
            metrics["requests"] += 1
            metrics["latencies_ms"].append(int((time.monotonic() - started) * 1000))
            last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
            last_status = None
            if attempt + 1 < max(1, attempts):
                sleeper(RETRY_DELAY_SECONDS)
            continue
        if last_status == 200:
            break
        last_error = f"HTTP {last_status}"
        if last_status in {429, 503} and attempt + 1 < max(1, attempts):
            sleeper(_retry_delay(response_headers))
            continue
        break
    if last_status != 200:
        status = "blocked" if last_status in {429, 503} else "source_error"
        note = (
            f"Wikidata Query Service {last_error} after {metrics['requests']} attempt(s); "
            "no Wikidata claims published for this batch."
        )
        if status == "blocked":
            note = "Rate-limited or temporarily unavailable: " + note
        return _failure_records(orgs, query_url, status, note, retrieved_at), metrics
    if len(raw) > max_bytes:
        return _failure_records(
            orgs, query_url, "source_error",
            "WDQS response exceeded the byte limit; no Wikidata claims published.", retrieved_at,
        ), metrics
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return _failure_records(
            orgs, query_url, "source_error",
            f"Unparseable WDQS response ({type(exc).__name__}); no Wikidata claims published.", retrieved_at,
        ), metrics
    try:
        grouped = parse_sparql_response(payload)
    except ValueError as exc:
        return _failure_records(
            orgs, query_url, "source_error",
            f"Unexpected SPARQL response shape ({exc}); no Wikidata claims published.", retrieved_at,
        ), metrics
    response_sha256 = hashlib.sha256(raw).hexdigest()
    effective_at = _effective_at_from_headers(response_headers)
    records: dict[str, dict[str, Any]] = {}
    for org in orgs:
        items = grouped.get(org, [])
        if not items:
            records[org] = _record(
                org, "not_found", query_url,
                note=(
                    f"No Wikidata item with P2333 '{org}' was returned. Absence in "
                    "Wikidata is abstention, not evidence the company does not exist."
                ),
                content_sha256=response_sha256,
                retrieved_at=retrieved_at,
                effective_at=effective_at,
            )
            continue
        if len(items) > 1:
            qids = ", ".join(item["qid"] for item in items)
            records[org] = _record(
                org, "not_found", query_url,
                note=(
                    f"Ambiguous: {len(items)} distinct Wikidata items claim P2333 "
                    f"'{org}' ({qids}); rejected, no Wikidata claims published."
                ),
                content_sha256=response_sha256,
                retrieved_at=retrieved_at,
                effective_at=effective_at,
            )
            continue
        item = items[0]
        name_ok, name_detail = verify_name(names.get(org), item["label"])
        if not name_ok:
            records[org] = _record(
                org, "not_found", query_url,
                note=(
                    f"Name check failed for {item['qid']} (label {item['label']!r} vs "
                    f"legal name {names.get(org)!r}: {name_detail.get('reason')}); "
                    "rejected, no Wikidata claims published."
                ),
                content_sha256=response_sha256,
                retrieved_at=retrieved_at,
                effective_at=effective_at,
            )
            continue
        value = {
            "organisation_number": org,
            "qid": item["qid"],
            "item_url": item["item_url"],
            "label": item["label"],
            "description": item["description"],
            "name_match": name_detail,
            "official_websites": item["official_websites"],
            "inception": item["inception"],
            "headquarters": item["headquarters"],
            "industries": item["industries"],
            "social_ids": item["social_ids"],
            "wikipedia_urls": item["wikipedia_urls"],
            "wikipedia_url_count": item["wikipedia_url_count"],
            "query_url": query_url,
            "response_sha256": response_sha256,
        }
        records[org] = _record(
            org, "available", query_url,
            value=value,
            note="Wikidata (CC0) via exact P2333 match; official website is a candidate only until the identity gate passes.",
            content_sha256=response_sha256,
            retrieved_at=retrieved_at,
            effective_at=effective_at,
        )
    return records, metrics


def fetch_wikidata_batch(
    profiles: list[dict[str, Any]],
    *,
    fetcher: Callable[..., tuple[int, bytes, dict[str, str]]] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    attempts: int = DEFAULT_ATTEMPTS,
    max_bytes: int = 10_000_000,
    sleeper: Callable[[float], None] = time.sleep,
    max_orgs_per_query: int = MAX_ORGS_PER_QUERY,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Run the batch WDQS lookup, chunked so the query URL stays short.

    ``profiles`` supplies organisation numbers and registry legal names (for
    the name cross-check). Returns ({org: evidence record}, metrics).
    Never raises: every failure mode yields per-company ``blocked`` or
    ``source_error`` records so the batch run continues. Batches larger
    than ``max_orgs_per_query`` run as several sequential queries (one per
    chunk, serially, before enrichment); each record carries its own
    chunk's query URL and response hash. A chunk that fails degrades only
    its own companies.
    """
    orgs = [str(profile.get("organisation_number") or "") for profile in profiles]
    metrics: dict[str, Any] = {"requests": 0, "bytes": 0, "latencies_ms": [], "query_url": None, "chunks": 0}
    try:
        build_sparql_query(orgs)
    except ValueError as exc:
        retrieved_at = utc_now()
        return _failure_records(orgs, WDQS_ENDPOINT, "source_error", str(exc), retrieved_at), metrics
    records: dict[str, dict[str, Any]] = {}
    chunk_size = max(1, max_orgs_per_query)
    for start in range(0, len(profiles), chunk_size):
        chunk_records, chunk_metrics = _fetch_wikidata_chunk(
            profiles[start:start + chunk_size],
            fetcher=fetcher, timeout=timeout, attempts=attempts,
            max_bytes=max_bytes, sleeper=sleeper,
        )
        records.update(chunk_records)
        metrics["requests"] += chunk_metrics["requests"]
        metrics["bytes"] += chunk_metrics["bytes"]
        metrics["latencies_ms"].extend(chunk_metrics["latencies_ms"])
        metrics["chunks"] += 1
        if metrics["query_url"] is None:
            metrics["query_url"] = chunk_metrics["query_url"]
    return records, metrics


def wikidata_website_candidates(record: dict[str, Any] | None) -> list[str]:
    """Official websites from an available Wikidata record, else [].

    Candidates only: the caller must fetch each URL and pass the identity
    gate before any website claim is published.
    """
    if not record or record.get("status") != "available":
        return []
    value = record.get("value") or {}
    sites = value.get("official_websites") or []
    return [str(site).strip() for site in sites if str(site).strip()]
