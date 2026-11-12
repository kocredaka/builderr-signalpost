# Signalpost — company research agent (builderr.ai challenge entry)

A company-research agent for Norwegian companies, keyed strictly on the
9-digit organisation number. Given a batch of organisation numbers it
returns one structured, source-linked profile per company — registry
details, people, locations, financials, group structure, company-website
evidence, Wikidata facts and dated YouTube activity — with every fact
carrying its source and retrieval date, plus refresh/change tracking and
a static comparison UI.

Built on the [builderr Signalpost starter kit](https://www.builderr.ai/signalpost-starter-kit.tar.gz)
(MIT, © 2026 builderr; see `LICENSE`). The kit's evidence pipeline,
identity gate and batch contract are kept; this entry adds a generic
`OUTPUT_CONTRACT.md` adapter, rule-based synthesis, full bulk-column
coverage, normalized group/roles/financials, deep company-website
extraction (sitemap, contacts, jobs, dated news, structured data),
Wikidata (P2333) and YouTube-RSS activity modules, and a rebuilt
comparison prototype. See `STRATEGI.md` for architecture, scoring
targets and honest limitations, and `SMOKE_TEST.md` for measured
evidence.

**Cost: $0 per run.** No model, no paid API, no personal credential.
All sources are public and keyless (see "Sources" below).

## Install

Requires Python 3.12+ and [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync
```

Pinned dependencies live in `uv.lock`. No other install step is needed.

## Inputs

```bash
curl -L 'https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv' -o brreg-enheter.csv
curl -L 'https://builderr.ai/signalpost-company-universe-2025.jsonl.gz' -o signalpost-universe.jsonl.gz
```

The universe file (411,160 eligible companies) is only needed to draw a
local test batch. The official batch is supplied by Builderr.

```bash
# Local 100-company smoke-test batch (seeded, reproducible).
uv run python select_entry_batch.py \
  --universe signalpost-universe.jsonl.gz \
  --count 100 --seed 20260823 \
  --output out/smoke-companies.jsonl
```

## One run command

```bash
bash run.sh <organisations.jsonl> <brreg-enheter.csv> <out-dir> <run-id> [expected-count] [previous-profiles.jsonl]
```

Example (the 100-company smoke test):

```bash
bash run.sh out/smoke-companies.jsonl brreg-enheter.csv out smoke-001 100
```

`run.sh` runs `uv sync`, then the batch runner, then builds the
prototype UI. It is equivalent to:

```bash
uv run python scripts/run_competition_batch.py \
  --organisations out/smoke-companies.jsonl \
  --bulk brreg-enheter.csv \
  --profiles-output out/profiles.jsonl \
  --output out/envelopes.jsonl \
  --report out/run-report.json \
  --run-id smoke-001 \
  --expected-count 100 \
  --contract-output out/contract.jsonl \
  [--previous-profiles out/previous-profiles.jsonl]

uv run python scripts/build_prototype.py \
  --input out/profiles.jsonl \
  --contract-envelopes out/contract.jsonl \
  --run-id smoke-001 \
  --output out/prototype.html
```

Outputs, per run:

| File | Contents |
| --- | --- |
| `envelopes.jsonl` | One terminal envelope per input (kit contract). |
| `contract.jsonl` | One `OUTPUT_CONTRACT.md` envelope per input: flat `claims[]` + `evidence[]`, `changes[]`, `errors[]`, `operations` (requests, runtime, `third_party_cost_usd: 0`). |
| `profiles.jsonl` | Full internal profiles (evidence records + hashes). Feed a previous run's file back via `--previous-profiles` for refresh diffing. |
| `run-report.json` | Machine-readable report: counts, module states, operations, refresh summary, validation checks. |
| `prototype.html` | Single-file static UI: search/filter/sort, 2–4 company comparison, change timeline, per-fact source links. No server needed. |

Validation is strict: the run fails loudly unless every input yields
exactly one terminal envelope with terminal module states and zero
silent drops (5/5 checks in the report).

## Refresh

Pass `--previous-profiles <earlier profiles.jsonl>` to diff the new run
against a previous snapshot. `changes[]` in each contract envelope lists
only material field changes (tracked fields in `src/norway_company_agent/refresh.py`);
re-running unchanged data yields zero changes (verified: two independent
live runs 10 minutes apart produced 0 changes; a real group-structure
correction produced exactly 6 changed companies, all `group.relationships`
— see `SMOKE_TEST.md`).

## Evidence modules (default `--modules`)

| Module | Source | Keying |
| --- | --- | --- |
| `registry` | Brreg bulk snapshot (frozen CSV) | Exact org number; all ~90 columns mapped |
| `accounting_obligation` | Derived from registry + filing rules | Org number |
| `registry_live` | `data.brreg.no` entity API | Org number, echo-checked |
| `financials` | `data.brreg.no` accounts API | Org number, echo-checked |
| `roles` | `data.brreg.no` roles API | Org number |
| `group` | `data.brreg.no` konsernstruktur API | Org number must appear anywhere in the tree (trees are parent-rooted) |
| `locations` | `data.brreg.no` subunits API (paged) | `overordnetEnhet` filter |
| `website` | Registry-listed site (+ Wikidata candidates), bounded crawl | Identity gate, threshold 0.9; sitemap/feed discovery, contacts, JobPosting, dated news, structured data |
| `wikidata` | Wikidata Query Service, P2333 | Exact P2333 + label token cross-check; chunked at 200 orgs/query |
| `youtube_activity` | Public YouTube channel RSS | Only gate-verified handles from the company site + channel-name check |

`financial_history` (annual-report PDF copies) is available via
`--modules` but off by default: it multiplies per-company latency past
the practical batch budget. See `STRATEGI.md`.

## Sources, licences, rights

| Source | Licence / terms | Key? |
| --- | --- | --- |
| Brønnøysundregistrene (Enhetsregisteret bulk + APIs, Regnskapsregisteret) | Norwegian Licence for Open Government Data (NLOD 2.0) | No |
| Wikidata / Wikidata Query Service | CC0 | No (identifying User-Agent, polite rate) |
| Company websites (public HTML, sitemap, RSS/Atom, JSON-LD) | Public pages; robots.txt respected, bounded crawl, SSRF guard | No |
| YouTube public channel RSS feeds | Public feeds; no API key, no scraping of watch pages | No |

No model is used. No third-party API cost is incurred. No personal
credential, cookie, or account is used anywhere. Sentiment is explicitly
abstained (no model, no review scraping). Sources that would need a key,
a login, or a terms-sensitive scrape (NAV, Brave, LinkedIn, Google Maps,
GDELT) were evaluated and rejected; see `STRATEGI.md`.

## Identity & precision rules

- The organisation number is the only join key. A 200 response that
  names a different company is rejected (`source_error`), never trusted.
- Website claims publish only when the identity gate scores ≥ 0.9
  (name, org number / VAT ID, domain, footer excerpt, sameAs). Below
  that, claims stay `ambiguous` and social links stay quarantined.
- Wikidata matches require an exact P2333 value plus a shared
  distinctive name token; multiple QIDs for one org number reject.
- YouTube activity requires a gate-verified handle AND a channel-name
  subset match.
- Missing values are `not_available`, never zero. Unknown dates are
  null, never guessed. Registry values are never overwritten by site
  values; disagreements are reported side by side with both sources.

## Tests

```bash
uv run --with pytest python -m pytest tests -q
```

249 tests + 18 subtests, all offline (mocked fetchers/fixtures).
`tests/fixtures/` carries the SPARQL, RSS, sitemap and refresh fixtures.

## Repository layout

- `src/norway_company_agent/` — evidence modules, contract adapter
  (`contract.py`), synthesis (`synthesis.py`), website extraction
  (`website.py`, `site_extract.py`), Wikidata (`wikidata.py`),
  YouTube RSS (`youtube_rss.py`), refresh (`refresh.py`).
- `scripts/run_competition_batch.py` — the batch runner (single entry point).
- `scripts/build_prototype.py` — the static comparison UI builder.
- `scripts/` — other kit scripts (research-agent evaluation, connectors,
  replay). Not on the scored path; retained from the kit.
- `docs/` — kit design docs, plus `docs/website-evidence-schema.md`
  (new: website evidence schema, budgets, limits) and
  `docs/audit-2026-11-12.md` (the dated audit this entry was built from;
  improvement items #1–#26, in Indonesian).
- `out/` — curated smoke-test artifacts (see `SMOKE_TEST.md`).
- `OUTPUT_CONTRACT.md` — the evaluator contract this entry targets.

## Known limitations (short version)

- Website coverage depends on the registry-listed URL: in the
  100-company smoke batch only 7 companies had one. Jobs/news/activity
  come only from company-owned sites and verified RSS feeds — there is
  no job-board, news-aggregator, or review coverage.
- No browser fallback: JavaScript-heavy sites yield only static HTML.
- The BRREG live endpoints throttle to ~2.1 s/company; a 1,000-company
  run takes ~35–40 minutes (measured extrapolation from the 100-company
  smoke runs at ~4–5 minutes).
- In sandboxed networks whose DNS resolver returns non-global
  (CGNAT) addresses, the SSRF guard blocks all website fetches; this is
  the guard working as designed, and website extraction was verified
  separately against real captured site bytes. See `SMOKE_TEST.md`.

Full discussion, scoring targets and rejected alternatives: `STRATEGI.md`.
Submission details (models/APIs/cost/run command): `SUBMISSION.md`.
