"""WAVE-3A website deepening (#12–#16): sitemap/feed discovery, site contacts,
site jobs, dated site news, and wider structured data.

All network access is mocked (FakeOpener + patched SSRF guard); no test
depends on a live site. Live probes, if any, are reported separately by the
worker and never run in this suite.
"""

from __future__ import annotations

import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import extruct  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402
from norway_company_agent import site_extract, website  # noqa: E402
from norway_company_agent.contract import contract_envelope, validate_contract_envelope  # noqa: E402
from norway_company_agent.crawl_events import extract_page_event, merge_profile_events  # noqa: E402
from norway_company_agent.evidence import evidence as evidence_record  # noqa: E402
from norway_company_agent.identity import apply_website_identity_gate  # noqa: E402
from norway_company_agent.website import _priority_links, fetch_website  # noqa: E402

ORG = "923609016"
NAME = "Fjord Bakeri AS"
DOMAIN = "https://fjordbakeri.no"

HOMEPAGE_HTML = """<html><head><title>Fjord Bakeri AS — Bakeri i Oslo</title>
<meta name="description" content="Fjord Bakeri AS baker brød og kaker i Oslo siden 1998.">
<link rel="alternate" type="application/rss+xml" href="/feed.xml">
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[
{"@type":"Organization","name":"Fjord Bakeri AS","legalName":"Fjord Bakeri AS","vatID":"NO 923 609 016 MVA",
 "telephone":"+47 22 41 12 00","email":"post@fjordbakeri.no","url":"https://fjordbakeri.no/",
 "address":{"@type":"PostalAddress","streetAddress":"Storgata 1","postalCode":"0155","addressLocality":"Oslo"},
 "openingHours":"Mo-Fr 07:00-17:00",
 "contactPoint":{"@type":"ContactPoint","contactType":"customer service","telephone":"+47 22 41 12 00","email":"post@fjordbakeri.no"},
 "foundingDate":"1998-05-01","numberOfEmployees":12,
 "aggregateRating":{"@type":"AggregateRating","ratingValue":"4.5","reviewCount":"20"}},
{"@type":"JobPosting","title":"Baker","datePosted":"2026-11-01","validThrough":"2026-12-01",
 "hiringOrganization":{"@type":"Organization","name":"Fjord Bakeri AS"},
 "jobLocation":{"@type":"Place","address":{"@type":"PostalAddress","addressLocality":"Oslo"}}},
{"@type":"JobPosting","title":"Utløpt stilling","datePosted":"2025-11-01","validThrough":"2026-01-01",
 "hiringOrganization":{"@type":"Organization","name":"Fjord Bakeri AS"}}
]}</script></head>
<body><main><p>Fjord Bakeri AS is a bakery in Oslo. Fjord Bakeri AS has baked bread,
cakes and pastries for the people of Oslo every single morning since 1998, using
stone ovens and Norwegian grain from local farms around the fjord.</p></main>
<nav><a href="/kontakt">Kontakt</a><a href="/karriere">Karriere</a>
<a href="/nyheter">Nyheter</a><a href="/om-oss">Om oss</a></nav>
<footer><address>Storgata 1, 0155 Oslo — Org.nr. 923 609 016 — <a href="tel:+4722411200">22 41 12 00</a> — <a href="mailto:post@fjordbakeri.no">post@fjordbakeri.no</a></address></footer>
</body></html>"""

KARRIERE_HTML = """<html><head><title>Ledige stillinger — Fjord Bakeri AS</title>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"JobPosting",
"title":"Konditor","datePosted":"2026-11-10",
"hiringOrganization":{"@type":"Organization","name":"Fjord Bakeri AS","url":"https://fjordbakeri.no/"}}</script>
</head><body><main><h1>Ledige stillinger</h1>
<p>Vi søker konditor og baker til vårt bakeri i Oslo. Fjord Bakeri AS vokser.</p>
<ul><li><a href="/karriere/baker-oslo">Baker Oslo</a></li></ul></main>
<footer>Org.nr. 923 609 016</footer></body></html>"""

KONTAKT_HTML = """<html><head><title>Kontakt — Fjord Bakeri AS</title></head><body>
<div itemscope itemtype="http://schema.org/Bakery">
<span itemprop="name">Fjord Bakeri AS</span>
<span itemprop="telephone">22 41 12 00</span>
<span itemprop="email">kontakt@fjordbakeri.no</span>
<div itemprop="address" itemscope itemtype="http://schema.org/PostalAddress">
<span itemprop="streetAddress">Storgata 1</span><span itemprop="postalCode">0155</span>
<span itemprop="addressLocality">Oslo</span></div></div>
<p>Kontakt Fjord Bakeri AS på telefon eller e-post. Vi svarer innen en virkedag.</p>
</body></html>"""

