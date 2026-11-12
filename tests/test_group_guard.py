"""Regression tests for the konsernstruktur identity guard (integration fix).

Live evidence 2026-11-12: konsernstruktur/930132764 returns a tree rooted
at the PARENT (919371943) with the queried company as a child. The guard
must accept a tree that names the queried company anywhere, and reject a
tree that names it nowhere. No test here touches the network.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.http import FetchResult  # noqa: E402
from norway_company_agent.official import fetch_official_modules  # noqa: E402


def _fetcher(mapping):
    def fetch(url):
        status, body = mapping[url]
        return FetchResult(url, status, 1, 10, body=body,
                           error=None if status == 200 else f"HTTP {status}",
                           content_sha256="a" * 64,
                           retrieved_at="2026-08-24T00:00:00Z")
    return fetch


class GroupTreeGuardTests(unittest.TestCase):
    ORG = "930132764"
    PARENT = "919371943"

    def _group_body(self, children_orgs):
        return {
            "navn": "AS OLE JAN STRØNEN",
            "organisasjonsnummer": self.PARENT,
            "organisasjonsform": {"kode": "AS", "beskrivelse": "Aksjeselskap"},
            "children": [
                {
                    "dato": "2019-12-12",
                    "grunnlag": "100 %",
                    "knytningsform": {"beskrivelse": "Konsern datter", "kode": "KDAT"},
                    "navn": f"Child {child}",
                    "nivaa": 1,
                    "organisasjonsform": {"kode": "AS", "beskrivelse": "Aksjeselskap"},
                    "organisasjonsnummer": child,
                    "parentNavn": "AS OLE JAN STRØNEN",
                    "parentOrganisasjonsnummer": self.PARENT,
                }
                for child in children_orgs
            ],
        }

    def _run(self, body):
        records, _ = fetch_official_modules(self.ORG, {"group"}, fetcher=_fetcher({
            f"https://data.brreg.no/enhetsregisteret/api/konsernstruktur/{self.ORG}": (200, body),
        }))
        return records["group"]

    def test_subsidiary_tree_rooted_at_parent_is_available(self):
        record = self._run(self._group_body(["958546319", self.ORG]))
        self.assertEqual(record["status"], "available")
        relationships = record["value"]["relationships"]
        self.assertTrue(any(rel["to_organisation_number"] == self.ORG for rel in relationships))
        self.assertTrue(all(rel["from_organisation_number"] == self.PARENT for rel in relationships))

    def test_tree_not_naming_the_company_is_rejected(self):
        record = self._run(self._group_body(["958546319", "951864188"]))
        self.assertEqual(record["status"], "source_error")
        self.assertIn("mismatch", record["note"])
        self.assertIsNone(record["value"])

    def test_tree_rooted_at_the_company_itself_is_available(self):
        body = self._group_body(["958546319"])
        body["organisasjonsnummer"] = self.ORG
        record = self._run(body)
        self.assertEqual(record["status"], "available")


if __name__ == "__main__":
    unittest.main()
