# Strategy — Signalpost entry

How this entry is built, what score it targets, and where it is honestly
weak. Written 2026-11-12, after three measured 100-company smoke runs.

## Rubric targeted (scoring version 2, effective 26 August 2026)

50 recall & coverage · 30 precision / exact identity / evidence ·
12 synthesis · 8 UX. Qualification ≥ 65/100. A material wrong-company
match blocks qualification regardless of score. Tiebreaks: fewer
wrong-company facts → higher coverage → lower third-party API cost.

The starter kit's own scorer uses an older 55/15/10/12/8 rubric; this
entry follows the v2 evaluation contract as authoritative.

## Architecture

```
organisations.jsonl ──► profiles_from_bulk (frozen BRREG snapshot)
                              │
                    ┌─────────▼──────────┐
                    │  evidence modules  │  (one record per module per company;
                    │  registry, live    │   terminal states only, hashes,
                    │  BRREG, website,   │   retrieved_at + effective_at)
                    │  wikidata, youtube │
                    └─────────┬──────────┘
                              │
              identity gate (website, threshold 0.9)
                              │
                    ┌─────────▼──────────┐
                    │  contract adapter  │  generic: every evidence value
                    │  (contract.py)     │  field becomes a claim; new modules
                    │                    │  map automatically
                    └─────────┬──────────┘
                              │
              synthesis (rule-based, evidence-linked)
                              │
              refresh diff vs previous profiles
                              │
              contract envelope + report + prototype UI
```

Design principles:

1. **The organisation number is the only join key.** Every live response
   is echo-checked against the requested number. A mismatch is a
   `source_error`, never a claim.
2. **Fail closed, abstain loudly.** Unknown = `not_available` /
   `ambiguous` / `blocked` with a reason. Missing values are never zero;
   unknown dates are null, never guessed.
3. **Evidence before synthesis.** The synthesis layer only re-states
   evidence values with `evidence_ids`; it invents nothing and uses no
   model.
4. **Generic mapping.** The contract adapter walks evidence values, so
   a new module's fields become claims without adapter changes. This is
   what let the website-deepening and Wikidata/YouTube work land without
   touching the adapter.
5. **Idempotent refresh.** The diff compares tracked field values, not
   timestamps, so re-runs of unchanged data produce zero changes.

## What each rubric part gets

### Recall & coverage (50)

The largest cheap win was already in hand: the BRREG bulk snapshot has
~90 columns, but the kit's normalizer used ~11. This entry maps all of
them (contact details, activity description, statutory purpose,
founding date, capital data, NACE codes, VAT/bankruptcy/liquidation
status, addresses, employees). Live modules add current entity data,
annual accounts, roles, group structure (normalized to parent/subsidiary
relationships with valid org numbers on both sides), and paged
subunits/locations.

Website deepening adds, per fetched company site: sitemap/feed
discovery, merged contact details with a site-vs-registry comparison,
JSON-LD job postings (verified against hiring-org identity),
dated news items (page + feed, archive vs article distinguished),
and broad structured data (`@graph`, `LocalBusiness` subtypes,
`PostalAddress`, `ContactPoint`, founding date, headcount,
site-declared ratings labelled as such).

Wikidata (exact P2333) adds inception, headquarters, industry,
official-website candidates and sitelinks for companies that have an
item — rare in a random sample (0/100 in the smoke batch), common for
large companies. YouTube RSS adds dated video activity for
gate-verified channels.

**Honest recall gaps:** jobs exist only when the company site publishes
JSON-LD or a career page; there is no job-board, news-aggregator, or
review coverage; `financial_history` (PDF copies) is opt-in only; most
small companies have no website in the registry at all (93/100 in the
smoke batch), which caps the website-dependent families for the long
tail. The entry maximizes what the authoritative registry + company-owned
sources can support, and abstains elsewhere rather than guessing.

### Precision / exact identity / evidence (30)

This is the entry's strongest part, by design:

- Echo-checks on every live BRREG response; the konsernstruktur guard
  was fixed during integration (trees are parent-rooted — the queried
  company may appear only as a child; verified live, 6/100 companies
  recovered, with regression tests).
- Website identity gate at 0.9, unchanged from the kit, extended with
  footer-excerpt, VAT-ID and sameAs evidence. Below-threshold claims
  stay `ambiguous`; social links stay quarantined.
- Wikidata: exact P2333 + distinctive-token label check; multiple QIDs
  reject. YouTube: gate-verified handle + channel-name subset check.
- Every claim carries `evidence_ids` → evidence records with source URL,
  source type, `retrieved_at`, `effective_at` (from response headers
  where available), and SHA-256 of the raw bytes.
- Claim completeness is deliberate: checked-but-absent fields are
  emitted as `not_available` claims (value null), so "checked, absent"
  is distinguishable from "never checked". ~165 claims/company, ~40%
  `not_available`, ~3.8 MB per 100-company contract file. Filtering
  empties was considered and rejected: it would weaken the
  checked-vs-unchecked distinction for no scoring benefit.

Known precision limitation: Wikidata ambiguous/name-rejected matches
are recorded as `not_found` (the internal evidence vocabulary has no
`ambiguous` status), so the adapter emits `not_available` rather than
`ambiguous` for that family. No QID-bearing claim leaks; the trade is
a slightly lossy status, not a wrong fact.

### Synthesis (12)

Rule-based, template + rules, no LLM (`synthesis.py`). Each profile
gets one `profile_synthesis` claim: what the company does (legal form,
activity, industry, purpose), scale signals (employees, latest accounts
figures), people/location/group summary, and explicit abstentions for
families with no evidence (jobs, activity, sentiment). Every sentence
carries `evidence_ids`. No model means no hallucination risk and $0
cost; the trade is prose variety — the synthesis is accurate and
complete, not eloquent.

