"""Chunking tests for fetch_wikidata_batch (integration fix).

Large batches must run as several sequential queries (one per chunk)
instead of one over-long GET URL, and a failed chunk must degrade only
its own companies. No test here touches the network.
"""

from __future__ import annotations

import json
import sys
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.wikidata import fetch_wikidata_batch  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


def _fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _empty_payload() -> bytes:
    return json.dumps({"results": {"bindings": []}}).encode()


class WikidataChunkingTests(unittest.TestCase):
    def _profiles(self, orgs):
        return [{"organisation_number": org, "name": f"Company {org} AS"} for org in orgs]

    def test_chunks_split_queries_and_merge_records(self):
        raw = _fixture_bytes("wikidata-sparql-batch.json")
        calls: list[str] = []

        def fetch(url, headers, **kwargs):
            calls.append(url)
            return 200, raw, {}

        profiles = self._profiles(["923609016", "818711832", "111111111"])
        records, metrics = fetch_wikidata_batch(
            profiles, fetcher=fetch, sleeper=lambda _s: None, max_orgs_per_query=2,
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(metrics["chunks"], 2)
        self.assertEqual(metrics["requests"], 2)
        self.assertEqual(set(records), {"923609016", "818711832", "111111111"})
        # Each chunk's query names only its own organisation numbers.
        first_query = urllib.parse.parse_qs(urllib.parse.urlparse(calls[0]).query)["query"][0]
        second_query = urllib.parse.parse_qs(urllib.parse.urlparse(calls[1]).query)["query"][0]
        self.assertIn("923609016", first_query)
        self.assertIn("818711832", first_query)
        self.assertNotIn("111111111", first_query)
        self.assertIn("111111111", second_query)
        self.assertNotIn("923609016", second_query)

    def test_failed_chunk_degrades_only_its_companies(self):
        calls: list[str] = []

        def fetch(url, headers, **kwargs):
            calls.append(url)
            if len(calls) == 1:
                return 500, b"boom", {}
            return 200, _empty_payload(), {}

        profiles = self._profiles(["923609016", "818711832"])
        records, metrics = fetch_wikidata_batch(
            profiles, fetcher=fetch, sleeper=lambda _s: None, max_orgs_per_query=1,
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(records["923609016"]["status"], "source_error")
        self.assertEqual(records["818711832"]["status"], "not_found")
        self.assertEqual(metrics["chunks"], 2)


if __name__ == "__main__":
    unittest.main()
