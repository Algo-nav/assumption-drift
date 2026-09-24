"""The `rr` command line.

    rr review <csv>
    rr stats <jsonl>

`rr validate` (SCOPE.md, Phase 3) is not built yet.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from research_record import reviewer, stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rr", description="research-record command line.")
    sub = parser.add_subparsers(dest="command", required=True)

    review_parser = sub.add_parser("review", help="review a review-queue CSV one row at a time")
    review_parser.add_argument("csv", type=Path)

    stats_parser = sub.add_parser("stats", help="print summary stats for a JSONL file of ResearchRecord rows")
    stats_parser.add_argument("path", type=Path)

    args = parser.parse_args(argv)

    if args.command == "review":
        try:
            reviewer.run(args.csv)
        except (FileNotFoundError, ValueError) as exc:
            print(f"rr review: {exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print()
            return 130
        return 0

    if args.command == "stats":
        try:
            print(stats.run(args.path))
        except FileNotFoundError as exc:
            print(f"rr stats: {exc}", file=sys.stderr)
            return 1
        return 0

    return 1  # unreachable: argparse rejects any command not registered above


if __name__ == "__main__":
    raise SystemExit(main())