NYHETER_HTML = """<html><head><title>Nyheter — Fjord Bakeri AS</title></head><body>
<main><h1>Nyheter</h1><p>Siste nytt fra Fjord Bakeri AS, bakeriet i Oslo.</p>
<ul><li><a href="/nyheter/2026/11/05/apning-butikk">Ny butikk åpner</a></li></ul></main>
</body></html>"""

ARTICLE_HTML = """<html><head><title>Ny butikk åpner — Fjord Bakeri AS</title>
<meta property="article:published_time" content="2026-11-05T08:00:00Z">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"NewsArticle",
"headline":"Ny butikk åpner","datePublished":"2026-11-05"}</script></head>
<body><article><p>Fjord Bakeri AS åpner ny butikk i Oslo sentrum. Butikken har plass
til førti gjester og selger ferskt brød hver morgen fra klokken syv.</p></article></body></html>"""

OM_OSS_HTML = """<html><head><title>Om oss — Fjord Bakeri AS</title></head><body>
<main><p>Om Fjord Bakeri AS: et bakeri i Oslo grunnlagt i 1998, eid og drevet av
familien Fjord i tre generasjoner.</p></main></body></html>"""

POSTING_HTML = """<html><head><title>Baker Oslo — Fjord Bakeri AS</title></head><body>
<main><p>Stilling som baker i Oslo hos Fjord Bakeri AS. Søknadsfrist 1. desember 2026.</p></main>
</body></html>"""

SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://fjordbakeri.no/karriere/baker-oslo</loc></url>
<url><loc>https://fjordbakeri.no/nyheter/2026/11/05/apning-butikk</loc></url>
<url><loc>https://fjordbakeri.no/nyheter</loc></url>
<url><loc>https://other.no/evil</loc></url>
<url><loc>https://fjordbakeri.no/assets/logo.png</loc></url>
<url><loc>https://fjordbakeri.no/kontakt</loc></url>
</urlset>"""

FEED_XML = """<?xml version="1.0"?><rss version="2.0"><channel><title>Fjord Bakeri AS</title>
<item><title>Ny butikk åpner</title><link>https://fjordbakeri.no/nyheter/2026/11/05/apning-butikk</link><pubDate>Wed, 05 Nov 2026 08:00:00 GMT</pubDate></item>
<item><title>Julemarked</title><link>https://fjordbakeri.no/nyheter/julemarked</link><pubDate>Mon, 01 Dec 2025 08:00:00 GMT</pubDate></item>
<item><title>Elsewhere</title><link>https://other.no/nyheter/x</link><pubDate>Mon, 01 Dec 2025 08:00:00 GMT</pubDate></item>
</channel></rss>"""

ROBOTS_TXT = "User-agent: *\nAllow: /\nSitemap: https://fjordbakeri.no/sitemap.xml\nSitemap: https://evil.example/sitemap.xml\n"

ROUTES = {
    DOMAIN + "/": ("text/html", HOMEPAGE_HTML),
    DOMAIN + "/robots.txt": ("text/plain", ROBOTS_TXT),
    DOMAIN + "/sitemap.xml": ("application/xml", SITEMAP_XML),
    DOMAIN + "/feed.xml": ("application/rss+xml", FEED_XML),
    DOMAIN + "/kontakt": ("text/html", KONTAKT_HTML),
    DOMAIN + "/karriere": ("text/html", KARRIERE_HTML),
    DOMAIN + "/karriere/baker-oslo": ("text/html", POSTING_HTML),
    DOMAIN + "/nyheter": ("text/html", NYHETER_HTML),
    DOMAIN + "/nyheter/2026/11/05/apning-butikk": ("text/html", ARTICLE_HTML),
    DOMAIN + "/om-oss": ("text/html", OM_OSS_HTML),
}


class FakeResponse:
    def __init__(self, url, content_type, body):
        self._url = url
        self._body = body.encode("utf-8")
        self.headers = {"content-type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n=-1):
        return self._body if n is None or n < 0 else self._body[:n]

    def geturl(self):
        return self._url


class FakeOpener:
    def __init__(self):
        self.requested: list[str] = []

    def open(self, request, timeout=None):
        url = request.full_url if isinstance(request, urllib.request.Request) else str(request)
        self.requested.append(url)
        route = ROUTES.get(url)
        if route is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        content_type, body = route
        return FakeResponse(url, content_type, body)


def run_mock_crawl(**context):
    opener = FakeOpener()
    with patch.object(website, "SAFE_OPENER", opener), patch.object(website, "assert_public_url", lambda url: None):
        record, metrics = fetch_website(DOMAIN + "/", **context)
    return record, metrics, opener


def metadata_for(html, url=DOMAIN + "/"):
    return extruct.extract(html, base_url=url, syntaxes=["json-ld", "microdata", "rdfa", "opengraph"])


class PriorityTermsTests(unittest.TestCase):
    def test_new_terms_select_career_and_blog_pages(self):
        soup = BeautifulSoup(
            '<a href="/karriere">Karriere</a><a href="/blog">Blog</a>'
            '<a href="/products">Products</a><a href="/ledige-stillinger">Stillinger</a>',
            "html.parser",
        )
        urls = _priority_links("https://example.no/", soup)
        self.assertIn("https://example.no/karriere", urls)
        self.assertIn("https://example.no/blog", urls)
        self.assertIn("https://example.no/ledige-stillinger", urls)
        self.assertNotIn("https://example.no/products", urls)

    def test_original_priority_order_unchanged(self):
        soup = BeautifulSoup('<a href="/kontakt">Contact</a><a href="/products">Products</a>', "html.parser")
        self.assertEqual(_priority_links("https://example.no/", soup), ["https://example.no/kontakt"])


class SitemapFeedTests(unittest.TestCase):
    def test_sitemap_index_and_out_of_domain_rejection(self):
        index = """<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <sitemap><loc>https://fjordbakeri.no/sitemap-news.xml</loc></sitemap>
        <sitemap><loc>https://evil.example/sitemap.xml</loc></sitemap></sitemapindex>"""
        parsed = site_extract.parse_sitemap_xml(index.encode(), homepage_domain="fjordbakeri.no")
        self.assertEqual(parsed["sitemap_urls"], ["https://fjordbakeri.no/sitemap-news.xml"])
        self.assertEqual(parsed["rejected_out_of_domain"], 1)

    def test_sitemap_urlset_rejects_out_of_domain_and_malformed(self):
        parsed = site_extract.parse_sitemap_xml(SITEMAP_XML.encode(), homepage_domain="fjordbakeri.no")
        self.assertIn(DOMAIN + "/karriere/baker-oslo", parsed["page_urls"])
        self.assertEqual(parsed["rejected_out_of_domain"], 1)
        bad = site_extract.parse_sitemap_xml(b"<urlset><oops", homepage_domain="fjordbakeri.no")
        self.assertTrue(bad["error"])
        self.assertEqual(bad["page_urls"], [])

    def test_robots_sitemap_directives(self):
        self.assertEqual(
            site_extract.parse_robots_sitemaps(ROBOTS_TXT),
            ["https://fjordbakeri.no/sitemap.xml", "https://evil.example/sitemap.xml"],
        )
        self.assertEqual(site_extract.parse_robots_sitemaps(None), [])

    def test_select_candidates_ranks_priority_terms_and_skips_binaries(self):
        urls = [DOMAIN + "/assets/logo.png", DOMAIN + "/karriere/x", DOMAIN + "/unrelated", DOMAIN + "/nyheter/y"]
        selected = site_extract.select_sitemap_candidates(urls, existing=[], limit=5)
        self.assertEqual(selected, [DOMAIN + "/nyheter/y", DOMAIN + "/karriere/x"])

    def test_feed_discovery_same_domain_only(self):
        soup = BeautifulSoup(
            '<link rel="alternate" type="application/rss+xml" href="/feed.xml">'
            '<link rel="alternate" type="application/rss+xml" href="https://other.no/feed.xml">'
            '<link rel="alternate" type="text/html" href="/nope">',
            "html.parser",
        )
        self.assertEqual(
            site_extract.discover_feed_links(soup, DOMAIN + "/", "fjordbakeri.no"),
            [DOMAIN + "/feed.xml"],
        )

    def test_rss_feed_parsing_drops_out_of_domain_links(self):
        items, stats = site_extract.parse_feed(
            FEED_XML.encode(), DOMAIN + "/feed.xml", homepage_domain="fjordbakeri.no",
            retrieved_at="2026-11-12T00:00:00Z",
        )
        self.assertEqual(stats["items_found"], 2)
        self.assertEqual(stats["items_dropped_out_of_domain"], 1)
        self.assertEqual(items[0]["date_published"], "2026-11-05")
        self.assertEqual(items[0]["hash_scope"], "feed_entry")
        self.assertEqual(items[0]["source_label"], "company_owned")

    def test_atom_feed_parsing(self):
        atom = """<feed xmlns="http://www.w3.org/2005/Atom"><title>x</title>
        <entry><title>Post</title><link href="https://fjordbakeri.no/blog/post"/>
        <updated>2026-10-02T10:00:00Z</updated></entry></feed>"""
        items, _ = site_extract.parse_feed(
            atom.encode(), DOMAIN + "/atom.xml", homepage_domain="fjordbakeri.no",
            retrieved_at="2026-11-12T00:00:00Z",
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["date_published"], "2026-10-02")


