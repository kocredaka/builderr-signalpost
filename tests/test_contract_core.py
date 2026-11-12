from __future__ import annotations

import copy
import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from bs4 import BeautifulSoup  # noqa: E402
from norway_company_agent.contract import contract_envelope, validate_contract_envelope  # noqa: E402
from norway_company_agent.evidence import evidence  # noqa: E402
from norway_company_agent.http import FetchResult, _effective_at_from_headers  # noqa: E402
from norway_company_agent.identity import assess_website_identity  # noqa: E402
from norway_company_agent.official import (  # noqa: E402
    fetch_official_modules,
    normalize_entity,
    normalize_financials,
    normalize_group,
    normalize_locations,
)
from norway_company_agent.refresh import TRACKED_FIELDS, diff_profile  # noqa: E402
from norway_company_agent.sampling import iter_bulk, normalize_row  # noqa: E402
from norway_company_agent.synthesis import synthesize_profile  # noqa: E402
from norway_company_agent.website import _identity_text_excerpt, fetch_website  # noqa: E402
from scripts.run_competition_batch import read_previous_profiles  # noqa: E402


def _profile(**overrides):
    base = {
        "organisation_number": "923609016",
        "name": "Example AS",
        "legal_form": "AS",
        "employees": 4,
        "municipality": "OSLO",
        "website": "example.no",
        "activity": "Makes cheese.",
        "industry_code": "10.510",
        "industry_label": "Cheese making",
        "email": "post@example.no",
        "run_metrics": {"requests": 6, "bytes": 100, "latencies_ms": [10, 20]},
        "evidence": {
            "registry": evidence(
                "registry", "available", "official_registry_bulk",
                "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
                value={"organisasjonsnummer": "923609016", "navn": "Example AS", "epostadresse": "post@example.no", "telefon": ""},
                content_sha256="a" * 64, retrieved_at="2026-08-24T06:00:00Z",
            ),
            "registry_live": evidence(
                "registry_live", "available", "official_registry_live",
                "https://data.brreg.no/enhetsregisteret/api/enheter/923609016",
                value={"organisation_number": "923609016", "employees": 0, "bankrupt": False, "phone": None},
                content_sha256="b" * 64, retrieved_at="2026-08-24T06:00:01Z",
            ),
            "financials": evidence(
                "financials", "available", "official_annual_accounts",
                "https://data.brreg.no/regnskapsregisteret/regnskap/923609016",
                value={"records": [{"revenue": 100, "currency": "NOK"}]},
                content_sha256="c" * 64, retrieved_at="2026-08-24T06:00:02Z",
            ),
            "roles": evidence(
                "roles", "not_found", "official_roles",
                "https://data.brreg.no/enhetsregisteret/api/enheter/923609016/roller",
                note="HTTP 404", content_sha256="d" * 64, retrieved_at="2026-08-24T06:00:03Z",
            ),
            "group": evidence(
                "group", "source_error", "official_group_structure",
                "https://data.brreg.no/enhetsregisteret/api/konsernstruktur/923609016",
                note="HTTP 500", content_sha256="e" * 64, retrieved_at="2026-08-24T06:00:04Z",
            ),
            "locations": evidence(
                "locations", "available", "official_subunits",
                "https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet=923609016",
                value={"locations": [], "total_elements": 0, "fetched": 0, "truncated": False},
                content_sha256="f" * 64, retrieved_at="2026-08-24T06:00:05Z",
            ),
            "website": evidence(
                "website", "available", "registry_linked_company_website", "https://example.no/",
                value={
                    "title": "Example AS", "description": "We make cheese.",
                    "identity_assessment": {"publishable": True, "status": "exact", "score": 1.0},
                },
                content_sha256="a1" * 32, retrieved_at="2026-08-24T06:00:06Z",
            ),
        },
    }
    base.update(overrides)
    return base


