"""WAVE-3B tests: Wikidata SPARQL (#17), YouTube channel RSS (#19),
workforce PDF no-OCR default (#21), and batch wiring.

All network access is faked: SPARQL/RSS fixtures live under
tests/fixtures/ and fetchers are injected. No test in this file touches
the network. Fixture QIDs other than Q1776022 (Equinor, coordinator
verified for P2333 "923609016") and the fixture channel/video ids are
synthetic values chosen to exercise parsing, not real-world facts.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from norway_company_agent.contract import contract_envelope, validate_contract_envelope  # noqa: E402
from norway_company_agent.evidence import evidence  # noqa: E402
from norway_company_agent.synthesis import synthesize_profile  # noqa: E402
from norway_company_agent.wikidata import (  # noqa: E402
    build_query_url,
    build_sparql_query,
    fetch_wikidata_batch,
    parse_sparql_response,
    verify_name,
    wikidata_website_candidates,
)
from norway_company_agent.youtube_rss import (  # noqa: E402
    channel_name_matches,
    extract_channel_reference,
    fetch_youtube_activity,
    parse_rss_feed,
    resolve_channel_id,
)
import scripts.run_annual_report_workforce_connector as workforce  # noqa: E402
import scripts.run_competition_batch as batch_script  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
CHANNEL_ID = "UCabcdefghij1234567890AB"
FEED_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"


def _fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _fixture_json(name: str) -> dict:
    return json.loads(_fixture_bytes(name))


class WikidataQueryTests(unittest.TestCase):
    def test_single_query_covers_whole_batch_with_exact_p2333_key(self):
        query = build_sparql_query(["923609016", "818711832", "923609016"])
        self.assertEqual(query.count("VALUES"), 1)
        self.assertEqual(query.count('"923609016"'), 1)
        self.assertIn('"818711832"', query)
        self.assertIn("wdt:P2333", query)
        self.assertIn("wdt:P856", query)
        self.assertIn("wdt:P571", query)
        self.assertIn("wdt:P159", query)
        self.assertIn("wdt:P452", query)
        self.assertIn("wdt:P2397", query)
        self.assertIn("wikibase:label", query)
        self.assertIn("GROUP_CONCAT", query)

    def test_query_url_is_encoded_with_json_format(self):
        url = build_query_url(build_sparql_query(["923609016"]))
        self.assertTrue(url.startswith("https://query.wikidata.org/sparql?"))
        self.assertIn("format=json", url)
        self.assertIn("P2333", url)

    def test_invalid_org_numbers_are_rejected(self):
        for bad in ([], ["123"], ["92360901a"], ["9236090160"]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                build_sparql_query(bad)


class WikidataParseTests(unittest.TestCase):
    def test_batch_fixture_parses_to_grouped_items(self):
        grouped = parse_sparql_response(_fixture_json("wikidata-sparql-batch.json"))
        self.assertEqual(sorted(grouped), ["818711832", "923609016"])
        equinor = grouped["923609016"][0]
        self.assertEqual(equinor["qid"], "Q1776022")
        self.assertEqual(equinor["item_url"], "https://www.wikidata.org/wiki/Q1776022")
        self.assertEqual(equinor["label"], "Equinor")
        self.assertEqual(equinor["official_websites"], ["https://www.equinor.com"])
        self.assertEqual(equinor["inception"], ["1972-06-14T00:00:00Z"])
        self.assertEqual(equinor["headquarters"], [{"qid": "Q999999001", "label": "Stavanger"}])
        self.assertEqual(len(equinor["industries"]), 2)
        self.assertEqual(equinor["social_ids"]["youtube_channel_id"], [CHANNEL_ID])
        self.assertEqual(equinor["social_ids"]["x_username"], ["equinor"])
        # Norwegian Wikipedia first, then English; total count kept separately.
        self.assertEqual(equinor["wikipedia_urls"][0], "https://nb.wikipedia.org/wiki/Equinor")
        self.assertEqual(equinor["wikipedia_url_count"], 2)
        sparse = grouped["818711832"][0]
        self.assertIsNone(sparse["official_websites"])
        self.assertIsNone(sparse["inception"])
        self.assertEqual(sparse["wikipedia_url_count"], 0)

    def test_ambiguous_fixture_yields_two_qids_for_one_org(self):
        grouped = parse_sparql_response(_fixture_json("wikidata-sparql-ambiguous.json"))
        self.assertEqual([item["qid"] for item in grouped["923609016"]], ["Q1776022", "Q999999020"])

    def test_empty_fixture_yields_no_items(self):
        self.assertEqual(parse_sparql_response(_fixture_json("wikidata-sparql-empty.json")), {})

    def test_malformed_payload_shape_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_sparql_response({"results": {"bindings": "nope"}})


class WikidataNameCheckTests(unittest.TestCase):
    def test_brand_shortening_passes_and_legal_forms_are_ignored(self):
        self.assertTrue(verify_name("EQUINOR ASA", "Equinor")[0])
        self.assertTrue(verify_name("Example Cheese AS", "Example Cheese")[0])
        self.assertEqual(verify_name("EQUINOR ASA", "Equinor")[1]["matched_tokens"], ["equinor"])

    def test_no_shared_token_fails(self):
        self.assertFalse(verify_name("EQUINOR ASA", "Statoil")[0])
        self.assertFalse(verify_name("EQUINOR ASA", "Totally Unrelated Museum")[0])

    def test_missing_or_empty_tokens_fail_closed(self):
        self.assertFalse(verify_name("Example AS", None)[0])
        self.assertFalse(verify_name("Example AS", "")[0])
        self.assertFalse(verify_name("AS", "AS")[0])
        self.assertFalse(verify_name(None, "Equinor")[0])


class WikidataBatchTests(unittest.TestCase):
    def _profiles(self):
        return [
            {"organisation_number": "923609016", "name": "EQUINOR ASA"},
            {"organisation_number": "818711832", "name": "LUFTAMBULANSETJENESTEN HF"},
            {"organisation_number": "111111111", "name": "No Such Company AS"},
        ]

    def _fetcher(self, body: bytes, status: int = 200, headers: dict | None = None, error: Exception | None = None):
        calls: list[dict] = []

        def fetch(url, request_headers, **kwargs):
            calls.append({"url": url, "headers": request_headers})
            if error is not None:
                raise error
            return status, body, dict(headers or {})

        fetch.calls = calls  # type: ignore[attr-defined]
        return fetch

    def test_one_request_for_whole_batch_with_identifying_user_agent(self):
        raw = _fixture_bytes("wikidata-sparql-batch.json")
        fetcher = self._fetcher(raw, headers={"Date": "Thu, 12 Nov 2026 06:16:05 GMT"})
        records, metrics = fetch_wikidata_batch(self._profiles(), fetcher=fetcher, sleeper=lambda _s: None)
        self.assertEqual(len(fetcher.calls), 1)
        sent = fetcher.calls[0]["headers"]
        self.assertIn("builderr-signalpost", sent["User-Agent"])
        self.assertEqual(sent["Accept"], "application/sparql-results+json")
        self.assertEqual(metrics["requests"], 1)
        self.assertEqual(metrics["bytes"], len(raw))
        equinor = records["923609016"]
        self.assertEqual(equinor["status"], "available")
        self.assertEqual(equinor["value"]["qid"], "Q1776022")
        self.assertEqual(equinor["value"]["query_url"], fetcher.calls[0]["url"])
        self.assertEqual(equinor["content_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(equinor["value"]["response_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(equinor["effective_at"], "2026-11-12T06:16:05Z")
        self.assertEqual(records["818711832"]["status"], "available")
        absent = records["111111111"]
        self.assertEqual(absent["status"], "not_found")
        self.assertIsNone(absent["value"])
        self.assertIn("abstention", absent["note"])
        self.assertEqual(absent["content_sha256"], hashlib.sha256(raw).hexdigest())

    def test_ambiguous_match_is_rejected_without_claims(self):
        fetcher = self._fetcher(_fixture_bytes("wikidata-sparql-ambiguous.json"))
        records, _ = fetch_wikidata_batch(self._profiles()[:1], fetcher=fetcher, sleeper=lambda _s: None)
        record = records["923609016"]
        self.assertEqual(record["status"], "not_found")
        self.assertIsNone(record["value"])
        self.assertIn("Ambiguous", record["note"])
        self.assertIn("Q1776022", record["note"])
        self.assertIn("Q999999020", record["note"])

    def test_name_mismatch_is_rejected_without_claims(self):
        fetcher = self._fetcher(_fixture_bytes("wikidata-sparql-name-mismatch.json"))
        records, _ = fetch_wikidata_batch(self._profiles()[:1], fetcher=fetcher, sleeper=lambda _s: None)
        record = records["923609016"]
        self.assertEqual(record["status"], "not_found")
        self.assertIsNone(record["value"])
        self.assertIn("Name check failed", record["note"])

    def test_rate_limit_becomes_blocked_with_retry_after_delay(self):
        delays: list[float] = []
        fetcher = self._fetcher(b"", status=429, headers={"Retry-After": "2"})
        records, metrics = fetch_wikidata_batch(
            self._profiles()[:1], fetcher=fetcher, sleeper=delays.append,
        )
        self.assertEqual(records["923609016"]["status"], "blocked")
        self.assertIsNone(records["923609016"]["value"])
        self.assertIsNone(records["923609016"]["content_sha256"])
        self.assertEqual(metrics["requests"], 2)
        self.assertEqual(delays, [2.0])

    def test_http_error_and_network_error_become_source_error_without_raising(self):
        fetcher = self._fetcher(b"boom", status=500)
        records, _ = fetch_wikidata_batch(self._profiles()[:1], fetcher=fetcher, sleeper=lambda _s: None)
        self.assertEqual(records["923609016"]["status"], "source_error")
        raising = self._fetcher(b"", error=urllib.error.URLError("dns down"))
        records, metrics = fetch_wikidata_batch(self._profiles()[:1], fetcher=raising, sleeper=lambda _s: None)
        self.assertEqual(records["923609016"]["status"], "source_error")
        self.assertEqual(metrics["requests"], 2)

    def test_unparseable_response_becomes_source_error(self):
        fetcher = self._fetcher(b"this is not json")
        records, _ = fetch_wikidata_batch(self._profiles()[:1], fetcher=fetcher, sleeper=lambda _s: None)
        self.assertEqual(records["923609016"]["status"], "source_error")

    def test_invalid_org_number_never_queries(self):
        fetcher = self._fetcher(b"{}")
        records, _ = fetch_wikidata_batch(
            [{"organisation_number": "123", "name": "Bad"}], fetcher=fetcher, sleeper=lambda _s: None,
        )
        self.assertEqual(records["123"]["status"], "source_error")
        self.assertEqual(len(fetcher.calls), 0)

    def test_website_candidates_only_from_available_records(self):
        available = evidence("wikidata", "available", "wikidata_sparql", "https://example.test",
                             value={"official_websites": ["https://www.equinor.com", ""]})
        self.assertEqual(wikidata_website_candidates(available), ["https://www.equinor.com"])
        self.assertEqual(wikidata_website_candidates(evidence("wikidata", "not_found", "wikidata_sparql", "https://example.test")), [])
        self.assertEqual(wikidata_website_candidates(None), [])


class WikidataContractTests(unittest.TestCase):
    def _profile_with_wikidata(self):
        raw = _fixture_bytes("wikidata-sparql-batch.json")
        records, _ = fetch_wikidata_batch(
            [{"organisation_number": "923609016", "name": "EQUINOR ASA"}],
            fetcher=lambda url, headers, **kw: (200, raw, {}), sleeper=lambda _s: None,
        )
        return {
            "organisation_number": "923609016",
            "name": "EQUINOR ASA",
            "run_metrics": {"requests": 1, "bytes": 10, "latencies_ms": [5]},
            "evidence": {
                # Every real batch profile carries a registry record; the
                # synthesis references it, so the fixture must too.
                "registry": evidence("registry", "available", "official_registry_bulk",
                                     "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
                                     value={"organisasjonsnummer": "923609016", "navn": "EQUINOR ASA"},
                                     content_sha256="a" * 64, retrieved_at="2026-11-12T00:00:00Z"),
                "wikidata": records["923609016"],
            },
        }

    def test_generic_adapter_maps_wikidata_to_claims_without_adapter_change(self):
        envelope = contract_envelope(
            self._profile_with_wikidata(), run_id="r1",
            started_at="2026-11-12T00:00:00Z", completed_at="2026-11-12T00:00:01Z",
        )
        self.assertEqual(validate_contract_envelope(envelope), [])
        by_field = {claim["field"]: claim for claim in envelope["claims"]}
        self.assertEqual(by_field["wikidata.qid"]["value"], "Q1776022")
        self.assertEqual(by_field["wikidata.qid"]["availability"], "available")
        self.assertEqual(by_field["wikidata.official_websites"]["value"], ["https://www.equinor.com"])
        self.assertIn("query.wikidata.org", by_field["wikidata.query_url"]["value"])
        self.assertEqual(len(by_field["wikidata.response_sha256"]["value"]), 64)

    def test_rejected_match_yields_one_not_available_claim_and_no_qid_claim(self):
        raw = _fixture_bytes("wikidata-sparql-ambiguous.json")
        records, _ = fetch_wikidata_batch(
            [{"organisation_number": "923609016", "name": "EQUINOR ASA"}],
            fetcher=lambda url, headers, **kw: (200, raw, {}), sleeper=lambda _s: None,
        )
        profile = {"organisation_number": "923609016", "name": "EQUINOR ASA", "evidence": {
            "registry": evidence("registry", "available", "official_registry_bulk",
                                 "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
                                 value={"organisasjonsnummer": "923609016", "navn": "EQUINOR ASA"},
                                 content_sha256="a" * 64, retrieved_at="2026-11-12T00:00:00Z"),
            "wikidata": records["923609016"],
        }}
        envelope = contract_envelope(profile, run_id="r1", started_at="2026-11-12T00:00:00Z", completed_at="2026-11-12T00:00:01Z")
        self.assertEqual(validate_contract_envelope(envelope), [])
        wikidata_claims = [claim for claim in envelope["claims"] if claim["field"].startswith("wikidata")]
        self.assertEqual(len(wikidata_claims), 1)
        self.assertEqual(wikidata_claims[0]["availability"], "not_available")
        self.assertNotIn("Q999999020", json.dumps(envelope["claims"]))

    def test_synthesis_unknowns_list_unavailable_wikidata(self):
        profile = {
            "organisation_number": "923609016", "name": "EQUINOR ASA",
            "evidence": {"wikidata": evidence("wikidata", "not_found", "wikidata_sparql", "https://example.test", note="absent")},
        }
        result = synthesize_profile(profile, [])
        self.assertIn("wikidata", [entry["family"] for entry in result["unknowns"]])


class YoutubeRssParseTests(unittest.TestCase):
    def test_feed_fixture_parses_to_dated_videos_without_metrics(self):
        parsed = parse_rss_feed(_fixture_bytes("youtube-rss-feed.xml"))
        self.assertEqual(parsed["channel_id"], CHANNEL_ID)
        self.assertEqual(parsed["channel_name"], "Equinor")
        self.assertEqual(len(parsed["videos"]), 3)
        first = parsed["videos"][0]
        self.assertEqual(first["video_id"], "AAAAAAAAAAA")
        self.assertEqual(first["title"], "Third quarter results presentation")
        self.assertEqual(first["published"], "2026-10-24T07:00:00+00:00")
        self.assertEqual(first["url"], "https://www.youtube.com/watch?v=AAAAAAAAAAA")
        dumped = json.dumps(parsed)
        for forbidden in ("subscriber", "view_count", "like_count", "comment_count"):
            self.assertNotIn(forbidden, dumped)

    def test_mismatch_fixture_reports_its_own_channel_name(self):
        parsed = parse_rss_feed(_fixture_bytes("youtube-rss-feed-mismatch.xml"))
        self.assertEqual(parsed["channel_name"], "Oslo Cat Videos")

    def test_malformed_or_wrong_feeds_raise(self):
        for raw in (b"not xml", b"<html></html>", b"<feed xmlns=\"http://www.w3.org/2005/Atom\"></feed>"):
            with self.subTest(raw=raw[:20]), self.assertRaises(ValueError):
                parse_rss_feed(raw)

    def test_channel_page_resolution_prefers_canonical(self):
        html = _fixture_bytes("youtube-channel-page.html").decode()
        self.assertEqual(resolve_channel_id(html, "https://youtube.com/@equinor"), CHANNEL_ID)
        json_only = '<html><script>{"channelId":"UC' + "B" * 22 + '"}</script></html>'
        self.assertEqual(resolve_channel_id(json_only, "https://youtube.com/@x"), "UC" + "B" * 22)
        both = (
            '<link rel="canonical" href="https://www.youtube.com/channel/UC' + "A" * 22 + '">'
            '<script>{"channelId":"UC' + "B" * 22 + '"}</script>'
        )
        self.assertEqual(resolve_channel_id(both, "https://youtube.com/@x"), "UC" + "A" * 22)
        self.assertIsNone(resolve_channel_id("<html><body>no id here</body></html>", "https://youtube.com/@x"))

    def test_channel_reference_extraction(self):
        self.assertEqual(
            extract_channel_reference(f"https://youtube.com/channel/{CHANNEL_ID}"),
            {"kind": "channel_id", "value": CHANNEL_ID},
        )
        self.assertEqual(extract_channel_reference("https://youtube.com/@equinor"), {"kind": "handle", "value": "@equinor"})
        self.assertEqual(extract_channel_reference("https://youtube.com/user/equinor"), {"kind": "legacy", "value": "user/equinor"})
        self.assertEqual(extract_channel_reference("https://youtube.com/c/equinor"), {"kind": "legacy", "value": "c/equinor"})
        self.assertIsNone(extract_channel_reference("https://instagram.com/equinor"))
        self.assertIsNone(extract_channel_reference("https://youtube.com/watch?v=AAAAAAAAAAA"))
        self.assertIsNone(extract_channel_reference("https://youtube.com/channel/short"))

    def test_channel_name_match_is_conservative(self):
        self.assertTrue(channel_name_matches("EQUINOR ASA", "Equinor")[0])
        self.assertTrue(channel_name_matches("Example Cheese AS", "Example Cheese")[0])
        self.assertFalse(channel_name_matches("EQUINOR ASA", "Equinor Careers")[0])
        self.assertFalse(channel_name_matches("EQUINOR ASA", "Oslo Cat Videos")[0])
        self.assertFalse(channel_name_matches("EQUINOR ASA", None)[0])
        self.assertFalse(channel_name_matches("EQUINOR ASA", "")[0])


class YoutubeFetchTests(unittest.TestCase):
    def _profile(self, social_links, publishable=True, name="EQUINOR ASA"):
        return {
            "organisation_number": "923609016",
            "name": name,
            "evidence": {"website": evidence(
                "website", "available", "registry_linked_company_website", "https://www.equinor.com/",
                value={
                    "social_links": social_links,
                    "identity_assessment": {"publishable": publishable, "status": "exact" if publishable else "review", "score": 1.0 if publishable else 0.3},
                },
            )},
        }

    def _fetcher(self, mapping):
        calls: list[str] = []

        def fetch(url, headers, **kwargs):
            calls.append(url)
            status, body = mapping[url]
            return status, body, {}

        fetch.calls = calls  # type: ignore[attr-defined]
        return fetch

    def test_direct_channel_url_fetches_one_rss_feed(self):
        raw = _fixture_bytes("youtube-rss-feed.xml")
        fetcher = self._fetcher({FEED_URL: (200, raw)})
        profile = self._profile([{"platform": "youtube", "url": f"https://youtube.com/channel/{CHANNEL_ID}"}])
        record, metrics = fetch_youtube_activity(profile, fetcher=fetcher, sleeper=lambda _s: None)
        self.assertEqual(record["status"], "available")
        self.assertEqual(record["source_url"], FEED_URL)
        self.assertEqual(record["content_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(record["value"]["channel_id"], CHANNEL_ID)
        self.assertEqual(record["value"]["video_count"], 3)
        self.assertEqual(record["value"]["handle_source"], "company_website_social_link")
        self.assertEqual(metrics["requests"], 1)
        self.assertNotIn("subscriber", json.dumps(record["value"]))

    def test_handle_resolves_via_channel_page_then_feed(self):
        fetcher = self._fetcher({
            "https://youtube.com/@equinor": (200, _fixture_bytes("youtube-channel-page.html")),
            FEED_URL: (200, _fixture_bytes("youtube-rss-feed.xml")),
        })
        profile = self._profile([{"platform": "youtube", "url": "https://youtube.com/@equinor"}])
        record, metrics = fetch_youtube_activity(profile, fetcher=fetcher, sleeper=lambda _s: None)
        self.assertEqual(record["status"], "available")
        self.assertEqual(record["value"]["handle_url"], "https://youtube.com/@equinor")
        self.assertEqual(metrics["requests"], 2)

    def test_unresolvable_handle_abstains(self):
        fetcher = self._fetcher({"https://youtube.com/@ghost": (200, b"<html>empty</html>")})
        profile = self._profile([{"platform": "youtube", "url": "https://youtube.com/@ghost"}])
        record, _ = fetch_youtube_activity(profile, fetcher=fetcher, sleeper=lambda _s: None)
        self.assertEqual(record["status"], "not_found")
        self.assertIsNone(record["value"])

    def test_feed_404_abstains_and_429_blocks(self):
        fetcher = self._fetcher({FEED_URL: (404, b"")})
        profile = self._profile([{"platform": "youtube", "url": f"https://youtube.com/channel/{CHANNEL_ID}"}])
        record, _ = fetch_youtube_activity(profile, fetcher=fetcher, sleeper=lambda _s: None)
        self.assertEqual(record["status"], "not_found")
        fetcher = self._fetcher({FEED_URL: (429, b"")})
        record, _ = fetch_youtube_activity(profile, fetcher=fetcher, sleeper=lambda _s: None)
        self.assertEqual(record["status"], "blocked")

    def test_channel_name_mismatch_publishes_nothing(self):
        fetcher = self._fetcher({FEED_URL: (200, _fixture_bytes("youtube-rss-feed-mismatch.xml"))})
        profile = self._profile([{"platform": "youtube", "url": f"https://youtube.com/channel/{CHANNEL_ID}"}])
        record, _ = fetch_youtube_activity(profile, fetcher=fetcher, sleeper=lambda _s: None)
        self.assertEqual(record["status"], "not_found")
        self.assertIsNone(record["value"])
        self.assertIn("mismatch", record["note"])

    def test_no_handle_or_failed_gate_costs_zero_requests(self):
        fetcher = self._fetcher({})
        record, metrics = fetch_youtube_activity(self._profile([]), fetcher=fetcher, sleeper=lambda _s: None)
        self.assertEqual(record["status"], "not_applicable")
        self.assertEqual(metrics["requests"], 0)
        record, metrics = fetch_youtube_activity(
            self._profile([{"platform": "youtube", "url": "https://youtube.com/@equinor"}], publishable=False),
            fetcher=fetcher, sleeper=lambda _s: None,
        )
        self.assertEqual(record["status"], "not_applicable")
        self.assertEqual(metrics["requests"], 0)
        record, metrics = fetch_youtube_activity(
            {"organisation_number": "923609016", "name": "EQUINOR ASA", "evidence": {}},
            fetcher=fetcher, sleeper=lambda _s: None,
        )
        self.assertEqual(record["status"], "not_applicable")
        self.assertEqual(metrics["requests"], 0)

    def test_fetcher_failure_becomes_source_error_without_raising(self):
        def raising(url, headers, **kwargs):
            raise urllib.error.URLError("down")

        profile = self._profile([{"platform": "youtube", "url": f"https://youtube.com/channel/{CHANNEL_ID}"}])
        record, _ = fetch_youtube_activity(profile, fetcher=raising, sleeper=lambda _s: None)
        self.assertEqual(record["status"], "source_error")

    def test_second_handle_is_tried_when_first_fails(self):
        fetcher = self._fetcher({
            "https://youtube.com/@aaa": (200, b"<html>empty</html>"),
            FEED_URL: (200, _fixture_bytes("youtube-rss-feed.xml")),
        })
        profile = self._profile([
            {"platform": "youtube", "url": "https://youtube.com/@aaa"},
            {"platform": "youtube", "url": f"https://youtube.com/channel/{CHANNEL_ID}"},
        ])
        record, _ = fetch_youtube_activity(profile, fetcher=fetcher, sleeper=lambda _s: None)
        self.assertEqual(record["status"], "available")
        self.assertEqual(len(record["value"]["handles_attempted"]), 2)

    def test_generic_adapter_maps_youtube_activity_to_claims(self):
        fetcher = self._fetcher({FEED_URL: (200, _fixture_bytes("youtube-rss-feed.xml"))})
        profile = self._profile([{"platform": "youtube", "url": f"https://youtube.com/channel/{CHANNEL_ID}"}])
        record, _ = fetch_youtube_activity(profile, fetcher=fetcher, sleeper=lambda _s: None)
        profile["evidence"]["youtube_activity"] = record
        profile["evidence"]["registry"] = evidence("registry", "available", "official_registry_bulk",
                                                   "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
                                                   value={"organisasjonsnummer": "923609016", "navn": "EQUINOR ASA"},
                                                   content_sha256="a" * 64, retrieved_at="2026-11-12T00:00:00Z")
        envelope = contract_envelope(profile, run_id="r1", started_at="2026-11-12T00:00:00Z", completed_at="2026-11-12T00:00:01Z")
        self.assertEqual(validate_contract_envelope(envelope), [])
        by_field = {claim["field"]: claim for claim in envelope["claims"]}
        self.assertEqual(by_field["youtube_activity.channel_id"]["value"], CHANNEL_ID)
        self.assertEqual(by_field["youtube_activity.video_count"]["value"], 3)


class WorkforceOcrDefaultTests(unittest.TestCase):
    def _collect_profile(self):
        return {
            "organisation_number": "923609016",
            "evidence": {
                "registry": {"value": {}},
                "financial_history": {"value": {"pdfs": [{"year": "2024", "url": "https://example.invalid/x.pdf"}]}},
            },
        }

    def _cached_pdf(self, cache_dir: Path) -> Path:
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / "923609016-2024.pdf"
        path.write_bytes(b"%PDF-1.4 fake")
        return path

    def _fake_reader(self, text: str):
        class FakePage:
            def extract_text(self):
                return text

        class FakeReader:
            def __init__(self, _stream, strict=False):
                self.pages = [FakePage()]

        return FakeReader

    def test_ocr_pages_zero_never_calls_ocr_even_when_text_needs_it(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            self._cached_pdf(cache_dir)
            short_text = "923609016\nAnnual report cover.\n"
            with (
                patch.object(workforce, "PdfReader", self._fake_reader(short_text)),
                patch.object(workforce, "ocr_pdf", Mock(side_effect=AssertionError("OCR must not run"))) as ocr_mock,
            ):
                observation, status = workforce.collect(self._collect_profile(), cache_dir, ocr_pages=0, ocr_dpi=130)
            self.assertIsNone(observation)
            self.assertEqual(status["status"], "no_employee_phrase")
            self.assertFalse(status["ocr_used"])
            ocr_mock.assert_not_called()

    def test_text_extraction_path_still_accepts_without_ocr(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            self._cached_pdf(cache_dir)
            text = "923609016 " + "Selskapet har 5 ansatte i regnskapsåret. " + ("Fylltekst om regnskapet. " * 6)
            with patch.object(workforce, "PdfReader", self._fake_reader(text)):
                observation, status = workforce.collect(self._collect_profile(), cache_dir, ocr_pages=0, ocr_dpi=130)
            self.assertEqual(status["status"], "accepted")
            self.assertEqual(status["workforce_value"], 5)
            self.assertFalse(status["ocr_used"])
            self.assertEqual(observation["signal_type"], "workforce_snapshot")

    def test_explicit_ocr_pages_still_uses_ocr_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            self._cached_pdf(cache_dir)
            short_text = "923609016\nAnnual report cover.\n"
            with (
                patch.object(workforce, "PdfReader", self._fake_reader(short_text)),
                patch.object(workforce, "ocr_pdf", Mock(return_value="Selskapet har 7 ansatte. ")) as ocr_mock,
            ):
                _observation, status = workforce.collect(self._collect_profile(), cache_dir, ocr_pages=15, ocr_dpi=130)
            ocr_mock.assert_called_once()
            self.assertTrue(status["ocr_used"])
            self.assertEqual(status["workforce_value"], 7)

    def test_script_help_declares_zero_ocr_default(self):
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "run_annual_report_workforce_connector.py"), "--help"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("default: 0", completed.stdout)
        self.assertIn("pdftoppm", completed.stdout)

    def test_script_runs_with_defaults_and_no_eligible_companies(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            profiles = base / "profiles.jsonl"
            profiles.write_text(json.dumps({
                "organisation_number": "923609016",
                "evidence": {"registry": {"value": {"antallAnsatte": "30"}}},
            }) + "\n", encoding="utf-8")
            orgs = base / "orgs.txt"
            orgs.write_text("923609016\n", encoding="utf-8")
            output, report = base / "out.jsonl", base / "report.json"
            argv = [
                "run_annual_report_workforce_connector.py",
                "--profiles", str(profiles), "--organisations", str(orgs),
                "--output", str(output), "--cache", str(base / "cache"), "--report", str(report),
            ]
            with patch.object(sys, "argv", argv):
                workforce.main()
            self.assertEqual(output.read_text(encoding="utf-8"), "")
            self.assertEqual(json.loads(report.read_text(encoding="utf-8"))["eligible"], 0)


class BatchWiringTests(unittest.TestCase):
    ORG = "818711832"
    CANDIDATE_URL = "https://candidate.example.no/"

    def _bulk_and_orgs(self, directory: Path) -> tuple[Path, Path]:
        bulk = directory / "bulk.csv.gz"
        with gzip.open(bulk, "wt", encoding="utf-8", newline="") as handle:
            handle.write((FIXTURES / "bulk_quoted_row.csv").read_text(encoding="utf-8"))
        orgs = directory / "orgs.txt"
        orgs.write_text(self.ORG + "\n", encoding="utf-8")
        return bulk, orgs

    def _website_record(self, status: str, value: dict | None, source_url: str) -> dict:
        return evidence("website", status, "registry_linked_company_website", source_url, value=value, content_sha256="a" * 64)

    def _gate_passing_value(self) -> dict:
        return {
            "title": "Luftambulansetjenesten HF",
            "description": "Air ambulance service.",
            "main_text_excerpt": "Luftambulansetjenesten HF operates air ambulances. " * 10,
            "identity_text_excerpt": "Luftambulansetjenesten HF, Org.nr. 818 711 832",
            "social_links": [],
            "structured_organisations": [],
            "pages": [],
            "content_sha256": "a" * 64,
        }

    def _gate_failing_value(self) -> dict:
        return {
            "title": "Someone Else",
            "description": "Unrelated page.",
            "main_text_excerpt": "short",
            "identity_text_excerpt": "",
            "social_links": [],
            "structured_organisations": [],
            "pages": [],
            "content_sha256": "c" * 64,
        }

    def _wikidata_record(self) -> dict:
        return evidence(
            "wikidata", "available", "wikidata_sparql", "https://query.wikidata.org/sparql?query=x",
            value={
                "organisation_number": self.ORG,
                "qid": "Q999999010",
                "label": "Luftambulansetjenesten",
                "official_websites": [self.CANDIDATE_URL],
            },
            content_sha256="b" * 64, retrieved_at="2026-11-12T00:00:00Z",
        )

    def _run_batch(self, directory: Path, fetch_website_impl, wikidata_impl, modules_arg: str | None):
        bulk, orgs = self._bulk_and_orgs(directory)
        profiles_out = directory / "profiles.jsonl"
        output = directory / "envelopes.jsonl"
        report_path = directory / "report.json"
        contract_out = directory / "contract.jsonl"
        argv = [
            "run_competition_batch.py",
            "--organisations", str(orgs), "--bulk", str(bulk),
            "--output", str(output), "--profiles-output", str(profiles_out),
            "--report", str(report_path), "--run-id", "wave3b-test",
            "--expected-count", "1", "--workers", "1",
            "--contract-output", str(contract_out),
        ]
        if modules_arg is not None:
            argv += ["--modules", modules_arg]
        youtube_record = evidence("youtube_activity", "not_applicable", "youtube_channel_rss",
                                  "https://www.youtube.com/feeds/videos.xml", note="mock")
        with (
            patch.object(batch_script, "fetch_official_modules", lambda org, modules: ({}, [])),
            patch.object(batch_script, "fetch_website", fetch_website_impl),
            patch.object(batch_script, "fetch_wikidata_batch", wikidata_impl),
            patch.object(batch_script, "fetch_youtube_activity", lambda profile, **kw: (youtube_record, {"requests": 0, "bytes": 0, "latencies_ms": []})),
            patch.object(sys, "argv", argv),
        ):
            with self.assertRaises(SystemExit) as caught:
                batch_script.main()
        self.assertEqual(caught.exception.code, 0)
        profiles = [json.loads(line) for line in profiles_out.read_text(encoding="utf-8").splitlines() if line.strip()]
        contract = [json.loads(line) for line in contract_out.read_text(encoding="utf-8").splitlines() if line.strip()]
        report = json.loads(report_path.read_text(encoding="utf-8"))
        return profiles[0], contract[0], report

    def test_default_modules_include_wikidata_and_youtube_and_candidate_publishes(self):
        calls: list[str] = []
        wikidata_calls: list[int] = []

        def fetch_website_impl(url, **kwargs):
            calls.append(str(url))
            if str(url) == self.CANDIDATE_URL:
                return self._website_record("available", self._gate_passing_value(), self.CANDIDATE_URL), {"requests": 2, "bytes": 10, "latencies_ms": [5]}
            return self._website_record("source_error", None, str(url)), {"requests": 2, "bytes": 0, "latencies_ms": [5]}

        def wikidata_impl(profiles, **kwargs):
            wikidata_calls.append(len(profiles))
            return {self.ORG: self._wikidata_record()}, {"requests": 1, "bytes": 10, "latencies_ms": [5]}

        with tempfile.TemporaryDirectory() as directory:
            profile, contract, report = self._run_batch(Path(directory), fetch_website_impl, wikidata_impl, None)
        self.assertEqual(wikidata_calls, [1])
        self.assertEqual(len(calls), 2)
        self.assertIn("wikidata", report["modules"])
        self.assertIn("youtube_activity", report["modules"])
        website_value = profile["evidence"]["website"]["value"]
        self.assertEqual(website_value["website_candidate_source"], "wikidata")
        self.assertEqual(website_value["wikidata_qid"], "Q999999010")
        self.assertEqual(website_value["registry_website"], profile["website"])
        self.assertTrue(website_value["identity_assessment"]["publishable"])
        self.assertEqual(report["wikidata"]["batch_requests"], 1)
        self.assertEqual(report["wikidata"]["status_counts"], {"available": 1})
        self.assertEqual(report["wikidata"]["website_candidates_published"], 1)
        self.assertEqual(report["youtube_activity"]["status_counts"], {"not_applicable": 1})
        fields = {claim["field"]: claim for claim in contract["claims"]}
        self.assertEqual(fields["wikidata.qid"]["value"], "Q999999010")
        self.assertEqual(fields["youtube_activity"]["availability"], "not_applicable")
        self.assertEqual(validate_contract_envelope(contract), [])

    def test_live_registry_website_is_never_replaced_by_a_candidate(self):
        calls: list[str] = []

        def fetch_website_impl(url, **kwargs):
            calls.append(str(url))
            return self._website_record("available", self._gate_passing_value(), str(url)), {"requests": 2, "bytes": 10, "latencies_ms": [5]}

        def wikidata_impl(profiles, **kwargs):
            return {self.ORG: self._wikidata_record()}, {"requests": 1, "bytes": 10, "latencies_ms": [5]}

        with tempfile.TemporaryDirectory() as directory:
            profile, _contract, _report = self._run_batch(Path(directory), fetch_website_impl, wikidata_impl, None)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("website_candidate_source", profile["evidence"]["website"]["value"])
        self.assertNotIn("website_candidate_attempt", profile["evidence"]["wikidata"]["value"])

    def test_failed_candidate_keeps_dead_registry_record_and_is_logged_on_wikidata(self):
        def fetch_website_impl(url, **kwargs):
            if str(url) == self.CANDIDATE_URL:
                return self._website_record("available", self._gate_failing_value(), self.CANDIDATE_URL), {"requests": 2, "bytes": 10, "latencies_ms": [5]}
            return self._website_record("source_error", None, str(url)), {"requests": 2, "bytes": 0, "latencies_ms": [5]}

        def wikidata_impl(profiles, **kwargs):
            return {self.ORG: self._wikidata_record()}, {"requests": 1, "bytes": 10, "latencies_ms": [5]}

        with tempfile.TemporaryDirectory() as directory:
            profile, _contract, report = self._run_batch(Path(directory), fetch_website_impl, wikidata_impl, None)
        self.assertEqual(profile["evidence"]["website"]["status"], "source_error")
        attempt = profile["evidence"]["wikidata"]["value"]["website_candidate_attempt"]
        self.assertFalse(attempt["published"])
        self.assertEqual(attempt["candidate_urls"], [self.CANDIDATE_URL])
        self.assertFalse(attempt["outcomes"][0]["publishable"])
        self.assertEqual(report["wikidata"]["website_candidates_published"], 0)

    def test_opting_out_of_wikidata_skips_query_and_candidate_flow(self):
        def fetch_website_impl(url, **kwargs):
            return self._website_record("source_error", None, str(url)), {"requests": 2, "bytes": 0, "latencies_ms": [5]}

        wikidata_impl = Mock(side_effect=AssertionError("Wikidata must not be queried when opted out"))
        with tempfile.TemporaryDirectory() as directory:
            profile, _contract, report = self._run_batch(
                Path(directory), fetch_website_impl, wikidata_impl,
                "registry,accounting_obligation,website,youtube_activity",
            )
        wikidata_impl.assert_not_called()
        self.assertNotIn("wikidata", profile["evidence"])
        self.assertIsNone(report["wikidata"])
        self.assertEqual(profile["evidence"]["website"]["status"], "source_error")


if __name__ == "__main__":
    unittest.main()
