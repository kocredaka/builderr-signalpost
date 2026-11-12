from __future__ import annotations

import json
import ipaddress
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup
import extruct
import tldextract
import trafilatura

from .evidence import evidence, utc_now
from .site_extract import (
    PRIORITY_TERMS,
    all_structured_nodes,
    build_news_items,
    classify_page_kind,
    compare_site_contacts,
    discover_feed_links,
    extract_career_page_links,
    extract_job_postings_from_nodes,
    extract_page_date,
    extract_site_contacts,
    extract_site_declared_facts,
    extract_structured_organisations,
    parse_feed,
    parse_robots_sitemaps,
    parse_sitemap_xml,
    select_sitemap_candidates,
    type_names,
    verify_job_postings,
)

USER_AGENT = "builderr-signalpost-poc/0.1 (+https://builderr.ai)"
SOCIAL_HOSTS = {
    "linkedin.com": "linkedin",
    "facebook.com": "facebook",
    "instagram.com": "instagram",
    "x.com": "x",
    "twitter.com": "x",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "tiktok.com": "tiktok",
}
# PRIORITY_TERMS is imported from site_extract (single source of truth,
# re-exported here for existing importers). It extends the original kit terms
# with career/news/staff terms; original terms keep their rank positions.

# Bounded-crawl budgets (WAVE-3A, #12): homepage + at most this many
# secondary HTML pages. Sitemap/feed discovery documents are additional
# bounded fetches, counted in run metrics, not in the page budget.
MAX_SECONDARY_PAGES = 8
MAX_SITEMAP_FILES = 10
SITEMAP_MAX_BYTES = 1_000_000
MAX_FEEDS_FETCHED = 2
FEED_MAX_BYTES = 500_000


def assert_public_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not host:
        raise ValueError("Only public HTTP(S) URLs are allowed")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise ValueError("Local hosts are blocked")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise ValueError("Hostname did not resolve") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("Private, loopback, link-local, multicast, and reserved addresses are blocked")


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        assert_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


SAFE_OPENER = urllib.request.build_opener(SafeRedirectHandler())


def normalize_homepage(value: str | None) -> str | None:
    value = str(value or "").strip()
    if not value:
        return None
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))