class ContractAdapterTests(unittest.TestCase):
    def test_round_trip_every_claim_points_at_real_evidence(self):
        envelope = contract_envelope(
            _profile(), run_id="r1",
            started_at="2026-08-24T06:00:00Z", completed_at="2026-08-24T06:00:08Z",
        )
        self.assertEqual(validate_contract_envelope(envelope), [])
        self.assertEqual(envelope["organisation_number"], "923609016")
        self.assertEqual(envelope["run"], {
            "run_id": "r1",
            "started_at": "2026-08-24T06:00:00Z",
            "completed_at": "2026-08-24T06:00:08Z",
            "terminal_status": "completed",
        })
        self.assertEqual(envelope["operations"]["third_party_cost_usd"], 0)
        self.assertEqual(envelope["operations"]["runtime_ms"], 8000)
        self.assertEqual(envelope["operations"]["requests"], 6)
        self.assertEqual(envelope["changes"], [])
        self.assertEqual([item["module"] for item in envelope["errors"]], ["group"])
        fields = {claim["field"] for claim in envelope["claims"]}
        self.assertIn("profile_synthesis", fields)
        self.assertIn("registry.epostadresse", fields)

    def test_missing_is_availability_not_zero_and_zero_stays_zero(self):
        envelope = contract_envelope(
            _profile(), run_id="r1",
            started_at="2026-08-24T06:00:00Z", completed_at="2026-08-24T06:00:01Z",
        )
        by_field = {claim["field"]: claim for claim in envelope["claims"]}
        self.assertEqual(by_field["registry.telefon"]["availability"], "not_available")
        self.assertIsNone(by_field["registry.telefon"]["value"])
        self.assertEqual(by_field["registry_live.phone"]["availability"], "not_available")
        self.assertEqual(by_field["registry_live.employees"]["value"], 0)
        self.assertEqual(by_field["registry_live.employees"]["availability"], "available")
        self.assertIs(by_field["registry_live.bankrupt"]["value"], False)
        self.assertEqual(by_field["locations.locations"]["value"], [])
        self.assertEqual(by_field["locations.locations"]["availability"], "available")

    def test_status_mapping(self):
        envelope = contract_envelope(
            _profile(), run_id="r1",
            started_at="2026-08-24T06:00:00Z", completed_at="2026-08-24T06:00:01Z",
        )
        by_field = {claim["field"]: claim for claim in envelope["claims"]}
        self.assertEqual(by_field["roles"]["availability"], "not_available")
        self.assertEqual(by_field["group"]["availability"], "failed")
        profile = _profile()
        profile["evidence"]["roles"] = evidence("roles", "blocked", "official_roles", "https://example.test", note="blocked")
        profile["evidence"]["group"] = evidence("group", "not_fetched", "official_group_structure", "https://example.test")
        envelope = contract_envelope(profile, run_id="r1", started_at="2026-08-24T06:00:00Z", completed_at="2026-08-24T06:00:01Z")
        by_field = {claim["field"]: claim for claim in envelope["claims"]}
        self.assertEqual(by_field["roles"]["availability"], "blocked")
        self.assertEqual(by_field["group"]["availability"], "failed")
        self.assertEqual(envelope["run"]["terminal_status"], "failed")

    def test_generic_over_future_modules_without_code_change(self):
        profile = _profile()
        profile["evidence"]["future_module"] = evidence(
            "future_module", "available", "official_future",
            "https://example.test/future",
            value={"new_field": "new value", "items": [{"a": 1}, {"a": 2}]},
            content_sha256="b2" * 32, retrieved_at="2026-08-24T06:00:07Z",
        )
        envelope = contract_envelope(profile, run_id="r1", started_at="2026-08-24T06:00:00Z", completed_at="2026-08-24T06:00:01Z")
        self.assertEqual(validate_contract_envelope(envelope), [])
        fields = {claim["field"] for claim in envelope["claims"]}
        self.assertIn("future_module.new_field", fields)
        self.assertIn("future_module.items[0]", fields)
        self.assertIn("future_module.items[1]", fields)

    def test_quarantined_website_claims_are_ambiguous(self):
        profile = _profile()
        profile["evidence"]["website"]["value"]["identity_assessment"] = {
            "publishable": False, "status": "review", "score": 0.85,
        }
        envelope = contract_envelope(profile, run_id="r1", started_at="2026-08-24T06:00:00Z", completed_at="2026-08-24T06:00:01Z")
        by_field = {claim["field"]: claim for claim in envelope["claims"]}
        self.assertEqual(by_field["website.description"]["availability"], "ambiguous")
        self.assertEqual(by_field["website.description"]["confidence"], 0.85)
        self.assertEqual(by_field["website.identity_assessment"]["availability"], "available")

    def test_validator_catches_orphan_claims(self):
        envelope = contract_envelope(
            _profile(), run_id="r1",
            started_at="2026-08-24T06:00:00Z", completed_at="2026-08-24T06:00:01Z",
        )
        envelope["claims"].append({"field": "x", "value": 1, "availability": "available", "confidence": 1.0, "evidence_ids": ["ev-999"]})
        self.assertTrue(any("ev-999" in problem for problem in validate_contract_envelope(envelope)))


