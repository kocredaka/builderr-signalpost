# Smoke test — 100 companies

Measured 2026-11-12 (WIB) on the final submission tree, unless noted.
Batch: `select_entry_batch.py --count 100 --seed 20260823` over the
official universe file (SHA-256 verified against the evaluation
contract); identical batch across all runs below. Bulk snapshot:
`brreg-enheter.csv` (155,275,979 bytes, 1,176,597 rows).

## Runs

| Run | Tree | Wall time | Envelopes | Validation | Contract problems | Requests | Errors |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `smoke-integrated-1` | Pre group-guard fix | 4m32s | 100/100 `complete` | 5/5 | 0 | 501 | 13 (7 website + 6 group) |
| `smoke-integrated-2` | Final tree, `--previous-profiles` = run 1 | 4m03s | 100/100 `complete` | 5/5 | 0 | 501 | 7 (website only) |
| `smoke-integrated-3` | Final tree, `--previous-profiles` = run 2 | 4m48s | 100/100 `complete` | 5/5 | 0 | 501 | 7 (website only) |

Artifacts kept in `out/`: `smoke-companies.jsonl` (input),
`smoke2-report.json` (refresh-diff evidence), `smoke3-*` (final-tree
envelopes/profiles/contract/report), `prototype.html` (built from run 3).

## Contract envelopes (run 3)

- 100/100 envelopes, 0 validation problems.
- Claims: 159–174 per company (avg ~165); evidence: 10 records per
  company; `third_party_cost_usd: 0` everywhere.
- Claim availability: 9,813 `available`, 6,647 `not_available`
  (checked-but-absent, mostly empty bulk columns), 100
  `not_applicable` (YouTube, no verified handle), 7 `blocked`
  (website, sandbox DNS — see below), 0 `failed`.
- `profile_synthesis` claim present in 100/100 envelopes.
- Module states (run 3): registry 100 available, registry_live 100,
  financials 100, roles 100, locations 100, accounting_obligation 100,
  group 9 available / 91 not_found / 0 errors, website 93 not_found /
  7 blocked, wikidata 100 not_found (1 WDQS query, 200 OK, no P2333
  matches in this random small-company sample), youtube_activity 100
  not_applicable (gate precondition unmet — 0 requests, by design).

## Refresh / idempotency (the strongest evidence)

- **Run 2 vs run 1:** exactly **6 changed companies**, all
  `group.relationships`, all six being the companies whose group
  trees the guard fix recovered. Zero changes anywhere else across
  two independent live runs.
- **Run 3 vs run 2:** exactly **0 changes** across all 100 companies
  and all tracked fields. Refresh is idempotent; no timestamp churn,
  no phantom diffs.
- The kit's saved-snapshot replay (`run_refresh_replay.py`) finds the
  expected changes with no false changes (kit baseline behavior,
  preserved).

## The 7 website `blocked` records — sandbox artifact, proven

All 7 companies with a registry-listed website were `blocked` with
"Private, loopback, link-local, multicast, and reserved addresses are
blocked". Cause: this build VM's DNS resolver returns CGNAT addresses
(`198.18.x.x`, `is_global=False`) for every hostname — verified with
`socket.gethostbyname` for equinor.com, google.com and data.brreg.no.
The kit's SSRF guard correctly rejects non-global IPs. Plain HTTP
egress works (equinor.com fetched 200, 385 KB via `requests`), so this
is a sandbox DNS artifact, not a guard bug. The guard was NOT loosened.

Website extraction itself was verified against real captured bytes:
the Equinor homepage HTML (fetched via `requests`, bypassing the guard
for parsing only) parsed to 1 Organization, honest-empty contacts,
0 feeds, 0 JobPostings, null dates (not guessed); robots.txt sitemap
directives parsed; the sitemap index yielded 2 nested sitemaps at the
200-URL cap with correct priority ranking. On a machine with normal
DNS, the fetcher behaves as the kit intended.

## Group-guard fix (found by run 1, verified live)

Run 1 showed 6 `group` source_errors: "Organisation-number mismatch".
Live inspection of `konsernstruktur/930132764` showed the response is
rooted at the PARENT (919371943) with the queried company as a child —
a legitimate tree the root-only echo-check falsely rejected. Fixed to
a whole-tree presence check; run 2 recovered all 6 as `available` with
verified relationships. 3 regression tests in
`tests/test_group_guard.py`.

## Clean-machine install test

Fresh copy of the tree (no `.venv`, no caches) in `/tmp`: `uv sync`
succeeded; a 1-company run emitted 1 terminal envelope (165 claims,
10 evidence records, 0 errors, $0) in 1m37s — dominated by the bulk
CSV scan (~60 s fixed cost). `uv run --with pytest python -m pytest
tests -q`: **249 passed, 18 subtests passed** (all offline).

## Prototype UI verification

`build_prototype.py` from run-3 outputs: 100 profiles, single 2.0 MB
HTML file, JS syntax valid (`node --check`), compare table, change
timeline, normalized group relationships, contract-envelope summary,
research-agent panel and mobile scroll tables all present.

## Environment notes

- Python 3.12, `uv` 0.13.7, pinned `uv.lock` (111 packages).
- BRREG live latency: p50 ~2.7–3.0 s/request, p95 ~3.1–4.0 s/request.
- Extrapolation: ~2.1 s/company throttle floor → ~35–40 min/1,000
  companies at 8 workers.