class StructuredDataTests(unittest.TestCase):
    def test_graph_organisations_and_job_postings(self):
        nodes = site_extract.all_structured_nodes(metadata_for(HOMEPAGE_HTML))
        orgs = site_extract.extract_structured_organisations(metadata_for(HOMEPAGE_HTML))
        # Main organisation + the (deduplicated) nested hiringOrganization
        # node, mirroring the historical recursive walk.
        self.assertEqual(len(orgs), 2)
        main = next(org for org in orgs if org.get("vatID"))
        self.assertEqual(main["name"], NAME)
        self.assertEqual(main["vatID"], "NO 923 609 016 MVA")
        jobs = site_extract.extract_job_postings_from_nodes(
            nodes, source_url=DOMAIN + "/", content_sha256="0" * 64, retrieved_at="2026-11-12T00:00:00Z",
        )
        self.assertEqual([job["title"] for job in jobs], ["Baker", "Utløpt stilling"])

    def test_microdata_local_business_subtype(self):
        orgs = site_extract.extract_structured_organisations(metadata_for(KONTAKT_HTML, DOMAIN + "/kontakt"))
        self.assertEqual(len(orgs), 1)
        self.assertEqual(orgs[0]["@type"], "Bakery")
        self.assertEqual(orgs[0].get("_source_syntax"), "microdata")
        self.assertEqual(orgs[0]["email"], "kontakt@fjordbakeri.no")

    def test_rdfa_organisation(self):
        html = ('<div vocab="https://schema.org/" typeof="Restaurant">'
                '<span property="name">Fjord Bakeri AS</span>'
                '<span property="email">post@fjordbakeri.no</span></div>')
        orgs = site_extract.extract_structured_organisations(metadata_for(html))
        self.assertEqual(len(orgs), 1)
        self.assertEqual(orgs[0]["name"], NAME)
        self.assertEqual(orgs[0].get("_source_syntax"), "rdfa")

    def test_declared_facts_labelled_site_declared(self):
        orgs = site_extract.extract_structured_organisations(metadata_for(HOMEPAGE_HTML))
        facts = site_extract.extract_site_declared_facts(orgs)
        self.assertEqual(facts["founding_date"], "1998-05-01")
        self.assertEqual(facts["number_of_employees_site_declared"], {"value": 12})
        self.assertEqual(facts["aggregate_rating_site_declared"]["rating_value"], 4.5)
        self.assertIn("not an independent review", facts["aggregate_rating_site_declared"]["label"])
        self.assertEqual(facts["vat_ids"], ["NO 923 609 016 MVA"])