class RefreshIntegrationTests(unittest.TestCase):
    def test_identical_second_run_reports_zero_changes(self):
        profile = _profile()
        self.assertEqual(diff_profile(profile, copy.deepcopy(profile)), [])

    def test_new_tracked_fields_detect_changes_with_provenance(self):
        old, new = _profile(), _profile()
        new["email"] = "new@example.no"
        new["evidence"]["group"] = evidence(
            "group", "available", "official_group_structure", "https://example.test/group",
            value={"relationships": [{"from_organisation_number": "923609016", "to_organisation_number": "111111111"}]},
            content_sha256="c3" * 32, retrieved_at="2026-08-25T00:00:00Z", effective_at="2026-08-24T00:00:00Z",
        )
        new["evidence"]["website"]["value"]["content_sha256"] = "d4" * 32
        changes = {change["field"]: change for change in diff_profile(old, new)}
        self.assertIn("registry.email", changes)
        self.assertIn("group.relationships", changes)
        self.assertIn("website.content_sha256", changes)
        self.assertEqual(changes["registry.email"]["old_value"], "post@example.no")
        self.assertEqual(changes["registry.email"]["new_value"], "new@example.no")
        self.assertEqual(changes["group.relationships"]["source_url"], "https://example.test/group")
        self.assertEqual(changes["group.relationships"]["effective_at"], "2026-08-24T00:00:00Z")

    def test_old_format_profiles_do_not_gain_spurious_changes(self):
        old = {"organisation_number": "923609016", "name": "Example AS", "employees": 4}
        new = _profile()
        new["employees"] = 4
        new["email"] = ""
        new["phone"] = ""
        new["capital"] = None
        changes = diff_profile(old, new)
        fields = {change["field"] for change in changes}
        self.assertNotIn("registry.email", fields)
        self.assertNotIn("registry.phone", fields)
        self.assertNotIn("registry.capital", fields)
        self.assertNotIn("registry.employees", fields)

    def test_tracked_fields_cover_group_contact_and_site_hash(self):
        self.assertIn("group.relationships", TRACKED_FIELDS)
        self.assertIn("registry.email", TRACKED_FIELDS)
        self.assertIn("registry.capital", TRACKED_FIELDS)
        self.assertIn("website.content_sha256", TRACKED_FIELDS)
        self.assertIn("financial_history.years", TRACKED_FIELDS)

    def test_previous_profiles_reader_accepts_profiles_and_envelopes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prev.jsonl"
            path.write_text(
                json.dumps({"organisation_number": "111111111", "name": "A AS"}) + "\n"
                + json.dumps({"organisation_number": "222222222", "profile": {"organisation_number": "222222222", "name": "B AS"}}) + "\n",
                encoding="utf-8",
            )
            previous = read_previous_profiles(path)
        self.assertEqual(previous["111111111"]["name"], "A AS")
        self.assertEqual(previous["222222222"]["name"], "B AS")

    def test_effective_at_from_response_headers(self):
        self.assertEqual(
            _effective_at_from_headers({"Date": "Thu, 12 Nov 2026 06:16:05 GMT"}),
            "2026-11-12T06:16:05Z",
        )
        self.assertEqual(
            _effective_at_from_headers({"Date": "Thu, 12 Nov 2026 06:16:05 GMT", "Last-Modified": "Wed, 11 Nov 2026 10:00:00 GMT"}),
            "2026-11-11T10:00:00Z",
        )
        self.assertIsNone(_effective_at_from_headers({"Date": "not a date"}))
        self.assertIsNone(_effective_at_from_headers({}))


