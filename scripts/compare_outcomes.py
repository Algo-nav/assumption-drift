"""Compare outcomes before and after a pipeline run. Read-only: writes nothing.

    PYTHONPATH=. .venv/bin/python scripts/compare_outcomes.py [--head data/_head] [--review data/review] [--outcomes data/outcomes]

Reads   <head>/<cik>.csv      the committed review file (before)
        <review>/<cik>.csv    the review file after the run
        <outcomes>/<cik>.jsonl  the pipeline's own outcome per draft, for the change-pending count

Per company and in total: rows with an outcome before and after; gained (none before, one now); lost (one before,
none now); changed (both, with a different reported value or outcome accession); and change-pending now, which is the
approved rows whose pipeline outcome (data/outcomes) differs from the outcome in the head file. An approved row's own
outcome columns are never overwritten by 05_review, so for those rows the review file alone cannot show the difference.
Then the first 20 lost and first 20 changed rows, and how many lost rows had a head excerpt that read like guidance.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

csv.field_size_limit(sys.maxsize)

VALUE, ACC, EXCERPT = "outcome.reported_value", "outcome.evidence.accession_number", "outcome.evidence.excerpt"
GUIDANCE = re.compile(r"expect|guidance|outlook|approximately", re.IGNORECASE)
SHOW = 20


def read_rows(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as fh:
        return {r["record_id"]: r for r in csv.DictReader(fh)}


def has_outcome(row: dict[str, str] | None) -> bool:
    return bool(row and row.get(VALUE, "").strip())


def same_outcome(a: dict[str, str], b: dict[str, str]) -> bool:
    return a.get(VALUE) == b.get(VALUE) and a.get(ACC) == b.get(ACC)


def pipeline_outcomes(path: Path) -> dict[str, dict | None]:
    if not path.exists():
        return {}
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {r["draft_id"]: r.get("outcome") for r in rows}


def differs_from_head(head: dict[str, str], outcome: dict | None) -> bool:
    if not has_outcome(head):
        return outcome is not None
    if outcome is None:
        return True
    return (float(head[VALUE]) != float(outcome["reported_value"])
            or head.get(ACC) != outcome["evidence"]["accession_number"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--head", type=Path, default=Path("data/_head"))
    parser.add_argument("--review", type=Path, default=Path("data/review"))
    parser.add_argument("--outcomes", type=Path, default=Path("data/outcomes"))
    args = parser.parse_args(argv)

    header = f"{'ticker':7}{'before':>8}{'after':>8}{'gained':>8}{'lost':>7}{'changed':>9}{'pending':>9}"
    print(header)
    totals = [0] * 6
    lost_rows: list[tuple[dict[str, str], dict[str, str]]] = []
    changed_rows: list[tuple[dict[str, str], dict[str, str]]] = []
    for head_path in sorted(args.head.glob("*.csv")):
        cik = head_path.stem
        head, after = read_rows(head_path), read_rows(args.review / f"{cik}.csv")
        pipeline = pipeline_outcomes(args.outcomes / f"{cik}.jsonl")
        before_n = sum(has_outcome(r) for r in head.values())
        after_n = sum(has_outcome(r) for r in after.values())
        gained = lost = changed = pending = 0
        for rid, new in after.items():
            old = head.get(rid)
            if old is None:
                continue
            if has_outcome(new) and not has_outcome(old):
                gained += 1
            elif has_outcome(old) and not has_outcome(new):
                lost += 1
                lost_rows.append((old, new))
            elif has_outcome(old) and has_outcome(new) and not same_outcome(old, new):
                changed += 1
                changed_rows.append((old, new))
            if old.get("approved", "").lower() == "true" and rid in pipeline and differs_from_head(old, pipeline[rid]):
                pending += 1
        ticker = next(iter(after.values()), next(iter(head.values()), {})).get("ticker") or cik
        row = [before_n, after_n, gained, lost, changed, pending]
        print(f"{ticker:7}" + "".join(f"{v:>{w}}" for v, w in zip(row, (8, 8, 8, 7, 9, 9))))
        totals = [a + b for a, b in zip(totals, row)]
    print(f"{'TOTAL':7}" + "".join(f"{v:>{w}}" for v, w in zip(totals, (8, 8, 8, 7, 9, 9))))

    def show(title: str, rows: list[tuple[dict[str, str], dict[str, str]]]) -> None:
        print(f"\n{title}: first {min(SHOW, len(rows))} of {len(rows)}")
        for old, new in rows[:SHOW]:
            new_value = new.get(VALUE) or "-"
            print(f"  {new.get('ticker') or old.get('ticker')} | {old['assumption.metric']} | {old['assumption.target_period']} | "
                  f"old {old.get(VALUE) or '-'} | new {new_value} | {(new.get(EXCERPT) or '')[:80]!r}")

    show("LOST", lost_rows)
    show("CHANGED", changed_rows)
    guided = sum(bool(GUIDANCE.search(old.get(EXCERPT, ""))) for old, _ in lost_rows)
    print(f"\nlost rows whose head excerpt contains expect, guidance, outlook or approximately: {guided} of {len(lost_rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
