#!/usr/bin/env bash
# The weekly refresh, on the real checkout. Publishes nothing (no 06_publish).
#
#   scripts/weekly_refresh.sh [--max-usd USD] [--back-days N]
#
# Order: 01 --since-manifest (stop with exit 3 on an SEC block); 02 and 03 --accessions --sync; 05 append; 03b --only (the
# new rows, flags left out) --sync; 04 --from-review --accessions --sync; 05 --attach-outcomes; the manifest advances only if
# every stage exited 0; then the review dumps and data/refresh/<date>/summary.json.
#
# MAX_USD (default 1.00) caps the model spend of the whole run: each stage gets what is left of it after the stages before.
# The stop on the first failing stage is exit 1 (data/_work/failure.txt names it); an SEC block is exit 3 (data/_work/blocked.txt
# holds the HTTP code). Everything printed is also appended to data/_work/refresh.log.
#
# Idempotent: once the manifest has advanced, a second run the same day finds no new filings, sends nothing, and leaves the
# day's summary.json and dumps as they are.
#
# --back-days N moves the manifest back N days first (a manual test in a week with nothing filed). It is only for the workflow's
# manual dispatch; the moved-back manifest is what gets committed, so use it on a throwaway run.
# ANTHROPIC_API_KEY must be in the environment; without it the job refuses to start. DRY_RUN=1 runs the model stages as dry runs,
# does not advance the manifest, and writes the summary and dumps under data/_work/dry-run/<date>/ instead of data/refresh/.
set -uo pipefail

MAX_USD="${MAX_USD:-1.00}"
BACK_DAYS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --max-usd) MAX_USD="$2"; shift 2 ;;
    --back-days) BACK_DAYS="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
if [ -n "${PYTHON:-}" ]; then PY="$PYTHON"; elif [ -x "$REPO/.venv/bin/python" ]; then PY="$REPO/.venv/bin/python"; else PY=python; fi
DATE="${REFRESH_DATE:-$(date -u +%F)}"
WORK="data/_work"
OUT="data/refresh/$DATE"
[ "${DRY_RUN:-0}" = 1 ] && OUT="$WORK/dry-run/$DATE"  # a dry run writes no tracked file: scratch output, manifest not advanced
mkdir -p "$WORK" "$OUT"
rm -f "$WORK/failure.txt" "$WORK/blocked.txt"
exec > >(tee -a "$WORK/refresh.log") 2>&1

SUBMIT="--submit"
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  if [ "${DRY_RUN:-0}" = 1 ]; then SUBMIT=""; else echo "ANTHROPIC_API_KEY is not set (DRY_RUN=1 to run the model stages as dry runs)" >&2; echo "preflight" > "$WORK/failure.txt"; exit 1; fi
fi

step() { echo; echo "== $*"; }
fail() {  # fail STAGE EXIT: record it, write the summary for the record, stop
  echo "$1 exit $2" > "$WORK/failure.txt"
  echo "FAILED: $1 (exit $2); the manifest is not advanced" >&2
  write_summary "$1:$2"
  exit 1
}
write_summary() {
  [ -f "$WORK/review_before.json" ] || return 0
  "$PY" -m pipeline.refresh summary --date "$DATE" --max-usd "$MAX_USD" --since-line "$LEDGER_LINES" --out "$OUT/summary.json" ${1:+--failure "$1"}
}
left() { "$PY" -m pipeline.refresh remaining --max-usd "$MAX_USD" --since-line "$LEDGER_LINES"; }  # what is left of MAX_USD
run() {  # run STAGE cmd...
  local stage="$1"; shift
  step "$stage"
  "$@"; local rc=$?
  echo "exit $rc"
  [ $rc -eq 0 ] || fail "$stage" $rc
}

LEDGER_LINES=0; [ -f data/spend_ledger.jsonl ] && LEDGER_LINES="$(wc -l < data/spend_ledger.jsonl | tr -d ' ')"

if [ "$BACK_DAYS" != "0" ]; then
  "$PY" -I - "$BACK_DAYS" <<'EOF'
