# Submission details — Signalpost

Fill in the repository URL, commit hash and contact at submit time.
Everything else below is final for this entry.

## Repository

- **Repository URL:** https://github.com/kocredaka/builderr-signalpost
- **Exact commit hash:** _(stated in the submission email — see SUBMISSION.md note: hash is fixed at commit time)_
- **Revisions used:** 0 of 4 allowed (5 versions total)

## Run command (single command)

```bash
bash run.sh <organisations.jsonl> <brreg-enheter.csv> <out-dir> <run-id> [expected-count] [previous-profiles.jsonl]
```

Install step (declared, clean machine): `uv sync` (Python 3.12+,
`uv`). Pinned dependencies in `uv.lock`. The run command accepts a
JSONL batch of organisation numbers and emits exactly one terminal
envelope per input, plus a machine-readable run report and a static
prototype UI. See `README.md`.

## Models

**None.** No LLM, no ML model, no embeddings. Synthesis is rule-based
(`src/norway_company_agent/synthesis.py`). Sentiment is explicitly
abstained.

## APIs and data sources

| API / source | Purpose | Licence | Key / credential | Cost |
| --- | --- | --- | --- | --- |
| Brønnøysundregistrene Enhetsregisteret bulk CSV + live APIs (`data.brreg.no`) | Registry details, roles, group, subunits | NLOD 2.0 | None | $0 |
| Brønnøysundregistrene Regnskapsregisteret API | Annual accounts | NLOD 2.0 | None | $0 |
| Wikidata Query Service (`query.wikidata.org/sparql`, P2333) | Cross-check facts, website candidates | CC0 | None (identifying User-Agent) | $0 |
| Company websites (public HTTP) | Contacts, jobs, news, structured data | Public pages; robots.txt respected | None | $0 |
| YouTube public channel RSS feeds | Dated video activity | Public feeds | None | $0 |

No personal credential, cookie, account, or paid API is used anywhere.

## Estimated cost per official run

**$0.00** — for 100 companies and for 1,000 companies alike. There is
no third-party API cost, no model cost, and no paid data source.
`operations.third_party_cost_usd` is `0` in every emitted envelope.
Measured request counts: 501 requests per 100-company run
(5 live BRREG requests/company + 1 WDQS query), ~0.94 MB total.

## Runtime estimate

~4–5 minutes per 100 companies (measured, 3 runs); ~35–40 minutes per
1,000 companies (extrapolated from the measured per-company throttle
floor of ~2.1 s). Single-machine, 8 workers, no GPU.

## Smoke test

100-company smoke test, seed 20260823, three runs: 100/100 terminal
envelopes, 5/5 validation checks, 0 contract-validation problems, $0
cost, idempotent refresh (0 false changes). Full numbers and artifacts
in `SMOKE_TEST.md` and `out/`.

## Contact

stokacc989@gmail.com (same contact as the Trading Round 2 submission)