class ContactTests(unittest.TestCase):
    def test_site_contacts_from_structured_data_and_footer(self):
        soup = BeautifulSoup(HOMEPAGE_HTML, "lxml")
        orgs = site_extract.extract_structured_organisations(metadata_for(HOMEPAGE_HTML))
        contacts = site_extract.extract_site_contacts(orgs, soup, source_url=DOMAIN + "/")
        self.assertIn("+47 22 41 12 00", contacts["telephone"])
        self.assertIn("post@fjordbakeri.no", contacts["email"])
        self.assertEqual(contacts["addresses"][0]["postalCode"], "0155")
        self.assertEqual(contacts["addresses"][0]["addressLocality"], "Oslo")
        self.assertIn("Mo-Fr 07:00-17:00", contacts["opening_hours"])
        self.assertEqual(contacts["contact_points"][0]["contact_type"], "customer service")
        self.assertIn("structured_data", contacts["provenance"]["telephone"])
        self.assertEqual(contacts["source"], "company_owned_website")

    def test_contact_comparison_match_differs_and_site_only(self):
        site = {"telephone": ["+47 22 41 12 00"], "email": ["post@fjordbakeri.no"],
                "addresses": [{"postalCode": "0155", "addressLocality": "Oslo", "formatted": "Storgata 1, 0155 Oslo"}]}
        registry = {"email": "POST@fjordbakeri.no", "phone": "22411200", "mobile": None,
                    "business_address": {"address": "Storgata 1", "post_town": "Oslo", "postcode": "0155"},
                    "postal_address": None}
        result = site_extract.compare_site_contacts(site, registry)
        verdicts = {item["field"]: item["verdict"] for item in result["comparisons"]}
        self.assertEqual(verdicts, {"email": "match", "telephone": "match", "address": "match"})
        registry["email"] = "other@example.no"
        registry["phone"] = "99999999"
        result = site_extract.compare_site_contacts(site, registry)
        verdicts = {item["field"]: item["verdict"] for item in result["comparisons"]}
        self.assertEqual(verdicts["email"], "differs")
        self.assertEqual(verdicts["telephone"], "differs")
        # Both values are shown, labelled by source; registry is never overwritten.
        email_item = next(item for item in result["comparisons"] if item["field"] == "email")
        self.assertEqual(email_item["site_values"], ["post@fjordbakeri.no"])
        self.assertEqual(email_item["registry_values"], ["other@example.no"])

    def test_contact_comparison_without_registry_context(self):
        result = site_extract.compare_site_contacts({"telephone": [], "email": [], "addresses": []}, None)
        self.assertFalse(result["registry_contacts_supplied"])
        self.assertEqual(result["comparisons"], [])


class DateTests(unittest.TestCase):
    def test_explicit_date_formats(self):
        self.assertEqual(site_extract.parse_explicit_date("2026-11-05T08:00:00Z"), "2026-11-05")
        self.assertEqual(site_extract.parse_explicit_date("05.11.2026"), "2026-11-05")
        self.assertEqual(site_extract.parse_explicit_date("5. november 2026"), "2026-11-05")
        self.assertEqual(site_extract.parse_explicit_date("November 5, 2026"), "2026-11-05")
        self.assertEqual(site_extract.parse_explicit_date("/nyheter/2026/11/05/x"), "2026-11-05")

    def test_unknown_date_is_none_never_guessed(self):
        self.assertIsNone(site_extract.parse_explicit_date("siden 1998"))
        self.assertIsNone(site_extract.parse_explicit_date("november 2026"))
        self.assertIsNone(site_extract.parse_explicit_date(""))
        self.assertIsNone(site_extract.parse_explicit_date("31.02.2026"))
        self.assertIsNone(site_extract.parse_explicit_date(None))

    def test_page_date_priority_and_unknown(self):
        soup = BeautifulSoup(ARTICLE_HTML, "lxml")
        nodes = site_extract.all_structured_nodes(metadata_for(ARTICLE_HTML))
        date, source = site_extract.extract_page_date(soup, nodes, DOMAIN + "/nyheter/2026/11/05/apning-butikk")
        self.assertEqual((date, source), ("2026-11-05", "json_ld_date_published"))
        soup = BeautifulSoup(NYHETER_HTML, "lxml")
        date, source = site_extract.extract_page_date(soup, [], DOMAIN + "/nyheter", "no date here")
        self.assertEqual((date, source), (None, None))

    def test_page_kind_archive_vs_article(self):
        self.assertEqual(site_extract.classify_page_kind(DOMAIN + "/nyheter"), "archive")
        self.assertEqual(site_extract.classify_page_kind(DOMAIN + "/nyheter/2026/11/05/x"), "article")
        self.assertEqual(site_extract.classify_page_kind(DOMAIN + "/karriere"), "career")
        self.assertEqual(site_extract.classify_page_kind(DOMAIN + "/", is_homepage=True), "homepage")
        nodes = site_extract.all_structured_nodes(metadata_for(ARTICLE_HTML))
        self.assertEqual(
            site_extract.classify_page_kind(DOMAIN + "/x", structured_nodes=nodes), "article",
        )


