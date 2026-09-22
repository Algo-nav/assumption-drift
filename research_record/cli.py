"""The `rr` command line.

    rr review <csv>

Only `review` exists so far. `rr validate` and `rr stats` (SCOPE.md, Phase 3) are not built yet.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from research_record import reviewer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rr", description="research-record command line.")
    sub = parser.add_subparsers(dest="command", required=True)

    review_parser = sub.add_parser("review", help="review a review-queue CSV one row at a time")
    review_parser.add_argument("csv", type=Path)

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

    return 1  # unreachable: argparse rejects any command not registered above


if __name__ == "__main__":
    raise SystemExit(main())
