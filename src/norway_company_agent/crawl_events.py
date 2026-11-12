from __future__ import annotations

import hashlib
from typing import Any

import extruct
import trafilatura
from bs4 import BeautifulSoup

from .evidence import evidence, utc_now
from .site_extract import (
    all_structured_nodes,
    build_news_items,
    classify_page_kind,
    compare_site_contacts,
    extract_career_page_links,
    extract_job_postings_from_nodes,
    extract_page_date,
    extract_site_contacts,
    extract_site_declared_facts,
    registry_contacts_from_profile,
    type_names,
    verify_job_postings,
)
from .website import _extraction_state, _identity_text_excerpt, _jsonld_organisations, _registered_domain, _social_links, normalize_homepage, structured_social_links


def extract_page_event(
    *,
    organisation_number: str,
    requested_url: str,
    final_url: str,
    status_code: int,
    content_type: str,
    body: bytes,
    page_kind: str,
    retrieved_at: str | None = None,
) -> dict[str, Any]:
    base = {
        "organisation_number": organisation_number,
        "requested_url": requested_url,
        "final_url": final_url,
        "status_code": status_code,
        "content_type": content_type,
        "page_kind": page_kind,
        "retrieved_at": retrieved_at or utc_now(),
        "bytes": len(body),
        "content_sha256": hashlib.sha256(body).hexdigest(),
    }
    if status_code < 200 or status_code >= 300:
        return {**base, "status": "not_found" if status_code in {404, 410} else "source_error", "error": f"HTTP {status_code}"}
    if "html" not in content_type.casefold():
        return {**base, "status": "source_error", "error": f"Unsupported content type: {content_type}"}
    page_html = body.decode("utf-8", errors="replace")
    soup = BeautifulSoup(page_html, "lxml")
    text = trafilatura.extract(page_html, url=final_url, include_links=False, include_tables=False, favor_precision=True) or ""
    title = soup.title.get_text(" ", strip=True)[:500] if soup.title else ""
    description_tag = soup.select_one('meta[name="description"], meta[property="og:description"]')
    description = str(description_tag.get("content") or "").strip()[:2000] if description_tag else ""
    identity_text = _identity_text_excerpt(soup)
    structured = extruct.extract(page_html, base_url=final_url, syntaxes=["json-ld", "microdata", "rdfa", "opengraph"])
    structured_nodes = all_structured_nodes(structured)
    published_date, date_source = extract_page_date(soup, structured_nodes, final_url, text)
    page_kind_value = classify_page_kind(final_url, structured_nodes=structured_nodes, is_homepage=(page_kind == "homepage"))
    content_sha256 = hashlib.sha256(body).hexdigest()
    job_candidates = extract_job_postings_from_nodes(
        structured_nodes, source_url=final_url, content_sha256=content_sha256, retrieved_at=base["retrieved_at"],
    )
    if page_kind_value == "career":
        job_candidates.extend(extract_career_page_links(
            soup, final_url, content_sha256=content_sha256, retrieved_at=base["retrieved_at"],
        ))
    event = {
        **base,
        "status": "available",
        "title": title,
        "description": description,
        "main_text_excerpt": text[:5000],
        "identity_text_excerpt": identity_text,
        "social_links": _social_links(final_url, soup),
        "extraction_state": _extraction_state(text, soup),
        "published_date": published_date,
        "date_source": date_source,
        "page_kind_value": page_kind_value,
        "structured_types": sorted({name for node in structured_nodes for name in type_names(node)})[:20],
        "structured_organisations": _jsonld_organisations(structured),
        "job_posting_candidates": job_candidates,
    }
    if page_kind == "homepage":
        combined_social = event["social_links"] + structured_social_links(event["structured_organisations"])
        event["social_links"] = list({(item["platform"], item["url"]): item for item in combined_social}.values())
        event["registered_domain"] = _registered_domain(final_url)
    return event


def error_page_event(
    *,
    organisation_number: str,
    requested_url: str,
    page_kind: str,
    error: str,
) -> dict[str, Any]:
    return {
        "organisation_number": organisation_number,
        "requested_url": requested_url,
        "final_url": requested_url,
        "status_code": 0,
        "content_type": "",
        "page_kind": page_kind,
        "retrieved_at": utc_now(),
        "bytes": 0,
        "content_sha256": hashlib.sha256(b"").hexdigest(),
        "status": "source_error",
        "error": error[:240],
    }