class JobVerificationTests(unittest.TestCase):
    def _candidate(self, **overrides):
        candidate = {
            "signal_type": "job_posting", "platform": "company_site",
            "source_url": DOMAIN + "/karriere", "content_sha256": "0" * 64,
            "retrieved_at": "2026-11-12T00:00:00Z", "title": "Baker",
            "date_posted": "2026-11-01", "valid_through": "2026-12-01",
            "hiring_organization": {"name": NAME}, "extraction": "json_ld_job_posting",
        }
        candidate.update(overrides)
        return candidate

    def test_name_match_verifies(self):
        verified, unverified = site_extract.verify_job_postings(
            [self._candidate()], legal_name=NAME, organisation_number=ORG, registered_domain="fjordbakeri.no",
        )
        self.assertEqual(len(verified), 1)
        self.assertTrue(verified[0]["exact_entity"])
        self.assertEqual(verified[0]["status"], "open")
        self.assertFalse(verified[0]["expired"])
        self.assertEqual(unverified, [])

    def test_org_number_on_page_verifies_link_candidate(self):
        candidate = self._candidate(hiring_organization=None, extraction="career_page_link")
        verified, _ = site_extract.verify_job_postings(
            [candidate], legal_name=NAME, organisation_number=ORG,
            registered_domain="fjordbakeri.no",
            page_texts={DOMAIN + "/karriere": "Org.nr. 923 609 016"},
        )
        self.assertEqual(len(verified), 1)
        self.assertIn("organisation number appears on the posting page", verified[0]["identity_proof"])

    def test_domain_match_verifies(self):
        candidate = self._candidate(hiring_organization={"name": "Some Group", "url": DOMAIN + "/about"})
        verified, _ = site_extract.verify_job_postings(
            [candidate], legal_name=NAME, organisation_number=ORG, registered_domain="fjordbakeri.no",
        )
        self.assertEqual(len(verified), 1)

    def test_unmatched_hiring_org_is_ambiguous_not_published(self):
        candidate = self._candidate(hiring_organization={"name": "Unrelated Parent AS"})
        verified, unverified = site_extract.verify_job_postings(
            [candidate], legal_name=NAME, organisation_number=ORG, registered_domain="fjordbakeri.no",
        )
        self.assertEqual(verified, [])
        self.assertEqual(unverified[0]["verification_status"], "ambiguous")
        self.assertFalse(unverified[0]["exact_entity"])

    def test_no_identity_context_quarantines_everything(self):
        verified, unverified = site_extract.verify_job_postings([self._candidate()])
        self.assertEqual(verified, [])
        self.assertEqual(unverified[0]["verification_reason"], "identity_context_not_supplied")

    def test_expired_posting_flagged_not_dropped(self):
        candidate = self._candidate(valid_through="2026-01-01")
        verified, _ = site_extract.verify_job_postings(
            [candidate], legal_name=NAME, organisation_number=ORG, registered_domain="fjordbakeri.no",
        )
        self.assertEqual(len(verified), 1)
        self.assertTrue(verified[0]["expired"])
        self.assertEqual(verified[0]["status"], "expired")


