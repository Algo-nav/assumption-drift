#!/usr/bin/env bash
# Simulate the weekly refresh on a fresh checkout, in a scratch directory under data/_work/ (gitignored). Nothing tracked in the working tree is written.
#
#   scripts/simulate_fresh_checkout.sh [--back-days N] [--max-usd USD]
#
# The checkout is `git archive HEAD` (tracked files only: no data/raw, data/candidates, data/drafts, data/batches,
# data/_work, no .env) plus data/manifest.json. While the pipeline changes are not committed yet, the files
# `git ls-files -m -o --exclude-standard` reports (modified, and new but not ignored) are laid over it too, which
# is the checkout the branch will be once they are. Git is only read.
#
# --back-days N sets every company's last_filing_date and the manifest's processed_through back by N days, for a week
# in which nothing was filed. ANTHROPIC_API_KEY must be in the environment for the model stages (this script never
# reads .env); without it those stages are dry runs (the projection is estimated offline, nothing is sent).
set -uo pipefail

BACK_DAYS=0
MAX_USD=0.50
while [ $# -gt 0 ]; do
  case "$1" in
    --back-days) BACK_DAYS="$2"; shift 2 ;;
    --max-usd) MAX_USD="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-$REPO/.venv/bin/python}"
mkdir -p "$REPO/data/_work"
SIM="${SIM_DIR:-$(mktemp -d "$REPO/data/_work/weekly-sim.XXXXXX")}"  # data/_work/ is gitignored; scratch lives there
mkdir -p "$REPO/data/_work" "$SIM"

cd "$REPO"
git archive HEAD | tar -x -C "$SIM"
git ls-files -m -o --exclude-standard -z | rsync -a --from0 --files-from=- "$REPO/" "$SIM/"
cd "$SIM"
echo "== fresh checkout in $SIM"
for d in raw candidates drafts batches _work _head; do
  [ -e "data/$d" ] && { echo "UNEXPECTED: data/$d exists in the fresh checkout" >&2; exit 1; }
done
[ -e .env ] && { echo "UNEXPECTED: .env exists in the fresh checkout" >&2; exit 1; }

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
    entry["accessions_on_last_date"] = []  # the accessions on a date that was moved back are not the manifest's any more
json.dump(m, open("data/manifest.json", "w"), indent=2)
print(f"manifest set back {n} days")
EOF
fi
step() { echo; echo "== $*"; }
KEY=no; [ -n "${ANTHROPIC_API_KEY:-}" ] && KEY=yes
SUBMIT=""; [ "$KEY" = yes ] && SUBMIT="--submit"
echo "model key in environment: $KEY (model stages are $([ "$KEY" = yes ] && echo real || echo dry runs))"

step "3. 01_fetch --since-manifest"
"$PY" -m pipeline.01_fetch --since-manifest; echo "exit $?"

step "4a. 02_candidates --accessions"
"$PY" -m pipeline.02_candidates --accessions data/_work/new_filings.json; echo "exit $?"

step "4b. 03_structure --accessions --sync --max-usd $MAX_USD"
"$PY" -m pipeline.03_structure --accessions data/_work/new_filings.json --sync --max-usd "$MAX_USD" $SUBMIT; echo "exit $?"

ids() {  # every record_id in the review queue, one per line, sorted
  "$PY" -I - <<'EOF2'
import csv, glob
csv.field_size_limit(10**9)
for path in sorted(glob.glob("data/review/*.csv")):
    for row in csv.DictReader(open(path, newline="", encoding="utf-8")):
        print(row["record_id"])
EOF2
}
mkdir -p data/_work
ids | sort > data/_work/ids_before.txt

step "6a. 05_review (append the new drafts to the queue)"
"$PY" -m pipeline.05_review; echo "exit $?"
ids | sort | comm -13 data/_work/ids_before.txt - > data/_work/new_row_ids.txt
echo "new rows in the queue: $(wc -l < data/_work/new_row_ids.txt | tr -d ' ')"

step "6b. 03b_verify --only (the new rows) --sync --max-usd $MAX_USD"
if [ -s data/_work/new_row_ids.txt ]; then
  "$PY" -m pipeline.03b_verify --only data/_work/new_row_ids.txt --sync --max-usd "$MAX_USD" $SUBMIT; echo "exit $?"
else
  echo "no new rows: nothing to verify"
fi

step "5. 04_outcomes --from-review --accessions --sync --max-usd $MAX_USD"
"$PY" -m pipeline.04_outcomes --from-review --accessions data/_work/new_filings.json --sync --max-usd "$MAX_USD" $SUBMIT; echo "exit $?"

step "6c. 05_review --attach-outcomes"
"$PY" -m pipeline.05_review --attach-outcomes; echo "exit $?"

step "advance the manifest (in the real job: only after every stage above succeeded; here every fetch was clean, and a dry run advances it only to show the dateline)"
"$PY" -m pipeline.manifest advance; echo "exit $?"

step "7. 06_publish (dry run)"
"$PY" -m pipeline.06_publish; echo "exit $?"
echo
grep -n "Filings processed through" card/README.md space/index.html

echo
echo "checkout kept at $SIM"