def _registered_domain(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    ext = tldextract.extract(parsed.hostname or "")
    return ext.top_domain_under_public_suffix


def _robots_url_for(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/robots.txt", "", "", ""))


def _fetch_robots_text(url: str, timeout: float) -> str | None:
    """robots.txt body for the URL's origin, or None when unavailable.

    Fetch failures return None (the historical behaviour treats an absent
    robots file as "no explicit disallow" for the bounded homepage fetch).
    The body is also the source of ``Sitemap:`` directives (#12).
    """
    assert_public_url(url)
    robots_url = _robots_url_for(url)
    try:
        request = urllib.request.Request(robots_url, headers={"User-Agent": USER_AGENT})
        with SAFE_OPENER.open(request, timeout=timeout) as response:
            return response.read(200_000).decode("utf-8", errors="replace")
    except Exception:
        return None


def _robots_allowed(url: str, timeout: float) -> bool:
    text = _fetch_robots_text(url, timeout)
    if text is None:
        # An unavailable robots file is not permission to ignore explicit site terms; callers retain
        # the URL and can route uncertain domains to review. For this bounded homepage POC, allow one
        # ordinary GET when robots.txt is absent rather than crawl deeper.
        return True
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(text.splitlines())
    return parser.can_fetch(USER_AGENT, url)


def _social_links(base_url: str, soup: BeautifulSoup) -> list[dict[str, str]]:
    found: dict[tuple[str, str], dict[str, str]] = {}
    candidates = [str(node.get("href") or "") for node in soup.select("a[href]")]
    candidates.extend(str(node.get("data-href") or "") for node in soup.select("[data-href]"))
    candidates.extend(str(node.get("src") or "") for node in soup.select("iframe[src]"))
    for candidate in candidates:
        url = urllib.parse.urljoin(base_url, candidate)
        parsed_candidate = urllib.parse.urlparse(url)
        if (parsed_candidate.hostname or "").casefold().removeprefix("www.") == "facebook.com" and parsed_candidate.path.startswith("/plugins/"):
            embedded = urllib.parse.parse_qs(parsed_candidate.query).get("href", [])
            if embedded:
                url = embedded[0]
        normalized = normalize_social_url(url)
        if not normalized:
            continue
        found[(normalized["platform"], normalized["url"])] = normalized
    return sorted(found.values(), key=lambda item: (item["platform"], item["url"]))


def structured_social_links(value: Any) -> list[dict[str, str]]:
    found: dict[tuple[str, str], dict[str, str]] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            same_as = node.get("sameAs")
            urls = same_as if isinstance(same_as, list) else [same_as]
            for raw in urls:
                if not isinstance(raw, str):
                    continue
                normalized = normalize_social_url(raw.strip())
                if normalized:
                    found[(normalized["platform"], normalized["url"])] = normalized
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return sorted(found.values(), key=lambda item: (item["platform"], item["url"]))


def normalize_social_url(url: str) -> dict[str, str] | None:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().removeprefix("www.")
    platform = next((label for domain, label in SOCIAL_HOSTS.items() if host == domain or host.endswith("." + domain)), None)
    if not platform:
        return None
    parts = [part.strip() for part in parsed.path.split("/") if part.strip()]
    lowered = [part.casefold() for part in parts]
    rejected_first = {
        "facebook": {"sharer", "sharer.php", "share.php", "dialog", "policy.php", "privacy", "events", "groups", "plugins"},
        "instagram": {"p", "reel", "reels", "stories", "explore"},
        "x": {"intent", "share", "home", "search", "i"},
    }
    if not parts or lowered[0] in rejected_first.get(platform, set()):
        return None
    if platform == "facebook" and lowered[0] == "profile.php":
        return None
    if platform == "linkedin" and (lowered[0] != "company" or len(parts) < 2):
        return None
    if platform == "youtube" and lowered[0] not in {"channel", "user", "c"} and not parts[0].startswith("@"):
        return None
    if host == "youtu.be":
        return None
    if platform == "tiktok" and not parts[0].startswith("@"):
        return None
    if platform == "x" and len(parts) != 1:
        return None
    canonical_host = {
        "linkedin": "linkedin.com",
        "facebook": "facebook.com",
        "instagram": "instagram.com",
        "x": "x.com",
        "youtube": "youtube.com",
        "tiktok": "tiktok.com",
    }[platform]
    if platform == "linkedin":
        parts = parts[:2]
    elif platform == "youtube":
        parts = parts[:1] if parts[0].startswith("@") else parts[:2]
    return {"platform": platform, "url": f"https://{canonical_host}/{'/'.join(parts)}"}


def _priority_links(base_url: str, soup: BeautifulSoup, limit: int = 4) -> list[str]:
    base = urllib.parse.urlparse(base_url)
    candidates: dict[str, int] = {}
    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href") or "").strip()
        url = urllib.parse.urljoin(base_url, href)
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != base.netloc.lower():
            continue
        haystack = (parsed.path + " " + anchor.get_text(" ", strip=True)).casefold()
        rank = next((index for index, term in enumerate(PRIORITY_TERMS) if term in haystack), None)
        if rank is None:
            continue
        clean = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))
        if clean.rstrip("/") == base_url.rstrip("/"):
            continue
        candidates[clean] = min(rank, candidates.get(clean, rank))
    return [url for url, _ in sorted(candidates.items(), key=lambda item: (item[1], item[0]))[:limit]]