class BulkNormalizationTests(unittest.TestCase):
    def test_expanded_row_maps_verified_bulk_columns(self):
        row = normalize_row({
            "organisasjonsnummer": "923609016",
            "navn": "Example AS",
            "organisasjonsform.kode": "AS",
            "organisasjonsform.beskrivelse": "Aksjeselskap",
            "epostadresse": "post@example.no",
            "telefon": "22 00 00 00",
            "mobil": "900 00 000",
            "aktivitet": "Makes cheese.",
            "vedtektsfestetFormaal": "Make cheese.",
            "stiftelsesdato": "2001-01-01",
            "registreringsdatoenhetsregisteret": "2001-02-01",
            "vedtektsdato": "2020-01-01",
            "registrertIMvaRegisteret": "true",
            "registreringsdatoMerverdiavgiftsregisteret": "2001-03-01",
            "kapital.belop": "1000000.0",
            "kapital.antallAksjer": "1000",
            "kapital.valuta": "NOK",
            "kapital.type": "Aksjekapital",
            "kapital.innbetalt": "500000.0",
            "kapital.fulltInnbetalt": "false",
            "konkursdato": "",
            "naeringskode2.kode": "10.520",
            "naeringskode2.beskrivelse": "Ice cream",
            "overordnetEnhet": "111111111",
            "erIKonsern": "false",
            "maalform": "Bokmål",
            "institusjonellSektorkode.kode": "6100",
            "hjelpeenhetskode.kode": "62.200",
            "paategninger": "Note.",
            "forretningsadresse.adresse": "Gate 1",
            "forretningsadresse.postnummer": "0150",
            "forretningsadresse.kommune": "OSLO",
        })
        self.assertEqual(row["email"], "post@example.no")
        self.assertEqual(row["phone"], "22 00 00 00")
        self.assertEqual(row["mobile"], "900 00 000")
        self.assertEqual(row["activity"], "Makes cheese.")
        self.assertEqual(row["statutory_purpose"], "Make cheese.")
        self.assertEqual(row["founded_date"], "2001-01-01")
        self.assertEqual(row["registration_date"], "2001-02-01")
        self.assertIs(row["vat_registered"], True)
        self.assertEqual(row["capital"]["amount"], 1000000.0)
        self.assertEqual(row["capital"]["shares"], 1000)
        self.assertEqual(row["capital"]["paid_in"], 500000.0)
        self.assertIs(row["capital"]["fully_paid"], False)
        self.assertEqual(row["bankruptcy_date"], "")
        self.assertEqual(row["industry_code_2"], "10.520")
        self.assertEqual(row["parent_unit"], "111111111")
        self.assertIs(row["in_group"], False)
        self.assertEqual(row["business_address"]["address"], "Gate 1")
        self.assertIsNone(row["postal_address"])
        self.assertEqual(row["raw"]["navn"], "Example AS")

    def test_blank_booleans_stay_unknown_not_false(self):
        row = normalize_row({"organisasjonsnummer": "923609016", "navn": "Example AS"})
        self.assertIsNone(row["vat_registered"])
        self.assertIsNone(row["in_group"])
        self.assertIsNone(row["capital"])

    def test_real_fixture_row_keeps_capital_and_activity(self):
        source = ROOT / "tests" / "fixtures" / "bulk_quoted_row.csv"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bulk.csv.gz"
            with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
                handle.write(source.read_text(encoding="utf-8"))
            rows = {row["organisation_number"]: row for row in iter_bulk(path)}
        row = rows["929455754"]
        self.assertEqual(row["capital"]["amount"], 33000.0)
        self.assertTrue(row["activity"].startswith("Direkte eller indirekte"))
        self.assertEqual(len(rows), 31)


