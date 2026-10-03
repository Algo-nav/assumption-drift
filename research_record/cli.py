"""The `rr` command line.

    rr review <csv> [--filter verify-no] [--filter no-note] [--filter fast|slow] [--filter ack-pending] [--filter change-pending] [--filter ids=<comma-separated record_ids>]
    rr validate <path>       path is a release .jsonl, or a review-queue .csv (approved rows only)
    rr stats <jsonl> [--json]

`--filter` may be repeated; specs combine with AND. See `research_record.reviewer` for what each one
matches. See `research_record.validate` for exactly what `rr validate` checks.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from research_record import reviewer, stats, validate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rr", description="research-record command line.")
    sub = parser.add_subparsers(dest="command", required=True)

    review_parser = sub.add_parser("review", help="review a review-queue CSV one row at a time")
    review_parser.add_argument("csv", type=Path)
    review_parser.add_argument(
        "--filter", action="append", default=[], metavar="SPEC",
        help="only rows matching SPEC: verify-no | no-note | fast | slow | ack-pending | change-pending | ids=<comma-separated record_ids>; "
             "repeat to combine with AND",
    )

    validate_parser = sub.add_parser("validate", help="validate a release .jsonl or an approved review-queue .csv")
    validate_parser.add_argument("path", type=Path)

    stats_parser = sub.add_parser("stats", help="print summary stats for a JSONL file of ResearchRecord rows")
    stats_parser.add_argument("path", type=Path)
    stats_parser.add_argument("--json", action="store_true", help="print the stats as JSON instead of plain text")

    args = parser.parse_args(argv)

    if args.command == "review":
        try:
            reviewer.run(args.csv, filters=args.filter or None)
        except (FileNotFoundError, ValueError) as exc:
            print(f"rr review: {exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print()
            return 130
        return 0

    if args.command == "validate":
        try:
            issues, checked = validate.validate_path(args.path)
        except (FileNotFoundError, ValueError) as exc:
            print(f"rr validate: {exc}", file=sys.stderr)
            return 1
        for issue in issues:
            print(issue, file=sys.stderr)
        if issues:
            print(f"rr validate: {len(issues)} issue(s) across {checked} row(s) checked", file=sys.stderr)
            return 1
        print(f"rr validate: {checked} row(s), all OK")
        return 0

    if args.command == "stats":
        try:
            print(stats.run(args.path, as_json=args.json))
        except FileNotFoundError as exc:
            print(f"rr stats: {exc}", file=sys.stderr)
            return 1
        return 0

    return 1  # unreachable: argparse rejects any command not registered above


if __name__ == "__main__":
    raise SystemExit(main())