def _identity_text_excerpt(soup: BeautifulSoup) -> str:
    """Footer/address/contact-node text: where organisation numbers usually live.

    Shared by the urllib fetcher and the Scrapy event extractor so both crawl
    paths give the identity gate the same evidence.
    """
    identity_nodes = soup.select(
        'footer, address, [itemprop="legalName"], [itemprop="address"], '
        '[itemprop="telephone"], [itemprop="email"]'
    )
    identity_text = " ".join(node.get_text(" ", strip=True) for node in identity_nodes)
    return " ".join(identity_text.split())[:3000]


def _fetch_secondary_page(url: str, *, homepage_domain: str, timeout: float, max_bytes: int, retrieved_at: str) -> tuple[dict[str, Any] | None, list[dict[str, str]], int, int, int, str | None]:
    if not _robots_allowed(url, timeout):
        return None, [], 1, 0, 0, "robots.txt disallows page"
    started = time.monotonic()
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    try:
        with SAFE_OPENER.open(request, timeout=timeout) as response:
            raw = response.read(max_bytes + 1)
            elapsed = int((time.monotonic() - started) * 1000)
            final_url = response.geturl()
            if len(raw) > max_bytes or "html" not in response.headers.get("content-type", "").lower():
                return None, [], 2, len(raw), elapsed, "unsupported or oversized page"
            if _registered_domain(final_url) != homepage_domain:
                return None, [], 2, len(raw), elapsed, "redirected outside registered domain"
        page_html = raw.decode("utf-8", errors="replace")
        page_soup = BeautifulSoup(page_html, "lxml")
        page_text = trafilatura.extract(page_html, url=final_url, include_links=False, include_tables=False, favor_precision=True) or ""
        structured = extruct.extract(page_html, base_url=final_url, syntaxes=["json-ld", "microdata", "rdfa", "opengraph"])
        nodes = all_structured_nodes(structured)
        published_date, date_source = extract_page_date(page_soup, nodes, final_url, page_text)
        page_kind = classify_page_kind(final_url, structured_nodes=nodes)
        content_sha256 = __import__("hashlib").sha256(raw).hexdigest()
        job_candidates = extract_job_postings_from_nodes(
            nodes, source_url=final_url, content_sha256=content_sha256, retrieved_at=retrieved_at,
        )
        if page_kind == "career":
            job_candidates.extend(extract_career_page_links(
                page_soup, final_url, content_sha256=content_sha256, retrieved_at=retrieved_at,
            ))
        page = {
            "url": final_url,
            "title": page_soup.title.get_text(" ", strip=True)[:500] if page_soup.title else "",
            "main_text_excerpt": page_text[:5000],
            "identity_text_excerpt": _identity_text_excerpt(page_soup),
            "content_sha256": content_sha256,
            "published_date": published_date,
            "date_source": date_source,
            "page_kind": page_kind,
            "structured_types": sorted({name for node in nodes for name in type_names(node)})[:20],
            "structured_organisations": extract_structured_organisations(structured),
            "job_posting_candidates": job_candidates,
        }
        return page, _social_links(final_url, page_soup), 2, len(raw), elapsed, None
    except Exception as exc:
        return None, [], 2, 0, int((time.monotonic() - started) * 1000), f"{type(exc).__name__}: {str(exc)[:120]}"