def missing_seed_error_events(profiles: list[dict[str, Any]], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Give every crawlable seed a terminal ledger entry, including robots rejections."""
    event_orgs = {str(event.get("organisation_number") or "") for event in events}
    missing = []
    for profile in profiles:
        organisation_number = str(profile.get("organisation_number") or "")
        if not profile.get("website") or organisation_number in event_orgs:
            continue
        requested_url = normalize_homepage(str(profile["website"]))
        if requested_url:
            missing.append(error_page_event(
                organisation_number=organisation_number,
                requested_url=requested_url,
                page_kind="homepage",
                error="No page event emitted; request was rejected before download (for example by robots.txt).",
            ))
    return missing


def _website_record(status: str, source_type: str, source_url: str, **kwargs: Any) -> dict[str, Any]:
    """Website evidence record; effective_at is the fetch moment (no server as-of)."""
    record = evidence("website", status, source_type, source_url, **kwargs)
    record["effective_at"] = record["retrieved_at"]
    return record


def merge_profile_events(profile: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    homepage_events = [item for item in events if item.get("page_kind") == "homepage"]
    homepage_events.sort(key=lambda item: item.get("retrieved_at") or "")
    homepage = homepage_events[-1] if homepage_events else None
    if not homepage:
        return _website_record(
            "not_found",
            "registry_linked_company_website_scrapy",
            str(profile.get("website") or "https://data.brreg.no/enhetsregisteret/api/enheter"),
            note="No homepage crawl event was produced",
        )
    if homepage.get("status") != "available":
        return _website_record(
            homepage.get("status", "source_error"),
            "registry_linked_company_website_scrapy",
            homepage.get("final_url") or homepage.get("requested_url"),
            note=homepage.get("error") or "Homepage fetch failed",
            retrieved_at=homepage.get("retrieved_at"),
            content_sha256=homepage.get("content_sha256"),
        )
    available = [item for item in events if item.get("status") == "available"]
    unique_pages = {}
    social = {}
    for item in sorted(available, key=lambda value: (value.get("page_kind") != "homepage", value.get("final_url") or "")):
        page = {
            "url": item.get("final_url"),
            "title": item.get("title") or "",
            "main_text_excerpt": item.get("main_text_excerpt") or "",
            "identity_text_excerpt": item.get("identity_text_excerpt") or "",
            "content_sha256": item.get("content_sha256"),
            "published_date": item.get("published_date"),
            "date_source": item.get("date_source"),
            "page_kind": item.get("page_kind_value") or ("homepage" if item.get("page_kind") == "homepage" else "other"),
            "structured_types": item.get("structured_types") or [],
            "structured_organisations": item.get("structured_organisations") or [],
            "job_posting_candidates": item.get("job_posting_candidates") or [],
        }
        unique_pages[item.get("final_url")] = page
        for link in item.get("social_links") or []:
            social[(link.get("platform"), link.get("url"))] = link
    pages = list(unique_pages.values())
    structured_organisations = homepage.get("structured_organisations") or []
    # Contacts/facts merge structured organisations across all crawled pages
    # (a contact page's microdata counts too); value-level
    # structured_organisations stays homepage-only for the identity gate.
    merged_orgs: list[dict[str, Any]] = []
    seen_orgs: set[str] = set()
    for page in pages:
        for org in page.get("structured_organisations") or []:
            key = repr(sorted(org.items(), key=lambda kv: str(kv[0])))
            if key not in seen_orgs:
                seen_orgs.add(key)
                merged_orgs.append(org)
    # Contacts from structured data only: the merge step has no parsed soup,
    # so footer contacts are unavailable on the Scrapy path (urllib path has
    # them). Provenance in site_contacts says which sources contributed.
    site_contacts = extract_site_contacts(merged_orgs, None, source_url=homepage.get("final_url"))
    page_texts = {
        str(page.get("url") or ""): " ".join(str(page.get(key) or "") for key in ("title", "main_text_excerpt", "identity_text_excerpt"))
        for page in pages
    }
    all_candidates = [candidate for page in pages for candidate in page.get("job_posting_candidates") or []]
    verified_jobs, unverified_jobs = verify_job_postings(
        all_candidates,
        legal_name=str(profile.get("name") or "") or None,
        organisation_number=str(profile.get("organisation_number") or "") or None,
        registered_domain=homepage.get("registered_domain"),
        page_texts=page_texts,
    )
    value = {
        "requested_url": homepage.get("requested_url"),
        "final_url": homepage.get("final_url"),
        "registered_domain": homepage.get("registered_domain"),
        "title": homepage.get("title") or "",
        "description": homepage.get("description") or "",
        "main_text_excerpt": homepage.get("main_text_excerpt") or "",
        "identity_text_excerpt": homepage.get("identity_text_excerpt") or "",
        "social_links": list(social.values()),
        "structured_organisations": structured_organisations,
        "content_sha256": homepage.get("content_sha256"),
        "extraction_state": homepage.get("extraction_state"),
        "site_contacts": site_contacts,
        "site_contact_comparison": compare_site_contacts(site_contacts, registry_contacts_from_profile(profile)),
        "site_declared_facts": extract_site_declared_facts(merged_orgs),
        "site_jobs": verified_jobs,
        "site_jobs_unverified": unverified_jobs,
        "site_news_items": build_news_items(pages, retrieved_at=homepage.get("retrieved_at") or utc_now()),
        "sitemap_discovery": {
            "status": "not_performed_in_scrapy_path",
            "note": "Sitemap/feed discovery lives in the urllib fetcher; the Scrapy scheduler follows priority links only.",
            "sitemap_urls_checked": [],
            "candidates_selected": [],
        },
        "feed_discovery": {
            "status": "not_performed_in_scrapy_path",
            "feed_urls": [],
            "feeds_fetched": 0,
            "items_found": 0,
            "errors": [],
        },
        "pages": pages,
        "crawl_errors": [
            {"url": item.get("final_url") or item.get("requested_url"), "error": item.get("error")}
            for item in events
            if item.get("status") != "available"
        ],
        "scheduler": "scrapy_resumable_v1",
    }
    return _website_record(
        "available",
        "registry_linked_company_website_scrapy",
        homepage.get("final_url"),
        value=value,
        note="Company-controlled claim layer; not an official registry fact",
        retrieved_at=homepage.get("retrieved_at"),
        content_sha256=homepage.get("content_sha256"),
    )