class LiveNormalizationTests(unittest.TestCase):
    def test_entity_maps_verified_live_fields_and_both_vat_spellings(self):
        body = {
            "organisasjonsnummer": "923609016",
            "navn": "EQUINOR ASA",
            "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allmennaksjeselskap"},
            "antallAnsatte": 0,
            "konkurs": False,
            "telefon": "51 99 00 00",
            "aktivitet": ["Oil", "Gas"],
            "vedtektsfestetFormaal": ["Purpose one"],
            "stiftelsesdato": "1972-09-18",
            "registreringsdatoEnhetsregisteret": "1995-03-12",
            "registrertIMvaregisteret": True,
            "naeringskode2": {"kode": "06.200", "beskrivelse": "Gas"},
            "kapital": {"belop": 5976872600.0, "valuta": "NOK"},
            "erIKonsern": True,
            "historiskeNavn": [{"navn": "Old Name"}],
            "_links": {"self": {"href": "https://example.test"}},
        }
        record = normalize_entity(body)
        self.assertEqual(record["employees"], 0)
        self.assertIs(record["bankrupt"], False)
        self.assertEqual(record["phone"], "51 99 00 00")
        self.assertEqual(record["activity"], ["Oil", "Gas"])
        self.assertIs(record["vat_registered"], True)
        self.assertEqual(record["industry_2"]["kode"], "06.200")
        self.assertEqual(record["capital"]["belop"], 5976872600.0)
        self.assertEqual(record["historical_names"], [{"navn": "Old Name"}])
        self.assertNotIn("_links", record["raw_body"])
        self.assertEqual(record["raw_body"]["navn"], "EQUINOR ASA")
        csv_spelling = normalize_entity({"registrertIMvaRegisteret": False})
        self.assertIs(csv_spelling["vat_registered"], False)

    def test_financials_preserve_breakdown_period_currency_and_zero(self):
        body = [{
            "id": 7,
            "regnskapstype": "KONSERN",
            "valuta": "USD",
            "regnskapsperiode": {"fraDato": "2023-01-01", "tilDato": "2023-12-31"},
            "virksomhet": {"organisasjonsnummer": "923609016", "morselskap": True},
            "resultatregnskapResultat": {
                "driftsresultat": {
                    "driftsresultat": 0,
                    "driftsinntekter": {"salgsinntekter": 10, "sumDriftsinntekter": 12},
                    "driftskostnad": {"sumDriftskostnad": 5},
                },
                "finansresultat": {
                    "nettoFinans": 3,
                    "finansinntekt": {"sumFinansinntekter": 4},
                    "finanskostnad": {"sumFinanskostnad": 1},
                },
                "ordinaertResultatFoerSkattekostnad": 8,
                "ordinaertResultatSkattekostnad": 2,
                "aarsresultat": 6,
            },
            "eiendeler": {
                "sumEiendeler": 80,
                "omloepsmidler": {"sumOmloepsmidler": 30},
                "anleggsmidler": {"sumAnleggsmidler": 50},
                "sumVarer": 1,
            },
            "egenkapitalGjeld": {
                "sumEgenkapitalGjeld": 80,
                "egenkapital": {"sumEgenkapital": 30, "innskuttEgenkapital": {"sumInnskuttEgenkaptial": 25}, "opptjentEgenkapital": {}},
                "gjeldOversikt": {"sumGjeld": 50, "kortsiktigGjeld": {"sumKortsiktigGjeld": 20}, "langsiktigGjeld": {"sumLangsiktigGjeld": 30}},
            },
        }] + [{"id": i} for i in range(4)]
        result = normalize_financials(body)
        record = result["records"][0]
        self.assertEqual(record["revenue"], 12)
        self.assertEqual(record["sales_revenue"], 10)
        self.assertEqual(record["operating_result"], 0)
        self.assertEqual(record["finance_income"], 4)
        self.assertEqual(record["net_finance"], 3)
        self.assertEqual(record["tax_cost"], 2)
        self.assertEqual(record["current_assets"], 30)
        self.assertEqual(record["fixed_assets"], 50)
        self.assertEqual(record["contributed_equity"], 25)
        self.assertEqual(record["current_debt"], 20)
        self.assertEqual(record["long_term_debt"], 30)
        self.assertEqual(record["currency"], "USD")
        self.assertEqual(record["period"]["tilDato"], "2023-12-31")
        self.assertEqual(len(result["records"]), 3)
        self.assertEqual(result["total_records"], 5)
        self.assertEqual(result["source_records"][0]["id"], 7)
        self.assertNotIn("margin", json.dumps(result))
        self.assertEqual(normalize_financials([]), {"records": [], "source_records": [], "total_records": 0})

    def test_group_only_publishes_relationships_with_two_valid_org_numbers(self):
        body = {
            "organisasjonsnummer": "923609016",
            "navn": "EQUINOR ASA",
            "children": [
                {"organisasjonsnummer": "996576027", "navn": "Child AS", "parentOrganisasjonsnummer": "923609016",
                 "parentNavn": "EQUINOR ASA", "knytningsform": {"kode": "KDAT", "beskrivelse": "Konsern datter"},
                 "grunnlag": "100%", "dato": "2025-11-12", "nivaa": 1},
                {"organisasjonsnummer": "short", "navn": "Bad Child"},
                {"navn": "No Number Child"},
            ],
        }
        result = normalize_group(body, "923609016")
        self.assertEqual(result["relationship_count"], 1)
        self.assertEqual(result["skipped_without_valid_org_numbers"], 2)
        rel = result["relationships"][0]
        self.assertEqual(rel["from_organisation_number"], "923609016")
        self.assertEqual(rel["to_organisation_number"], "996576027")
        self.assertEqual(rel["relationship_code"], "KDAT")
        self.assertEqual(rel["basis"], "100%")

    def test_locations_keep_business_and_postal_addresses_separate(self):
        body = {
            "_embedded": {"underenheter": [{
                "organisasjonsnummer": "973861883", "navn": "Sub",
                "beliggenhetsadresse": {"adresse": ["Factory 1"]},
                "postadresse": {"adresse": ["Postbox 1"]},
                "telefon": "123", "overordnetEnhet": "923609016",
            }]},
            "page": {"totalElements": 1, "number": 0, "totalPages": 1},
        }
        result = normalize_locations(body)
        item = result["locations"][0]
        self.assertEqual(item["business_address"], {"adresse": ["Factory 1"]})
        self.assertEqual(item["postal_address"], {"adresse": ["Postbox 1"]})
        self.assertEqual(item["phone"], "123")
        self.assertFalse(result["truncated"])
        self.assertEqual(result["total_elements"], 1)