class MockCrawlTests(unittest.TestCase):
    def test_full_mock_crawl_populates_all_deepening_keys(self):
        registry_contacts = {
            "email": "post@fjordbakeri.no", "phone": "22 41 12 00", "mobile": None,
            "business_address": {"address": "Storgata 1", "post_town": "Oslo", "postcode": "0155"},
            "postal_address": None,
        }
        record, metrics, opener = run_mock_crawl(
            legal_name=NAME, organisation_number=ORG, registry_contacts=registry_contacts,
        )
        self.assertEqual(record["status"], "available")
        value = record["value"]
        # #12: sitemap discovery ran; out-of-domain sitemap never fetched.
        self.assertIn(DOMAIN + "/sitemap.xml", value["sitemap_discovery"]["sitemap_urls_checked"])
        self.assertGreaterEqual(value["sitemap_discovery"]["sitemap_urls_rejected_out_of_domain"], 1)
        self.assertNotIn("https://evil.example/sitemap.xml", opener.requested)
        self.assertNotIn("https://other.no/evil", opener.requested)
        self.assertEqual(value["feed_discovery"]["feeds_fetched"], 1)
        # Page budget respected: homepage + at most 8 secondary pages.
        self.assertLessEqual(len(value["pages"]), 9)
        page_urls = {page["url"] for page in value["pages"]}
        self.assertIn(DOMAIN + "/karriere/baker-oslo", page_urls)  # sitemap-only URL
        self.assertIn(DOMAIN + "/nyheter/2026/11/05/apning-butikk", page_urls)
        # #13: contacts incl. microdata email from the contact page.
        self.assertIn("kontakt@fjordbakeri.no", value["site_contacts"]["email"])
        verdicts = {item["field"]: item["verdict"] for item in value["site_contact_comparison"]["comparisons"]}
        self.assertEqual(verdicts["email"], "match")
        self.assertEqual(verdicts["telephone"], "match")
        # #14: verified jobs (name match + domain match + page org-number),
        # expired posting flagged, nothing silently dropped.
        titles = sorted(job["title"] for job in value["site_jobs"])
        self.assertIn("Baker", titles)
        self.assertIn("Konditor", titles)
        expired = [job for job in value["site_jobs"] if job["title"] == "Utløpt stilling"]
        self.assertEqual(len(expired), 1)
        self.assertTrue(expired[0]["expired"])
        self.assertTrue(all(job["exact_entity"] for job in value["site_jobs"]))
        self.assertTrue(all("retrieved_at" in job and "date_posted" in job for job in value["site_jobs"]))
        # #15: dated items list, archive vs article distinguished, undated kept null.
        items = value["site_news_items"]
        by_url = {item["url"]: item for item in items}
        article = by_url[DOMAIN + "/nyheter/2026/11/05/apning-butikk"]
        self.assertEqual(article["date_published"], "2026-11-05")
        self.assertEqual(article["page_kind"], "article")
        archive = by_url[DOMAIN + "/nyheter"]
        self.assertEqual(archive["page_kind"], "archive")
        self.assertIsNone(archive["date_published"])
        self.assertTrue(all(item["source_label"] == "company_owned" for item in items))
        # #16: declared facts present.
        self.assertEqual(value["site_declared_facts"]["founding_date"], "1998-05-01")
        self.assertGreater(metrics["requests"], 2)

    def test_mock_crawl_without_identity_context_quarantines_name_only_jobs(self):
        record, _, _ = run_mock_crawl()
        value = record["value"]
        # No legal name / org number supplied: name-only postings cannot be
        # verified and stay quarantined. The Konditor posting verifies via
        # its hiringOrganization URL on the crawled domain (an accepted
        # match criterion); the identity gate still governs publication.
        self.assertNotIn("Baker", [job["title"] for job in value["site_jobs"]])
        self.assertIn("Baker", [job["title"] for job in value["site_jobs_unverified"]])
        self.assertIn("Konditor", [job["title"] for job in value["site_jobs"]])
        self.assertFalse(value["site_contact_comparison"]["registry_contacts_supplied"])

    def test_identity_gate_still_governs_mock_crawl(self):
        record, _, _ = run_mock_crawl(legal_name=NAME, organisation_number=ORG)
        profile = {"organisation_number": ORG, "name": NAME, "evidence": {"website": record}}
        gated = apply_website_identity_gate(profile, record)["website"]
        assessment = gated["value"]["identity_assessment"]
        self.assertTrue(assessment["publishable"])
        self.assertEqual(assessment["score"], 1.0)

    def test_contract_adapter_turns_deepening_into_claims(self):
        record, _, _ = run_mock_crawl(legal_name=NAME, organisation_number=ORG)
        registry = evidence_record(
            "registry", "available", "official_registry_bulk",
            "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
            value={"navn": NAME, "organisasjonsnummer": ORG},
        )
        profile = {
            "organisation_number": ORG, "name": NAME,
            "evidence": {"registry": registry, "website": record},
            "run_metrics": {"requests": 10, "latencies_ms": [100]},
        }
        profile["evidence"]["website"] = apply_website_identity_gate(profile, record)["website"]
        envelope = contract_envelope(
            profile, run_id="wave3a-test", started_at="2026-11-12T00:00:00Z",
            completed_at="2026-11-12T00:01:00Z", modules=["registry", "website"],
        )
        self.assertEqual(validate_contract_envelope(envelope), [])
        fields = {claim["field"] for claim in envelope["claims"]}
        self.assertIn("website.site_contacts", fields)
        self.assertIn("website.site_contact_comparison", fields)
        self.assertIn("website.site_declared_facts", fields)
        self.assertTrue(any(field.startswith("website.site_jobs[") for field in fields))
        self.assertTrue(any(field.startswith("website.site_news_items[") for field in fields))
        job_claims = [claim for claim in envelope["claims"] if claim["field"].startswith("website.site_jobs[")]
        self.assertTrue(all(claim["availability"] == "available" for claim in job_claims))
        self.assertEqual(envelope["operations"]["third_party_cost_usd"], 0)

    def test_contract_adapter_marks_deepening_ambiguous_when_gate_fails(self):
        record, _, _ = run_mock_crawl(legal_name=NAME, organisation_number=ORG)
        # A different organisation number AND name: neither the vatID/footer
        # org-number evidence nor the name evidence matches this profile,
        # so the gate must not publish — even though job verification (run
        # against the crawl context) verified the postings.
        registry = evidence_record(
            "registry", "available", "official_registry_bulk",
            "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
            value={"navn": "Totally Different Company AS", "organisasjonsnummer": "111111111"},
        )
        profile = {
            "organisation_number": "111111111", "name": "Totally Different Company AS",
            "evidence": {"registry": registry, "website": record},
            "run_metrics": {"requests": 10, "latencies_ms": [100]},
        }
        profile["evidence"]["website"] = apply_website_identity_gate(profile, record)["website"]
        self.assertFalse(profile["evidence"]["website"]["value"]["identity_assessment"]["publishable"])
        envelope = contract_envelope(
            profile, run_id="wave3a-test", started_at="2026-11-12T00:00:00Z",
            completed_at="2026-11-12T00:01:00Z", modules=["registry", "website"],
        )
        job_claims = [claim for claim in envelope["claims"] if claim["field"].startswith("website.site_jobs[")]
        self.assertTrue(job_claims)
        self.assertTrue(all(claim["availability"] == "ambiguous" for claim in job_claims))


