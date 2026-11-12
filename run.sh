#!/usr/bin/env bash
# Signalpost entry: single run command.
#
# Usage:
#   ./run.sh <organisations.jsonl> <brreg-enheter.csv> <out-dir> <run-id> [expected-count] [previous-profiles.jsonl]
#
# Example (100-company smoke test):
#   ./run.sh out/smoke-companies.jsonl brreg-enheter.csv out smoke-001 100
#
# Emits, in <out-dir>: envelopes.jsonl, contract.jsonl, profiles.jsonl,
# run-report.json, prototype.html — one terminal envelope per input.
set -euo pipefail

if [ "$#" -lt 4 ]; then
  echo "usage: $0 <organisations.jsonl> <brreg-enheter.csv> <out-dir> <run-id> [expected-count] [previous-profiles.jsonl]" >&2
  exit 2
fi

ORGANISATIONS="$1"
BULK="$2"
OUT_DIR="$3"
RUN_ID="$4"
EXPECTED_COUNT="${5:-100}"
PREVIOUS_PROFILES="${6:-}"

cd "$(dirname "$0")"
mkdir -p "$OUT_DIR"

uv sync

PREVIOUS_ARGS=()
if [ -n "$PREVIOUS_PROFILES" ]; then
  PREVIOUS_ARGS=(--previous-profiles "$PREVIOUS_PROFILES")
fi

uv run python scripts/run_competition_batch.py \
  --organisations "$ORGANISATIONS" \
  --bulk "$BULK" \
  --profiles-output "$OUT_DIR/profiles.jsonl" \
  --output "$OUT_DIR/envelopes.jsonl" \
  --report "$OUT_DIR/run-report.json" \
  --run-id "$RUN_ID" \
  --expected-count "$EXPECTED_COUNT" \
  --contract-output "$OUT_DIR/contract.jsonl" \
  "${PREVIOUS_ARGS[@]}"

uv run python scripts/build_prototype.py \
  --input "$OUT_DIR/profiles.jsonl" \
  --contract-envelopes "$OUT_DIR/contract.jsonl" \
  --run-id "$RUN_ID" \
  --output "$OUT_DIR/prototype.html"

echo "Done: $OUT_DIR/{envelopes,contract,profiles}.jsonl, run-report.json, prototype.html"