class FetchOfficialModulesTests(unittest.TestCase):
    def _fetcher(self, mapping):
        def fetch(url):
            status, body = mapping[url]
            return FetchResult(url, status, 1, 10, body=body, error=None if status == 200 else f"HTTP {status}",
                               content_sha256="a" * 64, retrieved_at="2026-08-24T00:00:00Z")
        return fetch

    def test_group_404_is_not_found_not_error(self):
        org = "923609016"
        records, _ = fetch_official_modules(org, {"group"}, fetcher=self._fetcher({
            f"https://data.brreg.no/enhetsregisteret/api/konsernstruktur/{org}": (404, None),
        }))
        self.assertEqual(records["group"]["status"], "not_found")
        records, _ = fetch_official_modules(org, {"group"}, fetcher=self._fetcher({
            f"https://data.brreg.no/enhetsregisteret/api/konsernstruktur/{org}": (500, None),
        }))
        self.assertEqual(records["group"]["status"], "source_error")

    def test_org_number_mismatch_is_rejected(self):
        org = "923609016"
        records, _ = fetch_official_modules(org, {"registry_live"}, fetcher=self._fetcher({
            f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}": (200, {"organisasjonsnummer": "111111111", "navn": "Other AS"}),
        }))
        self.assertEqual(records["registry_live"]["status"], "source_error")
        self.assertIn("mismatch", records["registry_live"]["note"])
        self.assertIsNone(records["registry_live"]["value"])
        records, _ = fetch_official_modules(org, {"financials"}, fetcher=self._fetcher({
            f"https://data.brreg.no/regnskapsregisteret/regnskap/{org}": (200, [{"id": 1, "virksomhet": {"organisasjonsnummer": "222222222"}}]),
        }))
        self.assertEqual(records["financials"]["status"], "source_error")
        records, _ = fetch_official_modules(org, {"registry_live"}, fetcher=self._fetcher({
            f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}": (200, {"organisasjonsnummer": org, "navn": "Example AS"}),
        }))
        self.assertEqual(records["registry_live"]["status"], "available")

    def test_subunit_pagination_follows_pages_and_flags_truncation(self):
        org = "923609016"
        base = f"https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={org}&size=1000"
        calls = []

        def fetch(url):
            calls.append(url)
            page = 1 if "page=1" in url else 0
            body = {
                "_embedded": {"underenheter": [{"organisasjonsnummer": f"10000000{page}", "navn": f"Sub {page}", "overordnetEnhet": org}]},
                "page": {"number": page, "size": 1, "totalElements": 2, "totalPages": 2},
            }
            return FetchResult(url, 200, 1, 10, body=body, content_sha256="a" * 64, retrieved_at="2026-08-24T00:00:00Z")

        records, metrics = fetch_official_modules(org, {"locations"}, fetcher=fetch)
        value = records["locations"]["value"]
        self.assertEqual(len(value["locations"]), 2)
        self.assertEqual(value["pages_fetched"], 2)
        self.assertFalse(value["truncated"])
        self.assertEqual(len(metrics), 2)
        self.assertEqual(len(calls), 2)

        def failing_fetch(url):
            if "page=1" in url:
                return FetchResult(url, 500, 1, 0, error="HTTP 500", retrieved_at="2026-08-24T00:00:00Z")
            body = {
                "_embedded": {"underenheter": [{"organisasjonsnummer": "100000000", "navn": "Sub 0", "overordnetEnhet": org}]},
                "page": {"number": 0, "size": 1, "totalElements": 2, "totalPages": 2},
            }
            return FetchResult(url, 200, 1, 10, body=body, content_sha256="a" * 64, retrieved_at="2026-08-24T00:00:00Z")

        records, _ = fetch_official_modules(org, {"locations"}, fetcher=failing_fetch)
        self.assertTrue(records["locations"]["value"]["truncated"])
        self.assertIn("Stopped", records["locations"]["value"]["pagination_note"])
        self.assertEqual(len(records["locations"]["value"]["locations"]), 1)

    def test_subunit_with_wrong_parent_is_excluded_and_flagged(self):
        org = "923609016"
        url = f"https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={org}&size=1000"
        body = {
            "_embedded": {"underenheter": [
                {"organisasjonsnummer": "100000000", "navn": "Good", "overordnetEnhet": org},
                {"organisasjonsnummer": "100000001", "navn": "Stray", "overordnetEnhet": "999999999"},
            ]},
            "page": {"number": 0, "size": 1000, "totalElements": 2, "totalPages": 1},
        }
        records, _ = fetch_official_modules(org, {"locations"}, fetcher=self._fetcher({url: (200, body)}))
        value = records["locations"]["value"]
        self.assertEqual(len(value["locations"]), 1)
        self.assertEqual(value["excluded_mismatched"], 1)
        self.assertIn("Excluded 1", records["locations"]["note"])


