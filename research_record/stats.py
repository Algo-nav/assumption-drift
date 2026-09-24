"""`rr stats <path>` (SCOPE.md section 5.2): summary numbers over a file of published records.

Reads a JSONL file, one ResearchRecord per line, and prints the row count, per-status counts, the median
`days_to_falsifiable`, the share of misses never acknowledged, and a per-company breakdown. This output
feeds the LinkedIn posts, so it stays plain and short.

A beat/shortfall split of the misses is added on top of what SCOPE.md 5.2 lists: which side of its own
guided range a missed row's reported value landed on. It is computed from the numbers each time
(`research_record.rubric.direction`), never read off a stored field: the schema carries no direction field,
and this module does not write one anywhere.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from research_record import rubric
from research_record.schema import ResearchRecord

__all__ = ["load_records", "missed_direction", "compute", "format_stats", "run"]


def load_records(path: Path) -> tuple[list[ResearchRecord], int]:
    """(records, invalid) from a JSONL file, one ResearchRecord per line. A line that is not valid JSON or
    not a valid ResearchRecord is counted as invalid and left out, not raised."""
    records: list[ResearchRecord] = []
    invalid = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(ResearchRecord.model_validate(json.loads(line)))
        except (json.JSONDecodeError, ValidationError):
            invalid += 1
    return records, invalid


def missed_direction(record: ResearchRecord) -> rubric.Direction | None:
    """"beat" or "shortfall" for a missed record's own numbers, or None when there is nothing to compare."""
    if record.outcome is None:
        return None
    return rubric.direction(record.assumption.target_low, record.assumption.target_high, record.outcome.reported_value)


def compute(records: list[ResearchRecord]) -> dict[str, Any]:
    """Pure: no file access, no model call."""
    by_status: Counter[str] = Counter(r.status for r in records)
    missed = [r for r in records if r.status == "missed"]
    by_direction: Counter[str] = Counter(missed_direction(r) or "unclassified" for r in missed)
    falsifiable = [r.days_to_falsifiable for r in records if r.days_to_falsifiable is not None]
    never_acknowledged = sum(1 for r in missed if r.acknowledged_at is None)
    by_company: dict[str, Counter[str]] = {}
    for r in records:
        by_company.setdefault(r.company, Counter())[r.status] += 1
    return {
        "rows": len(records),
        "by_status": dict(sorted(by_status.items())),
        "median_days_to_falsifiable": statistics.median(falsifiable) if falsifiable else None,
        "missed": len(missed),
        "missed_never_acknowledged": never_acknowledged,
        "missed_never_acknowledged_share": (never_acknowledged / len(missed)) if missed else None,
        "missed_by_direction": dict(sorted(by_direction.items())),
        "by_company": {company: dict(sorted(counts.items())) for company, counts in sorted(by_company.items())},
    }


def format_stats(stats: dict[str, Any]) -> str:
    share = stats["missed_never_acknowledged_share"]
    lines = [
        f"rows: {stats['rows']:,}",
        f"by status: {stats['by_status']}",
        f"median days to falsifiable: {stats['median_days_to_falsifiable']}",
        f"missed: {stats['missed']:,}, never acknowledged: {stats['missed_never_acknowledged']:,}"
        + (f" ({share:.0%})" if share is not None else ""),
        f"missed by direction (beat = above the high end, shortfall = below the low end): {stats['missed_by_direction']}",
        "by company:",
    ]
    for company, counts in stats["by_company"].items():
        lines.append(f"  {company}: {counts}")
    return "\n".join(lines)


def run(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist")
    records, invalid = load_records(path)
    text = format_stats(compute(records))
    if invalid:
        text += f"\n{invalid} row(s) did not parse as a ResearchRecord and were left out"
    return text
