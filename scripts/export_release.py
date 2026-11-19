"""Write the release from Postgres and prove it is the published one.

    python scripts/export_release.py [--out DIR] [--against PATH] [--csv-dir DIR]

Reads the review queues of the published companies from the database (whatever STORE says, this script reads Postgres;
DATABASE_URL and STORE_SCHEMA as in research_record/store.py) and builds the release with pipeline/06_publish.py's own
functions: `build_records` (approved rows only, status from the rubric) and `write_release`. Nothing here formats a row.

The jsonl it writes (default data/_work/pg/release/assumption_drift.jsonl) is then compared, byte for byte, with the
published file (default data/release/assumption_drift.jsonl, the file the site serves as data/release.jsonl). If they
differ the run fails (exit 1) and prints the first ten differences: the line, the record_id and the fields that differ.

Two more checks, both of which also fail the run:
  * `statements.published_status`, the status the database stores for an approved row, must equal the status of the
    record built from it;
  * with --csv-dir DIR, every company's review queue is written to DIR/{cik}.csv from the database and must be
    byte-identical to data/review/{cik}.csv. The CSV stays the export and review format; this is that export.
"""

from __future__ import annotations

import argparse
import csv
import importlib
import io
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import common  # noqa: E402
from research_record import store  # noqa: E402

DEFAULT_OUT = common.WORK_DIR / "pg" / "release"


def published_companies(db: store.PostgresBackend) -> list[common.Company]:
    """The companies the database marks `publish`, as the stages' own Company."""
    return [
        common.Company(name=c["name"], ticker=c["ticker"], cik=c["cik"], fiscal_year_end_month=c["fiscal_year_end_month"],
                       fiscal_year_named_for=c["fiscal_year_named_for"], tax_rate_parens_negative=c["tax_rate_parens_negative"],
                       publish=c["publish"], unpublished_reason=c["unpublished_reason"], sector=c["sector"])
        for c in db.companies() if c["publish"]
    ]


def _flat(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, inner in value.items():
            out.update(_flat(inner, f"{prefix}{key}."))
        return out
    return {prefix[:-1]: value}


def differences(ours: str, theirs: str, limit: int = 10) -> list[str]:
    """The first `limit` lines that differ between two release files, each as one readable line."""
    a, b = ours.splitlines(), theirs.splitlines()
    found: list[str] = []
    for n in range(max(len(a), len(b))):
        if len(found) == limit:
            break
        mine, published = (a[n] if n < len(a) else None), (b[n] if n < len(b) else None)
        if mine == published:
            continue
        if mine is None or published is None:
            only = "the published file" if mine is None else "the database export"
            rid = json.loads(published or mine).get("record_id", "?")
            found.append(f"line {n + 1}: only in {only}: {rid}")
            continue
        try:
            x, y = _flat(json.loads(mine)), _flat(json.loads(published))
        except json.JSONDecodeError:
            found.append(f"line {n + 1}: not JSON on one side")
            continue
        if x.get("record_id") != y.get("record_id"):
            found.append(f"line {n + 1}: database has {x.get('record_id')}, published has {y.get('record_id')}")
            continue
        fields = [f"{k}: database {x.get(k)!r}, published {y.get(k)!r}" for k in sorted(set(x) | set(y)) if x.get(k) != y.get(k)]
        found.append(f"line {n + 1}: {x.get('record_id')}: " + ("; ".join(fields) if fields else "same values, different bytes"))
    return found


def export_csvs(db: store.PostgresBackend, targets: list[common.Company], out_dir: Path) -> list[str]:
    """Each company's review queue as DIR/{cik}.csv, from the database. The ones that are not byte-identical to data/review/."""
    review = importlib.import_module("pipeline.05_review")
    out_dir.mkdir(parents=True, exist_ok=True)
    differ: list[str] = []
    for c in targets:
        source = common.REVIEW_DIR / f"{c.cik}.csv"
        rows = db.read_csv(source) if db.exists(source) else []
        text = io.StringIO(newline="")
        writer = csv.DictWriter(text, fieldnames=review.COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        (out_dir / f"{c.cik}.csv").write_text(text.getvalue(), encoding="utf-8", newline="")
        if not source.exists() or source.read_text(encoding="utf-8") != text.getvalue():
            differ.append(c.ticker)
    return differ


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write release.jsonl from Postgres and diff it against the published file.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="directory for the exported release (default data/_work/pg/release)")
    parser.add_argument("--against", type=Path, default=common.RELEASE_DIR / "assumption_drift.jsonl", help="the published release.jsonl")
    parser.add_argument("--csv-dir", type=Path, metavar="DIR", help="also export every review queue as CSV here and compare with data/review/")
    args = parser.parse_args(argv)

    publish = importlib.import_module("pipeline.06_publish")
    db = store.PostgresBackend()
    failed = False
    with store.use(db):
        targets = published_companies(db)
        records, invalid = publish.build_records(targets, common.REVIEW_DIR)
        _, jsonl_path = publish.write_release(records, args.out)
    print(f"export_release: {len(records):,} approved row(s) across {len(targets)} company(ies), {invalid:,} left out as invalid")
    print(f"  wrote {jsonl_path}")

    stored = dict(db._all("SELECT record_id, published_status FROM statements WHERE published_status IS NOT NULL"))
    built = {r.record_id: r.status for r in records}
    if stored != built:
        failed = True
        wrong = sorted(k for k in set(stored) | set(built) if stored.get(k) != built.get(k))
        print(f"FAILED: published_status in the database disagrees with the built record on {len(wrong)} row(s): {', '.join(wrong[:10])}", file=sys.stderr)

    ours, theirs = jsonl_path.read_bytes(), args.against.read_bytes()
    if ours == theirs:
        print(f"  byte-identical to {args.against} ({len(ours):,} bytes, {len(records):,} rows)")
    else:
        failed = True
        print(f"FAILED: {jsonl_path} is not byte-identical to {args.against} "
              f"({len(ours):,} bytes and {ours.count(b'\n'):,} lines against {len(theirs):,} bytes and {theirs.count(b'\n'):,} lines). "
              "First differences:", file=sys.stderr)
        for line in differences(ours.decode("utf-8"), theirs.decode("utf-8")):
            print(f"  {line}", file=sys.stderr)

    if args.csv_dir:
        differ = export_csvs(db, targets, args.csv_dir)
        if differ:
            failed = True
            print(f"FAILED: review CSV exported from the database differs from data/review/ for: {', '.join(differ)}", file=sys.stderr)
        else:
            print(f"  {len(targets)} review CSVs written to {args.csv_dir}, each byte-identical to data/review/")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