class WebsiteIdentityGateTests(unittest.TestCase):
    def _website_profile(self, value):
        return {
            "organisation_number": "923609016",
            "name": "Example Cheese AS",
            "evidence": {"website": {"status": "available", "value": value}},
        }

    def test_vatid_exact_match_scores_one(self):
        for vat_id in ("923609016", "NO 923 609 016 MVA", "NO923609016MVA"):
            with self.subTest(vat_id=vat_id):
                profile = self._website_profile({
                    "title": "Unrelated title",
                    "structured_organisations": [{"@type": "Organization", "name": "Something Else", "vatID": vat_id}],
                })
                assessment = assess_website_identity(profile)
                self.assertEqual(assessment["score"], 1.0)
                self.assertTrue(assessment["publishable"])

    def test_vatid_mismatch_or_extra_digit_does_not_score_one(self):
        for vat_id in ("111111111", "9236090160", "92360901"):
            with self.subTest(vat_id=vat_id):
                profile = self._website_profile({
                    "title": "Unrelated title",
                    "structured_organisations": [{"@type": "Organization", "vatID": vat_id}],
                })
                self.assertNotEqual(assess_website_identity(profile)["score"], 1.0)
                self.assertFalse(assess_website_identity(profile)["publishable"])

    def test_footer_org_number_scores_one(self):
        profile = self._website_profile({
            "title": "Cheese shop",
            "identity_text_excerpt": "Example Cheese AS, Gate 1, Oslo. Org.nr. 923 609 016",
        })
        assessment = assess_website_identity(profile)
        self.assertEqual(assessment["score"], 1.0)
        self.assertTrue(assessment["publishable"])

    def test_secondary_page_footer_org_number_alone_is_not_homepage_identity(self):
        profile = self._website_profile({
            "title": "Group front page",
            "main_text_excerpt": "A group with many companies.",
            "pages": [{"title": "Contact", "main_text_excerpt": "Other text", "identity_text_excerpt": "Org.nr. 923 609 016"}],
        })
        assessment = assess_website_identity(profile)
        self.assertNotEqual(assessment["score"], 1.0)
        self.assertFalse(assessment["publishable"])

    def test_publishable_threshold_is_unchanged(self):
        profile = self._website_profile({"title": "Example Cheese"})
        assessment = assess_website_identity(profile)
        self.assertEqual(assessment["publishable"], assessment["score"] >= 0.9)

    def test_identity_excerpt_extracts_footer_and_address_nodes(self):
        soup = BeautifulSoup(
            "<html><body><main>Main text</main><footer>Example AS, Org 923609016</footer>"
            "<address>Gate 1, Oslo</address></body></html>", "html.parser",
        )
        excerpt = _identity_text_excerpt(soup)
        self.assertIn("923609016", excerpt)
        self.assertIn("Gate 1", excerpt)
        self.assertNotIn("Main text", excerpt)

    def test_urllib_fetch_merges_sameas_links_and_footer_excerpt(self):
        html = (
            "<html><head><title>Example Cheese AS</title>"
            '<meta name="description" content="We make cheese in Oslo for customers across Norway.">'
            '<script type="application/ld+json">{"@type": "Organization", "name": "Example Cheese AS",'
            '"vatID": "NO 923 609 016 MVA",'
            '"sameAs": ["https://instagram.com/examplecheese"]}</script></head>'
            "<body><p>Example Cheese AS makes cheese in Oslo. We have made cheese for many years and "
            "we sell cheese to shops and restaurants across the whole country every single week.</p>"
            '<a href="https://linkedin.com/company/examplecheese">LinkedIn</a>'
            "<footer>Example Cheese AS, Gate 1, 0150 Oslo. Org.nr. 923 609 016</footer></body></html>"
        ).encode()

        class FakeResponse:
            headers = {"content-type": "text/html; charset=utf-8"}

            def __init__(self, payload):
                self._payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _n=-1):
                return self._payload

            def geturl(self):
                return "https://example.no/"

        class FakeOpener:
            def open(self, _request, timeout=None):
                return FakeResponse(html)

        with (
            patch("norway_company_agent.website.SAFE_OPENER", FakeOpener()),
            patch("norway_company_agent.website.assert_public_url", lambda _url: None),
            patch("norway_company_agent.website._robots_allowed", lambda _url, _timeout: True),
        ):
            record, _metrics = fetch_website("example.no")
        self.assertEqual(record["status"], "available")
        value = record["value"]
        self.assertIn("923 609 016", value["identity_text_excerpt"])
        platforms = {item["platform"] for item in value["social_links"]}
        self.assertIn("instagram", platforms)
        self.assertIn("linkedin", platforms)