### UX (8)

`scripts/build_prototype.py` produces one static HTML file from any
profiles + contract output: search, structured filters (form, status,
accounts, slice, jobs, group, external signals), 8 sort orders,
2–4 company side-by-side comparison per field family, a change
timeline from `changes[]`, per-fact source links with hashes, a
server-side research-agent panel (answers precomputed by `research.py`,
explicit abstentions for hiring/activity/sentiment), and mobile
scroll-x tables. No server, no build step, no external asset.
Verified: builds from real smoke output, valid JS, all markers present.

## Sources, licences, rights assumptions

| Source | Licence / basis | Cost | Key |
| --- | --- | --- | --- |
| BRREG Enhetsregisteret bulk + live APIs, Regnskapsregisteret | NLOD 2.0 (Norwegian Licence for Open Government Data) | $0 | None |
| Wikidata Query Service | CC0 data; polite identifying UA, ≤200 orgs/query, retry honours Retry-After | $0 | None |
| Company websites (HTML, sitemap, RSS/Atom, JSON-LD) | Public pages; robots.txt respected, bounded crawl (homepage + ≤8 secondary pages, byte caps), SSRF guard | $0 | None |
| YouTube public channel RSS | Public feeds; no watch-page scraping, no metrics invented | $0 | None |

Rights assumptions: registry data is open government data; Wikidata is
CC0; company-site facts are reported with source links and retrieval
dates, never republished wholesale; YouTube titles/dates come from the
public feed. No content is stored beyond short evidence excerpts and
hashes.

## Rejected alternatives (evaluated, deliberately not used)

- **Sentiment model** (kit's NOSIBLE/torch path): heavy, `trust_remote_code`,
  no Norwegian labelled corpus, kit's own qualification gate (≥300
  labelled items, F1 ≥ 0.80) unmet. Sentiment = explicit abstain.
- **NAV jobs API**: requires a JWT credential → personal credential,
  forbidden. No job-board coverage as a consequence.
- **Brave Search**: needs a paid API key. Rejected.
- **LinkedIn / Google Maps / yt-dlp / Fagfolkguiden**: key, login, or
  terms-sensitive scraping. Rejected; the kit marks them experimental.
- **GDELT news**: 429 from the build network, plus wrong-company risk
  from name-only matching. Skipped.
- **Google News as evidence**: rights status unverified; discovery-only
  in the kit, never published. Not used as evidence.
- **OpenStreetMap/Nominatim**: low priority vs the registry's own
  authoritative addresses; skipped.
- **LLM synthesis**: would add cost, latency and hallucination risk for
  the 12 synthesis points; rule-based synthesis covers the scored
  properties (accurate, evidence-linked, abstains honestly).

## Integration fixes (found by measurement, not by plan)

1. **Konsernstruktur guard** (see above): root-only echo-check falsely
   rejected subsidiaries. Fixed to a whole-tree presence check;
   6/100 smoke companies recovered; 3 regression tests.
2. **Wikidata chunking**: the single GET query URL would exceed header
   limits past ~400 orgs. Chunked at 200 orgs/query, sequential, with
   per-chunk provenance; 2 regression tests. Small batches still cost
   exactly 1 request.
3. **Website identity context**: the batch runner did not pass
   `legal_name` / org number / registry contacts to the website
   fetcher, leaving job verification and contact comparison
   quarantined. Wired through (backward-compatible).
4. **Prototype group summary**: now reads the normalized
   `relationships[]` shape instead of treating the group body as raw.

## Limitations (honest)

1. **Website coverage is registry-bound.** Only companies with a
   registry-listed website (or a Wikidata candidate that passes the
   gate) get website families. In the smoke batch: 7/100.
2. **No browser fallback.** JavaScript-heavy sites yield static HTML
   only. The kit's Scrapy/Playwright path exists but is not on the
   scored batch path; sitemap discovery is not implemented there.
3. **BRREG throttle floor.** Live endpoints throttle to ~2.1 s/company
   with the kit's politeness settings: ~4–5 min/100 companies,
   ~35–40 min/1,000 (measured extrapolation). `financial_history`
   would multiply this and stays opt-in.
4. **Sandbox DNS artifact.** In networks whose resolver returns
   non-global (CGNAT) addresses, the SSRF guard blocks all website
   fetches. This is the guard working as designed; website extraction
   was verified separately against real captured site bytes (Equinor
   homepage + sitemaps). On normal DNS the fetcher behaves as the kit
   intended. See `SMOKE_TEST.md` for the proof.
5. **Single-company runs pay the bulk-scan cost.** The 155 MB bulk CSV
   (~845 MB uncompressed, 1.17M rows) is scanned per run (~60 s);
   amortized over a batch, dominant for a 1-company run.
6. **No review/rating coverage.** No lawful keyless review source was
   found; site-declared `aggregateRating` is reported labelled as
   site-declared only.
7. **YouTube depends on the gate.** Without a fetched, gate-passing
   website, the module is `not_applicable` (0 requests). It activates
   only where the company site itself links a channel.

## Expected score (self-assessment, unverified)

No official score exists; the union-based recall denominator is only
known after the official run. The entry should be strong on precision
and evidence (the 30), solid on registry/financial/people/location
recall, partial on jobs/activity (company-site dependent), with
honest synthesis and a complete UX layer. The qualification bar
(≥65) depends on how much of the verified union is registry-derivable
vs external-only; the entry deliberately trades speculative external
recall for zero wrong-company risk, since a material wrong-company
match blocks qualification outright.