def _jsonld_organisations(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """Structured organisations, expanded (#16): JSON-LD incl. @graph plus
    microdata and RDFa, LocalBusiness subtypes included.

    Kept under the historical name/signature: crawl_events.py imports it.
    JSON-LD-only pages behave exactly as before (original dicts, same order).
    """
    return extract_structured_organisations(metadata)


def _fetch_discovery_document(url: str, *, timeout: float, max_bytes: int, accept: str) -> tuple[bytes | None, int, int, str | None]:
    """Fetch one discovery document (sitemap/feed) under the SSRF guard.

    Returns (body, requests, bytes, error). Robots rules do not govern
    fetching sitemap.xml itself; feed URLs are robots-checked by the caller.
    Oversized bodies are rejected, never truncated silently.
    """
    try:
        assert_public_url(url)
    except ValueError as exc:
        return None, 0, 0, str(exc)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        with SAFE_OPENER.open(request, timeout=timeout) as response:
            raw = response.read(max_bytes + 1)
            if len(raw) > max_bytes:
                return None, 1, len(raw), "discovery document exceeds byte limit"
            if _registered_domain(response.geturl()) != _registered_domain(url):
                return None, 1, len(raw), "redirected outside registered domain"
            return raw, 1, len(raw), None
    except Exception as exc:
        return None, 1, 0, f"{type(exc).__name__}: {str(exc)[:120]}"


def _discover_sitemap_candidates(
    homepage_url: str,
    homepage_domain: str,
    *,
    timeout: float,
    existing_urls: list[str],
    limit: int,
) -> tuple[list[str], dict[str, Any], dict[str, Any]]:
    """Sitemap discovery (#12): robots Sitemap: directives + /sitemap.xml.

    Follows sitemap indexes one level deep (bounded file count). Out-of-domain
    sitemap URLs are rejected, never fetched. Returns (candidates, info, metrics).
    """
    metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
    info: dict[str, Any] = {
        "sitemap_urls_checked": [],
        "sitemap_urls_rejected_out_of_domain": 0,
        "urls_found": 0,
        "candidates_selected": [],
        "errors": [],
    }
    robots_text = _fetch_robots_text(homepage_url, timeout)
    metrics["requests"] += 1
    sitemap_urls: list[str] = []
    for candidate in parse_robots_sitemaps(robots_text):
        if _registered_domain(candidate) != homepage_domain:
            info["sitemap_urls_rejected_out_of_domain"] += 1
            continue
        if candidate not in sitemap_urls:
            sitemap_urls.append(candidate)
    default_sitemap = urllib.parse.urljoin(homepage_url, "/sitemap.xml")
    if default_sitemap not in sitemap_urls:
        sitemap_urls.append(default_sitemap)
    page_urls: list[str] = []
    checked: list[str] = []
    queue = list(sitemap_urls)
    while queue and len(checked) < MAX_SITEMAP_FILES:
        sitemap_url = queue.pop(0)
        if sitemap_url in checked:
            continue
        checked.append(sitemap_url)
        body, requests, nbytes, error = _fetch_discovery_document(
            sitemap_url, timeout=timeout, max_bytes=SITEMAP_MAX_BYTES, accept="application/xml,text/xml,*/*",
        )
        metrics["requests"] += requests
        metrics["bytes"] += nbytes
        if error is not None or body is None:
            info["errors"].append({"url": sitemap_url, "error": error or "empty sitemap"})
            continue
        parsed = parse_sitemap_xml(body, homepage_domain=homepage_domain)
        info["sitemap_urls_rejected_out_of_domain"] += parsed["rejected_out_of_domain"]
        if parsed["error"]:
            info["errors"].append({"url": sitemap_url, "error": parsed["error"]})
            continue
        for nested in parsed["sitemap_urls"]:
            if nested not in checked and nested not in queue:
                queue.append(nested)
        for url in parsed["page_urls"]:
            if url not in page_urls:
                page_urls.append(url)
    info["sitemap_urls_checked"] = checked
    info["urls_found"] = len(page_urls)
    candidates = select_sitemap_candidates(page_urls, existing=existing_urls, limit=limit)
    info["candidates_selected"] = candidates
    return candidates, info, metrics


def _fetch_feed_items(
    feed_urls: list[str],
    homepage_domain: str,
    *,
    timeout: float,
    retrieved_at: str,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Fetch up to MAX_FEEDS_FETCHED same-domain feeds (#12/#15)."""
    metrics = {"requests": 0, "bytes": 0, "latencies_ms": []}
    info: dict[str, Any] = {"feed_urls": list(feed_urls), "feeds_fetched": 0, "items_found": 0, "errors": []}
    items: list[dict[str, Any]] = []
    for feed_url in feed_urls[:MAX_FEEDS_FETCHED]:
        if not _robots_allowed(feed_url, timeout):
            info["errors"].append({"url": feed_url, "error": "robots.txt disallows feed"})
            continue
        body, requests, nbytes, error = _fetch_discovery_document(
            feed_url, timeout=timeout, max_bytes=FEED_MAX_BYTES, accept="application/rss+xml,application/atom+xml,application/xml,text/xml",
        )
        metrics["requests"] += requests + 1  # +1 for the robots.txt fetch above
        metrics["bytes"] += nbytes
        if error is not None or body is None:
            info["errors"].append({"url": feed_url, "error": error or "empty feed"})
            continue
        feed_items, stats = parse_feed(body, feed_url, homepage_domain=homepage_domain, retrieved_at=retrieved_at)
        info["feeds_fetched"] += 1
        info["items_found"] += stats["items_found"]
        if stats["error"]:
            info["errors"].append({"url": feed_url, "error": stats["error"]})
        items.extend(feed_items)
    return items, info, metrics


def _extraction_state(text: str, soup: BeautifulSoup) -> str:
    return "js_fallback_candidate" if len(text.strip()) < 100 and len(soup.select("script[src]")) >= 2 else "static_complete"


def _website_record(status: str, source_type: str, url: str, **kwargs: Any) -> dict[str, Any]:
    """Website evidence record; effective_at is the fetch moment (no server as-of)."""
    record = evidence("website", status, source_type, url, **kwargs)
    record["effective_at"] = record["retrieved_at"]
    return record


def fetch_website(
    url: str | None,
    *,
    timeout: float = 15.0,
    max_bytes: int = 2_000_000,
    legal_name: str | None = None,
    organisation_number: str | None = None,
    registry_contacts: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Crawl a registry-linked company website (bounded, robots-respecting).

    Deep extraction (#12–#16) lands in the website evidence value; the
    generic contract adapter turns each value field into claims, and the
    identity gate (applied by the caller) decides publishability. The
    optional identity context (``legal_name`` / ``organisation_number`` /
    ``registry_contacts``) enables hiring-organisation verification for jobs
    and the site-vs-registry contact comparison; without it, job candidates
    stay quarantined as ambiguous and the comparison reports
    ``registry_contacts_not_supplied``. The batch runner must pass this
    context for those two features to activate (see coordinator note in
    docs/website-evidence-schema.md).
    """
    supplied_url = str(url or "").strip()
    supplied_scheme = bool(re.match(r"^https?://", supplied_url, re.I))
    normalized = normalize_homepage(url)
    if not normalized:
        return _website_record( "not_found", "registry_linked_company_website", "https://data.brreg.no/enhetsregisteret/api/enheter", note="No valid registry website URL"), {"requests": 0, "bytes": 0, "latencies_ms": []}
    try:
        assert_public_url(normalized)
    except ValueError as exc:
        return _website_record( "blocked", "registry_linked_company_website", normalized, note=str(exc)), {"requests": 0, "bytes": 0, "latencies_ms": []}
    if not _robots_allowed(normalized, timeout):
        return _website_record( "blocked", "registry_linked_company_website", normalized, note="robots.txt disallows this user agent"), {"requests": 1, "bytes": 0, "latencies_ms": []}
    started = time.monotonic()
    request = urllib.request.Request(normalized, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    try:
        with SAFE_OPENER.open(request, timeout=timeout) as response:
            content_type = response.headers.get("content-type", "")
            raw = response.read(max_bytes + 1)
            elapsed = int((time.monotonic() - started) * 1000)
            if len(raw) > max_bytes:
                return _website_record( "blocked", "registry_linked_company_website", normalized, note="Homepage exceeds byte limit"), {"requests": 2, "bytes": len(raw), "latencies_ms": [elapsed]}
            if "html" not in content_type.lower():
                return _website_record( "source_error", "registry_linked_company_website", normalized, note=f"Unsupported content type: {content_type}"), {"requests": 2, "bytes": len(raw), "latencies_ms": [elapsed]}
            final_url = response.geturl()
            assert_public_url(final_url)
        html = raw.decode("utf-8", errors="replace")
        retrieved_at = utc_now()
        soup = BeautifulSoup(html, "lxml")
        structured = extruct.extract(html, base_url=final_url, syntaxes=["json-ld", "microdata", "rdfa", "opengraph"])
        structured_nodes = all_structured_nodes(structured)
        text = trafilatura.extract(html, url=final_url, include_links=False, include_tables=False, favor_precision=True) or ""
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        description_tag = soup.select_one('meta[name="description"], meta[property="og:description"]')
        description = str(description_tag.get("content") or "").strip() if description_tag else ""
        structured_organisations = _jsonld_organisations(structured)
        homepage_social = _social_links(final_url, soup)
        combined_social = homepage_social + structured_social_links(structured_organisations)
        homepage_social = list({(item["platform"], item["url"]): item for item in combined_social}.values())
        homepage_domain = _registered_domain(final_url)
        homepage_sha256 = __import__("hashlib").sha256(raw).hexdigest()
        homepage_date, homepage_date_source = extract_page_date(soup, structured_nodes, final_url, text)
        value = {
            "requested_url": normalized,
            "final_url": final_url,
            "registered_domain": homepage_domain,
            "title": title[:500],
            "description": description[:2000],
            "main_text_excerpt": text[:5000],
            "identity_text_excerpt": _identity_text_excerpt(soup),
            "social_links": homepage_social,
            "structured_organisations": structured_organisations,
            "content_sha256": homepage_sha256,
            "extraction_state": _extraction_state(text, soup),
        }
        # #13/#16 contacts and facts are computed after the secondary crawl
        # from structured data merged across all crawled pages (a contact
        # page's microdata counts too); value-level structured_organisations
        # stays homepage-only so the identity gate sees exactly what it did
        # before WAVE-3A.
        pages = [{
            "url": final_url,
            "title": title[:500],
            "main_text_excerpt": text[:5000],
            "identity_text_excerpt": value["identity_text_excerpt"],
            "content_sha256": homepage_sha256,
            "published_date": homepage_date,
            "date_source": homepage_date_source,
            "page_kind": "homepage",
            "structured_types": sorted({name for node in structured_nodes for name in type_names(node)})[:20],
            "structured_organisations": structured_organisations,
            "job_posting_candidates": extract_job_postings_from_nodes(
                structured_nodes, source_url=final_url, content_sha256=homepage_sha256, retrieved_at=retrieved_at,
            ),
        }]
        social = value["social_links"]
        crawl_errors = []
        requests = 2
        bytes_received = len(raw)
        page_latencies = [elapsed]
        # #12: sitemap + feed discovery (bounded; same registered domain only).
        priority_urls = _priority_links(final_url, soup)
        sitemap_candidates, sitemap_info, sitemap_metrics = _discover_sitemap_candidates(
            final_url, homepage_domain, timeout=timeout,
            existing_urls=[final_url, *priority_urls],
            limit=max(0, MAX_SECONDARY_PAGES - len(priority_urls)),
        )
        requests += sitemap_metrics["requests"]
        bytes_received += sitemap_metrics["bytes"]
        value["sitemap_discovery"] = sitemap_info
        feed_urls = discover_feed_links(soup, final_url, homepage_domain)
        feed_items, feed_info, feed_metrics = _fetch_feed_items(
            feed_urls, homepage_domain, timeout=timeout, retrieved_at=retrieved_at,
        )
        requests += feed_metrics["requests"]
        bytes_received += feed_metrics["bytes"]
        value["feed_discovery"] = feed_info
        secondary_urls = list(dict.fromkeys([*priority_urls, *sitemap_candidates]))[:MAX_SECONDARY_PAGES]
        for page_url in secondary_urls:
            page, page_social, page_requests, page_bytes, page_elapsed, page_error = _fetch_secondary_page(
                page_url,
                homepage_domain=homepage_domain,
                timeout=timeout,
                max_bytes=min(max_bytes, 1_000_000),
                retrieved_at=retrieved_at,
            )
            requests += page_requests
            bytes_received += page_bytes
            if page_elapsed:
                page_latencies.append(page_elapsed)
            if page:
                pages.append(page)
                social.extend(page_social)
            elif page_error:
                crawl_errors.append({"url": page_url, "error": page_error})
        value["pages"] = pages
        value["social_links"] = list({(item["platform"], item["url"]): item for item in social}.values())
        value["crawl_errors"] = crawl_errors
        # #13/#16: merge structured organisations across all crawled pages.
        merged_orgs: list[dict[str, Any]] = []
        seen_orgs: set[str] = set()
        for page in pages:
            for org in page.get("structured_organisations") or []:
                key = repr(sorted(org.items(), key=lambda kv: str(kv[0])))
                if key not in seen_orgs:
                    seen_orgs.add(key)
                    merged_orgs.append(org)
        value["site_contacts"] = extract_site_contacts(merged_orgs, soup, source_url=final_url)
        value["site_contact_comparison"] = compare_site_contacts(value["site_contacts"], registry_contacts)
        value["site_declared_facts"] = extract_site_declared_facts(merged_orgs)
        # #14: verify hiring organisations; ambiguous candidates stay quarantined.
        page_texts = {
            str(page.get("url") or ""): " ".join(str(page.get(key) or "") for key in ("title", "main_text_excerpt", "identity_text_excerpt"))
            for page in pages
        }
        all_candidates = [candidate for page in pages for candidate in page.get("job_posting_candidates") or []]
        verified_jobs, unverified_jobs = verify_job_postings(
            all_candidates,
            legal_name=legal_name,
            organisation_number=organisation_number,
            registered_domain=homepage_domain,
            page_texts=page_texts,
        )
        value["site_jobs"] = verified_jobs
        value["site_jobs_unverified"] = unverified_jobs
        # #15: dated site-activity items from pages + feeds.
        value["site_news_items"] = build_news_items(pages, retrieved_at=retrieved_at, feed_items=feed_items)
        return _website_record( "available", "registry_linked_company_website", final_url, value=value, note="Company-controlled claim layer; not an official registry fact", content_sha256=value["content_sha256"], retrieved_at=retrieved_at), {"requests": requests, "bytes": bytes_received, "latencies_ms": page_latencies}
    except urllib.error.HTTPError as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        status = "not_found" if exc.code in {404, 410} else "source_error"
        return _website_record( status, "registry_linked_company_website", normalized, note=f"HTTP {exc.code}"), {"requests": 2, "bytes": 0, "latencies_ms": [elapsed]}
    except urllib.error.URLError as exc:
        if not supplied_scheme and normalized.startswith("https://"):
            first_elapsed = int((time.monotonic() - started) * 1000)
            record, metrics = fetch_website(
                "http://" + supplied_url, timeout=timeout, max_bytes=max_bytes,
                legal_name=legal_name, organisation_number=organisation_number,
                registry_contacts=registry_contacts,
            )
            metrics["requests"] += 2
            metrics["latencies_ms"].insert(0, first_elapsed)
            return record, metrics
        elapsed = int((time.monotonic() - started) * 1000)
        return _website_record( "source_error", "registry_linked_company_website", normalized, note=f"URLError: {str(exc.reason)[:180]}"), {"requests": 2, "bytes": 0, "latencies_ms": [elapsed]}
    except Exception as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        return _website_record( "source_error", "registry_linked_company_website", normalized, note=f"{type(exc).__name__}: {str(exc)[:180]}"), {"requests": 2, "bytes": 0, "latencies_ms": [elapsed]}