class SynthesisTests(unittest.TestCase):
    def test_every_statement_traces_to_evidence_and_baseline_is_marked(self):
        profile = _profile()
        result = synthesize_profile(profile, None)
        self.assertEqual(result["what_changed"][0]["evidence_ids"], [])
        self.assertIn("Baseline", result["what_changed"][0]["text"])
        for statement in result["what_it_does"]:
            self.assertTrue(statement["evidence_ids"])
        for entry in result["unknowns"]:
            self.assertTrue(entry["evidence_ids"])
        self.assertIn("roles", [entry["family"] for entry in result["unknowns"]])
        self.assertIn("group", [entry["family"] for entry in result["unknowns"]])

    def test_no_numbers_are_invented(self):
        profile = {
            "organisation_number": "923609016",
            "name": "",
            "evidence": {
                "registry": evidence("registry", "available", "official_registry_bulk", "https://example.test",
                                     value={"aktivitet": "Makes cheese."}, content_sha256="a" * 64),
                "financials": evidence("financials", "available", "official_annual_accounts", "https://example.test",
                                       value={"records": [{"revenue": 424242}]}, content_sha256="b" * 64),
            },
        }
        result = synthesize_profile(profile, [])
        joined = " ".join(statement["text"] for statement in result["what_it_does"])
        self.assertNotIn("424242", joined)
        self.assertNotIn("923609016", joined)

    def test_unknown_activity_is_stated_not_guessed(self):
        profile = {"organisation_number": "923609016", "name": "Example AS", "evidence": {
            "registry": evidence("registry", "available", "official_registry_bulk", "https://example.test", value={}, content_sha256="a" * 64),
        }}
        result = synthesize_profile(profile, [])
        self.assertTrue(any("not known" in statement["text"] for statement in result["what_it_does"]))

    def test_quarantined_website_description_is_not_synthesized(self):
        profile = _profile()
        profile["evidence"]["website"]["value"]["identity_assessment"] = {"publishable": False, "status": "review", "score": 0.85}
        profile["evidence"]["website"]["value"]["description"] = "Secret sauce recipe"
        result = synthesize_profile(profile, [])
        joined = " ".join(statement["text"] for statement in result["what_it_does"])
        self.assertNotIn("Secret sauce", joined)
        self.assertTrue(any(entry["family"] == "website" and entry["status"] == "identity_not_established" for entry in result["unknowns"]))

    def test_changes_render_old_and_new_values(self):
        profile = _profile()
        changes = [{"field": "registry.employees", "old_value": 2, "new_value": 0}]
        result = synthesize_profile(profile, changes)
        self.assertIn("from 2 to 0", result["what_changed"][0]["text"])
        result = synthesize_profile(profile, [])
        self.assertIn("No tracked field changed", result["what_changed"][0]["text"])

    def test_synthesis_uses_contract_evidence_ids(self):
        profile = _profile()
        result = synthesize_profile(profile, [], evidence_ids={"registry": "ev-9", "roles": "ev-3"})
        ids = {evidence_id for statement in result["what_it_does"] for evidence_id in statement["evidence_ids"]}
        self.assertIn("ev-9", ids)


if __name__ == "__main__":
    unittest.main()
