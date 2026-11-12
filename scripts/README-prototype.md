# Prototype UX (improvement #25)

`scripts/build_prototype.py` builds one static HTML file from local JSONL
files. Data is embedded as JSON; there is no backend, no network access
and no credential use. The prototype makes **no competition-score claim**.

## Run it

From the repository root:

```sh
python scripts/build_prototype.py \
  --input profiles.jsonl \
  --external-observations observations.jsonl \
  --contract-envelopes envelopes.jsonl \
  --output site/index.html
```

Then open `site/index.html` in a browser. All arguments except
`--input` and `--output` are optional.

| Argument | Meaning |
|---|---|
| `--input` | Profiles JSONL in kit shape (see below). |
| `--external-observations` | Generic observations JSONL, any platform; repeatable. |
| `--contract-envelopes` | `OUTPUT_CONTRACT.md` envelopes JSONL; supplies the batch header (run id + date) and `changes[]` for the timeline. |
| `--run-id`, `--run-date` | Header overrides. Must describe the actual batch; by default both come from the envelopes, and "not reported" is shown when no run metadata is available. |
| `--limit` | Build only the first N profiles. |
| `--score`, `--control-loop`, `--independent-score` | Accepted for backward compatibility but **ignored** (a warning is printed). No score is rendered. |

## Input contracts

**Profiles** (kit shape): top-level `organisation_number`, `name`,
`legal_form`, `employees`, `bankrupt`, `liquidating`, `municipality`,
`industry_code`/`industry_label`, `website`,
`latest_submitted_accounts`, `evidence` per module
(`field`/`status`/`source_type`/`source_class`/`source_url`/
`retrieved_at`/`value`/`as_of`/`note`/`content_sha256`/
`source_row_key`/`effective_at`), `run_metrics`; optional
`change_history`.

**Contract envelopes** (`OUTPUT_CONTRACT.md`): `organisation_number`,
`run{run_id, started_at, completed_at, terminal_status}`,
`claims[{field, value, availability, confidence, evidence_ids}]`,
`evidence[{id, source_url, source_class, retrieved_at, content_sha256,
claim_span}]`, `changes[]`, `errors[]`,
`operations{requests, runtime_ms, third_party_cost_usd}`. Envelope
`changes[]` are merged with the profile's `change_history`
(deduplicated, sorted by retrieval date).

**Observations** (generic, any platform): `id`,
`organisation_number`, `platform`, `signal_type`, `source_url` (or
`url`), `retrieved_at`, `content_sha256`, `exact_entity`,
`identity_proof`, `evidence_span`, `rights_status`,
`acquisition_mode`, plus optional `source_class`/`metrics`. Unknown
platforms and signal types render generically; nothing is
platform-specific.

## What the page does

- **Find**: text search (name/org/municipality/industry) + filters
  (legal form, status, accounts, slice, jobs, group, external
  signals) + sort (name, employees, revenue, latest accounts,
  municipality) + pin + save view + export JSON.
- **Compare**: tick 2–4 companies (checkbox in the list, or "Add to
  compare" on a profile) for a side-by-side table per family
  (identity, financials, people, locations, group, website, jobs,
  activity). Every cell shows its value, evidence status and source
  link. The table scrolls horizontally on small screens.
- **Verify**: "Inspect evidence" per section (source URL, class,
  retrieval date, effective date, SHA-256). Quarantined website
  claims stay quarantined (title/description replaced, only counts
  shown). "Missing is not zero" throughout.
- **External signals**: observations grouped by signal type, labelled
  by platform, rights status, acquisition mode and exact-entity
  verification. Items without approved rights + exact-entity
  verification are labelled "experimental — not a verified fact"
  and are never presented as facts.
- **Change timeline**: `changes[]` as a timeline (field, old → new,
  source, date, hashes). With no changes the section says
  "Baseline — belum ada snapshot sebelumnya" instead of staying
  silently empty.
- **Ask this company**: answers are precomputed at build time by
  importing `norway_company_agent.research.answer_profile` (the same
  Python logic as `scripts/ask_agent.py`) and embedded as data. The
  browser script only selects which precomputed mode to display; it
  never recomputes facts. A free-text question only routes to modes
  (keyword routing that mirrors the `research.py` topic terms, plus
  hiring/activity terms); the displayed facts always come from the
  embedded Python answers. Hiring/activity modes have no counterpart
  in `research.py`: they are derived in Python from the supplied
  observations and abstain explicitly when no supporting observation
  exists. Sentiment always abstains.

## Definitions that matter

- **Jobs filter**: `has` = a contract claim says job data is
  available, or an exact-entity job-posting observation exists.
  `none` = jobs were checked and no opening was found (contract
  claim `not_available`, or observations were supplied for the
  company but contain no job posting). This is *not* proof the
  company has no vacancies. `not_checked` = no contract claim and
  no observations for the company.
- **Group family**: the batch run stores the group endpoint body
  raw (no normalizer yet). The prototype shows only the evidence
  status plus a structural summary (field keys, list sizes); it
  never names a parent/subsidiary relation.
- **Publishable label** (display only): exact-entity verified +
  `rights_status == "approved"` + acquisition mode in the approved
  set (`official_api`, `licensed_api`, `company_authorized_export`,
  `permitted_public_page`, mirroring
  `external_footprint.PUBLISHABLE_ACQUISITION_MODES`) + URL + 64-hex
  hash. This is a conservative display label, not
  `publishable_observation()`.

## Responsive behaviour

Breakpoints 1150/900/520px are kept from the kit prototype. The
financial table and the compare table sit in explicit horizontal
scroll wrappers (`.table-scroll`); the compare table keeps a sticky
first column. Verified by CSS/structure checks in the test suite
only — no real-device testing was done.

## Tests

```sh
python -m unittest tests.test_prototype_ux25 -v
```

Stdlib-only. Fixtures (5 synthetic companies: complete, minimal,
with-changes, jobs/news observations, quarantined) live in
`tests/fixtures/prototype-ux25-{profiles,observations,envelopes}.jsonl`.
The suite also runs the embedded JavaScript logic through a
throwaway Node harness during development (not committed): filter,
sort, compare, timeline, quarantine and agent-mode rendering were
all exercised headlessly. The suite pins:

- header reflects the actual batch (count, run id, date);
- no stale copy ("100-company showcase", "22 exact LinkedIn
  profiles") and no score claim of any kind;
- no LinkedIn-specific rendering/monkey-patch; one `<script>` block;
- embedded `agentAnswers` equal `research.answer_profile` outputs
  for every research mode (equivalence test);
- hiring/activity/sentiment abstain explicitly without supporting
  evidence;
- generic observations render for any platform with rights labels;
- changes merge from `change_history` + envelopes;
- quarantine behaviour identical to `test_poc.py`'s PrototypeTests.

## Known limitations / requests to the coordinator

- `qualification_copy()` and `normalize_independent_score()` are
  retained as deprecated, unused shims only because `test_poc.py`
  (core worker owned) still imports and tests them. They render
  nothing. Request: retire the matching test so the shims can be
  deleted.
- Group relations cannot be shown until the core worker lands group
  normalization (audit #7).
- Hiring/activity/sentiment have no `research.py` topic; if the core
  worker adds them, `build_agent_answers()` should route through
  `research.py` instead of the local observation derivation.