class CrawlEventsDeepeningTests(unittest.TestCase):
    def _event(self, url, html, kind):
        return extract_page_event(
            organisation_number=ORG, requested_url=url, final_url=url,
            status_code=200, content_type="text/html; charset=utf-8",
            body=html.encode("utf-8"), page_kind=kind,
            retrieved_at="2026-11-12T00:00:00Z",
        )

    def test_page_event_carries_date_kind_and_job_candidates(self):
        event = self._event(DOMAIN + "/nyheter/2026/11/05/apning-butikk", ARTICLE_HTML, "priority")
        self.assertEqual(event["published_date"], "2026-11-05")
        self.assertEqual(event["page_kind_value"], "article")
        career = self._event(DOMAIN + "/karriere", KARRIERE_HTML, "priority")
        self.assertEqual(career["page_kind_value"], "career")
        self.assertTrue(any(c["title"] == "Konditor" for c in career["job_posting_candidates"]))

    def test_merge_profile_events_populates_deepening_keys(self):
        events = [
            self._event(DOMAIN + "/", HOMEPAGE_HTML, "homepage"),
            self._event(DOMAIN + "/karriere", KARRIERE_HTML, "priority"),
            self._event(DOMAIN + "/nyheter/2026/11/05/apning-butikk", ARTICLE_HTML, "priority"),
        ]
        profile = {
            "organisation_number": ORG, "name": NAME, "website": DOMAIN + "/",
            "email": "post@fjordbakeri.no", "phone": "22411200", "mobile": None,
            "business_address": {"address": "Storgata 1", "post_town": "Oslo", "postcode": "0155"},
            "postal_address": None,
        }
        record = merge_profile_events(profile, events)
        self.assertEqual(record["status"], "available")
        value = record["value"]
        self.assertIn("post@fjordbakeri.no", value["site_contacts"]["email"])
        self.assertEqual(value["site_contact_comparison"]["registry_contacts_supplied"], True)
        self.assertTrue(any(job["title"] == "Baker" for job in value["site_jobs"]))
        self.assertTrue(any(job["title"] == "Konditor" for job in value["site_jobs"]))
        self.assertTrue(any(item["date_published"] == "2026-11-05" for item in value["site_news_items"]))
        self.assertEqual(value["site_declared_facts"]["founding_date"], "1998-05-01")
        self.assertEqual(value["sitemap_discovery"]["status"], "not_performed_in_scrapy_path")
        # Identity gate still sees the homepage evidence on the Scrapy path.
        gated = apply_website_identity_gate(
            {**profile, "evidence": {"website": record}}, record,
        )["website"]
        self.assertTrue(gated["value"]["identity_assessment"]["publishable"])


if __name__ == "__main__":
    unittest.main()
