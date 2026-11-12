"""Prototype UX tests for improvement #25 (AUDIT.md section 4).

Stdlib-only on purpose: the full test_poc.py suite needs third-party deps
(beautifulsoup4, ...) that are not installed in this environment, while
build_prototype.py + research.py need only the standard library. The two
PrototypeTests from test_poc.py that touch build_prototype are replicated
here (test_qualification_copy_compat, test_quarantine_replicates_test_poc)
so backward compatibility stays verified even when test_poc.py cannot run.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from norway_company_agent.evidence import evidence  # noqa: E402
from norway_company_agent.research import answer_profile  # noqa: E402
from scripts.build_prototype import (  # noqa: E402
    PUBLISHABLE_ACQUISITION_MODES,
    _RESEARCH_MODE_QUESTIONS,
    build,
    build_agent_answers,
    compact,
    derive_jobs,
    derive_run_meta,
    normalize_observation,
    qualification_copy,
    read_jsonl,
    summarize_group,
)

FIXTURES = ROOT / "tests" / "fixtures"


def load_fixtures():
    rows = read_jsonl(FIXTURES / "prototype-ux25-profiles.jsonl")
    observations: dict[str, list[dict]] = {}
    for item in read_jsonl(FIXTURES / "prototype-ux25-observations.jsonl"):
        observations.setdefault(str(item["organisation_number"]), []).append(item)
    envelopes = {
        str(env["organisation_number"]): env
        for env in read_jsonl(FIXTURES / "prototype-ux25-envelopes.jsonl")
    }
    return rows, observations, envelopes


def extract_embedded(html: str) -> tuple[list[dict], dict]:
    start = html.index("const DATA=") + len("const DATA=")
    decoder = json.JSONDecoder()
    data, end = decoder.raw_decode(html[start:])
    batch_start = html.index("const BATCH=", end) + len("const BATCH=")
    batch, _ = decoder.raw_decode(html[batch_start:])
    return data, batch


class PrototypeUx25Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows, cls.observations, cls.envelopes = load_fixtures()
        cls.html = build(cls.rows, None, None, None, cls.observations, cls.envelopes)
        cls.data, cls.batch = extract_embedded(cls.html)
        cls.by_org = {row["org"]: row for row in cls.data}

    def test_fixtures_cover_the_required_shapes(self):
        self.assertEqual(len(self.rows), 5)
        self.assertEqual(len(self.observations["444444444"]), 5)
        self.assertEqual(len(self.envelopes), 2)

    def test_header_reflects_actual_batch(self):
        self.assertIn("5 companies · run ux25-test-run · 2026-11-12", self.html)
        self.assertEqual(self.batch["count"], 5)
        self.assertEqual(self.batch["runId"], "ux25-test-run")
        self.assertEqual(self.batch["completedAt"], "2026-11-12T01:00:00Z")

    def test_header_without_envelopes_reports_missing_run_data(self):
        html = build(self.rows, None, None, None, self.observations, {})
        self.assertIn("5 companies · run run not reported · date not reported", html)

    def test_no_stale_copy_or_score_claims(self):
        for stale in (
            "100-company showcase",
            "22 exact LinkedIn profiles",
            "32 exact LinkedIn profiles captured",
            "9 sites and 17 handles discovered downstream",
            "Independent judge",
            "Competition proxy",
            "core-score",
            "score-grid",
            "raw_score",
            "awardable",
            "qualification",
        ):
            self.assertNotIn(stale, self.html)
        self.assertNotIn("/100", self.html)
        self.assertIn("This prototype makes no competition-score claim.", self.html)

    def test_no_linkedin_monkey_patch(self):
        self.assertNotIn("renderLinkedInLayer", self.html)
        self.assertNotIn("linkedin-layer", self.html)
        self.assertNotIn("injectLinkedInLayer", self.html)
        self.assertEqual(self.html.count("<script>"), 1)
        self.assertIn("renderExternalSection", self.html)
        self.assertIn('id="external-signals"', self.html)

    def test_embedded_data_is_valid_json_with_new_fields(self):
        self.assertEqual(len(self.data), 5)
        for row in self.data:
            for key in ("agentAnswers", "changes", "jobsStatus", "group", "contract", "external"):
                self.assertIn(key, row)
            self.assertIn("items", row["external"])
            self.assertIn("summary", row["external"])
            self.assertNotIn("linkedin", row["external"])
            self.assertEqual(
                set(row["agentAnswers"]),
                {"overview", "financials", "leaders", "locations", "social", "hiring", "activity", "sentiment"},
            )

    def test_agent_answers_equal_research_py(self):
        """Equivalence: embedded research-mode answers are answer_profile, verbatim."""
        for row in self.rows:
            compacted = self.by_org[row["organisation_number"]]
            for mode, question in _RESEARCH_MODE_QUESTIONS.items():
                expected = answer_profile(row, question)
                actual = compacted["agentAnswers"][mode]
                self.assertEqual(actual["facts"], expected["facts"], f"{row['organisation_number']} {mode} facts")
                self.assertEqual(
                    actual["unsupported_or_uncertain"][: len(expected["unsupported_or_uncertain"])],
                    expected["unsupported_or_uncertain"],
                    f"{row['organisation_number']} {mode} unsupported",
                )
                self.assertEqual(actual["question"], expected["question"])

    def test_hiring_activity_sentiment_abstain_explicitly(self):
        minimal = self.by_org["222222222"]
        self.assertEqual(minimal["agentAnswers"]["hiring"]["facts"], [])
        self.assertTrue(any("not checked" in item for item in minimal["agentAnswers"]["hiring"]["unsupported_or_uncertain"]))
        self.assertTrue(any("not checked" in item for item in minimal["agentAnswers"]["activity"]["unsupported_or_uncertain"]))
        hiring = self.by_org["444444444"]["agentAnswers"]["hiring"]
        self.assertTrue(any("Baker" in str(fact["value"]) for fact in hiring["facts"]))
        self.assertFalse(any("Driver" in str(fact["value"]) for fact in hiring["facts"]))
        activity = self.by_org["444444444"]["agentAnswers"]["activity"]
        self.assertTrue(any("office" in str(fact.get("detail") or "") for fact in activity["facts"]))
        for row in self.data:
            sentiment = row["agentAnswers"]["sentiment"]
            self.assertEqual(sentiment["facts"], [])
            self.assertTrue(any("Sentiment is not scored" in item for item in sentiment["unsupported_or_uncertain"]))

    def test_generic_observations_any_platform(self):
        items = self.by_org["444444444"]["external"]["items"]
        self.assertEqual(len(items), 5)
        self.assertEqual(
            {item["platform"] for item in items},
            {"company_site", "job_board", "news", "company_directory", "linkedin"},
        )
        by_id = {item["id"]: item for item in items}
        # obs-j2 supplies only `url` (no source_url): the fallback must work.
        self.assertEqual(by_id["obs-j2"]["url"], "https://jobs.example.test/posting/driver-9")
        self.assertTrue(by_id["obs-j1"]["publishable"])
        self.assertTrue(by_id["obs-h1"]["publishable"])
        self.assertFalse(by_id["obs-j2"]["publishable"])
        self.assertFalse(by_id["obs-n1"]["publishable"])
        self.assertFalse(by_id["obs-r1"]["publishable"])
        summary = self.by_org["444444444"]["external"]["summary"]
        self.assertEqual(summary["total"], 5)
        self.assertEqual(summary["publishable"], 2)
        self.assertEqual(summary["exactJobs"], 1)
        self.assertEqual(summary["totalJobs"], 2)
        self.assertEqual(self.by_org["555555555"]["external"]["items"][0]["platform"], "x")

    def test_changes_merged_from_history_and_envelope(self):
        changes = self.by_org["333333333"]["changes"]
        self.assertEqual(len(changes), 3)
        self.assertEqual(
            {change["field"] for change in changes},
            {"registry.employees", "financials.records", "website.title"},
        )
        employees = next(c for c in changes if c["field"] == "registry.employees")
        self.assertEqual(employees["oldValue"], 40)
        self.assertEqual(employees["newValue"], 45)
        self.assertTrue(employees["sourceUrl"])
        self.assertEqual(self.by_org["111111111"]["changes"], [])
        self.assertEqual(self.by_org["222222222"]["changes"], [])

    def test_baseline_copy_present_in_template(self):
        self.assertIn("belum ada snapshot sebelumnya", self.html)
        self.assertIn('id="change-timeline"', self.html)

    def test_jobs_status_derivation(self):
        self.assertEqual(self.by_org["111111111"]["jobsStatus"], "none")  # contract claim not_available
        self.assertEqual(self.by_org["222222222"]["jobsStatus"], "not_checked")
        self.assertEqual(self.by_org["333333333"]["jobsStatus"], "not_checked")
        self.assertEqual(self.by_org["444444444"]["jobsStatus"], "has")
        self.assertEqual(self.by_org["555555555"]["jobsStatus"], "none")  # observations, no postings
        self.assertEqual(derive_jobs(None, [])["status"], "not_checked")

    def test_group_summary_does_not_invent_relations(self):
        group = self.by_org["111111111"]["group"]
        self.assertEqual(group["status"], "available")
        self.assertEqual(group["summary"]["keyCount"], 2)
        self.assertEqual(group["summary"]["listItems"], 1)
        self.assertEqual(self.by_org["222222222"]["group"]["status"], "not_found")
        self.assertEqual(self.by_org["333333333"]["group"]["status"], "not_applicable")
        self.assertEqual(summarize_group(None)["kind"], "none")
        self.assertEqual(summarize_group([1, 2])["listItems"], 2)

    def test_contract_envelope_summary(self):
        contract = self.by_org["111111111"]["contract"]
        self.assertEqual(contract["terminalStatus"], "completed")
        self.assertEqual(contract["errorCount"], 0)
        self.assertEqual(contract["claimCount"], 2)
        self.assertEqual(contract["thirdPartyCostUsd"], 0)
        self.assertIsNone(self.by_org["222222222"]["contract"])

    def test_missing_is_not_zero(self):
        minimal = self.by_org["222222222"]
        self.assertIsNone(minimal["employees"])
        self.assertEqual(minimal["financial"]["records"], [])
        self.assertIn("Missing is not zero", self.html)

    def test_quarantine_replicates_test_poc(self):
        row = {
            "organisation_number": "923609016",
            "name": "Subsidiary AS",
            "legal_form": "AS",
            "employees": None,
            "municipality": "OSLO",
            "industry_code": None,
            "industry_label": None,
            "website": "https://parent.test",
            "bankrupt": False,
            "liquidating": False,
            "evidence": {
                "website": evidence(
                    "website",
                    "available",
                    "company_site",
                    "https://parent.test",
                    value={
                        "identity_assessment": {"publishable": False},
                        "social_links": [],
                        "discovered_social_links": [
                            {"platform": "linkedin", "url": "https://linkedin.com/company/parent"}
                        ],
                    },
                )
            },
        }
        website = compact(row)["web"]["value"]
        self.assertEqual(website["quarantined_social_count"], 1)
        self.assertEqual(website["social_links"], [])
        quarantined = self.by_org["555555555"]["web"]["value"]
        self.assertEqual(quarantined["quarantined_social_count"], 2)
        self.assertEqual(quarantined["title"], "Registry-linked site — identity not verified")

    def test_qualification_copy_compat(self):
        """Replicates PrototypeTests in test_poc.py: the deprecated shim stays compatible."""
        header, boundary = qualification_copy({
            "weighted_score": {"verified_points": 100, "maximum_points": 100},
            "qualification": {"poc_qualified": True, "production_qualified": False},
        })
        self.assertIn("100/100", header)
        self.assertIn("not production-qualified", header)
        self.assertIn("remain quarantined", boundary)

    def test_compare_filter_sort_controls_present(self):
        for marker in (
            'id="jobs"',
            'id="group"',
            'id="external"',
            'id="sort"',
            'value="has"',
            'value="not_checked"',
            'value="revenue-desc"',
            'value="employees-desc"',
            'id="compare-panel"',
            'compare-table',
            'compare-check',
            'COMPARE_MAX=4',
            'class="table-scroll"',
        ):
            self.assertIn(marker, self.html)
        self.assertGreaterEqual(self.html.count('class="table-scroll"'), 2)

    def test_responsive_css_breakpoints_and_scroll(self):
        self.assertIn("@media(max-width:1150px)", self.html)
        self.assertIn("@media(max-width:900px)", self.html)
        self.assertIn("@media(max-width:520px)", self.html)
        self.assertIn(".table-scroll{overflow-x:auto", self.html)
        self.assertIn(".compare-table{min-width:720px}", self.html)
        self.assertIn(".compare-table{min-width:640px}", self.html)

    def test_acquisition_modes_mirror_external_footprint(self):
        from norway_company_agent.external_footprint import PUBLISHABLE_ACQUISITION_MODES as canonical

        self.assertEqual(PUBLISHABLE_ACQUISITION_MODES, canonical)

    def test_build_agent_answers_handles_research_failure_visibly(self):
        def boom(_row, _question):
            raise ValueError("boom")

        answers = build_agent_answers(self.rows[0], [], boom)
        self.assertEqual(answers["overview"]["facts"], [])
        self.assertTrue(any("boom" in item for item in answers["overview"]["unsupported_or_uncertain"]))

    def test_run_meta_prefers_most_common_run_id(self):
        meta = derive_run_meta(3, self.envelopes)
        self.assertEqual(meta["runId"], "ux25-test-run")
        meta = derive_run_meta(3, self.envelopes, run_id="override-run", run_date="2026-11-13")
        self.assertEqual(meta["runId"], "override-run")
        self.assertEqual(meta["completedAt"], "2026-11-13")

    def test_observation_normalization_defaults(self):
        item = normalize_observation({"platform": "news", "signal_type": "public_mention"})
        self.assertFalse(item["publishable"])
        self.assertFalse(item["exactEntity"])
        self.assertIsNone(item["url"])

    def test_cli_build_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "site" / "index.html"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/build_prototype.py",
                    "--input",
                    str(FIXTURES / "prototype-ux25-profiles.jsonl"),
                    "--external-observations",
                    str(FIXTURES / "prototype-ux25-observations.jsonl"),
                    "--contract-envelopes",
                    str(FIXTURES / "prototype-ux25-envelopes.jsonl"),
                    "--output",
                    str(output),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("5 profiles", result.stdout)
            html = output.read_text(encoding="utf-8")
            self.assertIn("5 companies · run ux25-test-run · 2026-11-12", html)
            self.assertIn('id="compare-panel"', html)

    def test_cli_ignores_score_inputs_with_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            score_file = Path(tmp) / "score.json"
            score_file.write_text(json.dumps({"raw_score": 99}), encoding="utf-8")
            output = Path(tmp) / "index.html"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/build_prototype.py",
                    "--input",
                    str(FIXTURES / "prototype-ux25-profiles.jsonl"),
                    "--score",
                    str(score_file),
                    "--output",
                    str(output),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ignored", result.stderr)
            html = output.read_text(encoding="utf-8")
            self.assertNotIn("core-score", html)
            self.assertNotIn("/100", html)
            self.assertIn("This prototype makes no competition-score claim.", html)


if __name__ == "__main__":
    unittest.main()