import json, sys
from datetime import date, timedelta
n = int(sys.argv[1])
m = json.load(open("data/manifest.json"))
back = lambda s: (date.fromisoformat(s) - timedelta(days=n)).isoformat()
m["processed_through"] = back(m["processed_through"])
for entry in m["companies"].values():
    entry["last_filing_date"] = back(entry["last_filing_date"])
    entry["accessions_on_last_date"] = []
json.dump(m, open("data/manifest.json", "w"), indent=2)
print(f"manifest set back {n} days")
EOF
fi

"$PY" -m pipeline.refresh snapshot || fail snapshot $?

step "01_fetch --since-manifest"
"$PY" -m pipeline.01_fetch --since-manifest 2>&1 | tee "$WORK/fetch.out"; rc=${PIPESTATUS[0]}
echo "exit $rc"
if [ $rc -eq 3 ]; then
  grep -o 'HTTP [0-9][0-9][0-9]' "$WORK/fetch.out" | tail -1 | cut -d' ' -f2 > "$WORK/blocked.txt"
  echo "EDGAR blocked the runner (HTTP $(cat "$WORK/blocked.txt")); stopping, nothing advanced" >&2
  exit 3
fi
[ $rc -eq 0 ] || fail 01_fetch $rc

NEW_FILINGS="$WORK/new_filings.json"
NFILINGS="$("$PY" -I -c 'import json,sys; print(sum(1 for f in json.load(open(sys.argv[1]))["filings"] if f.get("path")))' "$NEW_FILINGS")"
echo "new filings with a document: $NFILINGS"

if [ "$NFILINGS" != "0" ]; then
  run 02_candidates "$PY" -m pipeline.02_candidates --accessions "$NEW_FILINGS"
  run 03_structure "$PY" -m pipeline.03_structure --accessions "$NEW_FILINGS" --sync --max-usd "$(left)" $SUBMIT
fi

run 05_review "$PY" -m pipeline.05_review
"$PY" -m pipeline.refresh new-ids || fail new-ids $?

if [ -s "$WORK/new_verify_ids.txt" ]; then
  run 03b_verify "$PY" -m pipeline.03b_verify --only "$WORK/new_verify_ids.txt" --sync --max-usd "$(left)" $SUBMIT
else
  echo "no new draft rows: nothing to verify"
fi

if [ "$NFILINGS" != "0" ]; then
  run 04_outcomes "$PY" -m pipeline.04_outcomes --from-review --accessions "$NEW_FILINGS" --sync --max-usd "$(left)" $SUBMIT
fi
run 05_review_attach "$PY" -m pipeline.05_review --attach-outcomes

if [ "${DRY_RUN:-0}" = 1 ]; then echo "DRY_RUN: manifest not advanced"; else run manifest_advance "$PY" -m pipeline.manifest advance; fi

# Dumps: for each company with new rows or new proposals. The _new file is the new rows (an id filter on top of no-note, so the
# backlog is not dumped again); the _pending file is the change proposals, then the acknowledgement proposals.
if [ "$NFILINGS" != "0" ] || [ ! -e "$OUT/summary.json" ]; then
  "$PY" -m pipeline.refresh dump-targets | while read -r TICKER CIK IDS NPROP; do
    CSV="data/review/$CIK.csv"
    if [ "$IDS" != "-" ]; then
      "$PY" -m research_record.cli review "$CSV" --filter no-note --filter "ids=$IDS" --dump "$OUT/${TICKER}_new.txt"
    fi
    if [ "$NPROP" != "0" ]; then
      "$PY" -m research_record.cli review "$CSV" --filter change-pending --dump "$OUT/${TICKER}_pending.txt"
      "$PY" -m research_record.cli review "$CSV" --filter ack-pending --dump "$WORK/${TICKER}_ack.txt"
      [ -s "$WORK/${TICKER}_ack.txt" ] && { echo >> "$OUT/${TICKER}_pending.txt"; cat "$WORK/${TICKER}_ack.txt" >> "$OUT/${TICKER}_pending.txt"; }
      rm -f "$WORK/${TICKER}_ack.txt"
    fi
  done
  find "$OUT" -name '*.txt' -empty -delete
  write_summary ""
else
  echo "no new filings and today's summary exists: leaving it"
fi
echo "done: $OUT"
