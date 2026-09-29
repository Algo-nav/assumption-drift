"""`rr stats <path>` (SCOPE.md section 5.2): summary numbers over a file of published records.

Reads a JSONL file, one ResearchRecord per line, and prints the row count, per-status counts, the median
`days_to_falsifiable`, the share of misses never acknowledged, and a per-company breakdown. This output
feeds the LinkedIn posts, so it stays plain and short.

On top of what SCOPE.md 5.2 lists, added for Phase 3:

- **better/worse split of the misses**: whether a missed row's reported value landed on the good or the
  bad side of its own guided range, computed from the numbers and the metric's polarity each time
  (`research_record.rubric.direction`, `higher_is_better` in pipeline/config.yaml), never read off a
  stored field: the schema carries no direction field, and this module does not write one anywhere.
  Above the range is better for revenue and worse for operating expenses.
- **one-sided floors, counted separately**: a floor ("at least X", `target_high` is null) can only ever
  be met or fall below; there is nothing above it to exceed, so lumping it into the better/worse split
  would silently under-count "better" as if a floor had a ceiling it does not have.
- **median `days_to_falsifiable` by direction**, and **acknowledgement rate by direction**: among missed
  rows only, split the same way.
- **a per-company table** (`company_table`): rows, the same per-status counts as `by_status`, and the
  never-acknowledged share, one entry per company, for `format_stats`'s table and for `pipeline/06_publish.py`'s
  card renderer. `by_company` (bare `{status: count}`, unchanged) stays for anything that only wants that.

`rr stats --json` (`run(path, as_json=True)`) prints `compute()`'s own dict as JSON, for the card renderer
to consume without re-parsing the plain-text report.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from research_record import polarity, rubric
from research_record.schema import ResearchRecord

__all__ = ["load_records", "missed_direction", "is_floor", "compute", "format_stats", "run"]

_DIRECTIONS: tuple[rubric.Direction, ...] = ("better", "worse")


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
    """"better" or "worse" for a missed record's own numbers, or None when there is nothing to compare."""
    return polarity.record_direction(record)


def is_floor(record: ResearchRecord) -> bool:
    """A one-sided lower bound ("at least X"): a target_low with no target_high. It can be met or fall
    below it; there is no ceiling above it to exceed, so it is counted apart from the better/worse split."""
    a = record.assumption
    return a.target_low is not None and a.target_high is None


def _median(values: list[float | int]) -> float | None:
    return statistics.median(values) if values else None


def _by_direction(missed: list[ResearchRecord]) -> dict[rubric.Direction, list[ResearchRecord]]:
    buckets: dict[rubric.Direction, list[ResearchRecord]] = {d: [] for d in _DIRECTIONS}
    for r in missed:
        d = missed_direction(r)
        if d is not None:
            buckets[d].append(r)
    return buckets


def _company_row(company: str, records: list[ResearchRecord]) -> dict[str, Any]:
    missed = [r for r in records if r.status == "missed"]
    never_acknowledged = sum(1 for r in missed if r.acknowledged_at is None)
    return {
        "company": company,
        "rows": len(records),
        "by_status": dict(sorted(Counter(r.status for r in records).items())),
        "missed_never_acknowledged_share": (never_acknowledged / len(missed)) if missed else None,
    }


def compute(records: list[ResearchRecord]) -> dict[str, Any]:
    """Pure: no file access, no model call."""
    by_status: Counter[str] = Counter(r.status for r in records)
    missed = [r for r in records if r.status == "missed"]
    by_direction: Counter[str] = Counter(missed_direction(r) or "unclassified" for r in missed)
    falsifiable = [r.days_to_falsifiable for r in records if r.days_to_falsifiable is not None]
    never_acknowledged = sum(1 for r in missed if r.acknowledged_at is None)
    missed_by_direction_records = _by_direction(missed)
    by_company: dict[str, Counter[str]] = {}
    for r in records:
        by_company.setdefault(r.company, Counter())[r.status] += 1
    companies = sorted({r.company for r in records})
    return {
        "rows": len(records),
        "by_status": dict(sorted(by_status.items())),
        "median_days_to_falsifiable": statistics.median(falsifiable) if falsifiable else None,
        "missed": len(missed),
        "missed_never_acknowledged": never_acknowledged,
        "missed_never_acknowledged_share": (never_acknowledged / len(missed)) if missed else None,
        "missed_by_direction": dict(sorted(by_direction.items())),
        "missed_floors": sum(1 for r in missed if is_floor(r)),
        "median_days_to_falsifiable_by_direction": {
            d: _median([r.days_to_falsifiable for r in group if r.days_to_falsifiable is not None])
            for d, group in missed_by_direction_records.items()
        },
        "acknowledged_rate_by_direction": {
            d: ((sum(1 for r in group if r.acknowledged_at is not None) / len(group)) if group else None)
            for d, group in missed_by_direction_records.items()
        },
        "by_company": {company: dict(sorted(counts.items())) for company, counts in sorted(by_company.items())},
        "company_table": [_company_row(c, [r for r in records if r.company == c]) for c in companies],
    }


def _company_table_lines(company_table: list[dict[str, Any]]) -> list[str]:
    if not company_table:
        return ["  (no rows)"]
    columns = ["met", "missed", "withdrawn", "unresolved"]
    header = ["company", "rows", *columns, "never-ack share"]
    widths = [max(len(header[i]), *(len(_cell(row, header[i], columns)) for row in company_table)) for i in range(len(header))]
    lines = ["  " + "  ".join(h.ljust(w) for h, w in zip(header, widths))]
    for row in company_table:
        cells = [_cell(row, h, columns) for h in header]
        lines.append("  " + "  ".join(c.ljust(w) for c, w in zip(cells, widths)))
    return lines


def _cell(row: dict[str, Any], column: str, status_columns: list[str]) -> str:
    if column == "company":
        return row["company"]
    if column == "rows":
        return str(row["rows"])
    if column == "never-ack share":
        share = row["missed_never_acknowledged_share"]
        return f"{share:.0%}" if share is not None else "-"
    if column in status_columns:
        return str(row["by_status"].get(column, 0))
    raise KeyError(column)  # pragma: no cover - every header name is handled above


def format_stats(stats: dict[str, Any]) -> str:
    share = stats["missed_never_acknowledged_share"]
    lines = [
        f"rows: {stats['rows']:,}",
        f"by status: {stats['by_status']}",
        f"median days to falsifiable: {stats['median_days_to_falsifiable']}",
        f"missed: {stats['missed']:,}, never acknowledged: {stats['missed_never_acknowledged']:,}"
        + (f" ({share:.0%})" if share is not None else ""),
        f"missed by direction (better or worse than guided, by each metric's polarity): {stats['missed_by_direction']}",
        f"missed as one-sided floors (only a value below a floor can be missed; a floor has no ceiling): {stats['missed_floors']:,}",
        f"median days to falsifiable by direction: {stats['median_days_to_falsifiable_by_direction']}",
        f"acknowledged rate by direction: "
        f"{ {d: (f'{v:.0%}' if v is not None else None) for d, v in stats['acknowledged_rate_by_direction'].items()} }",
        "company table:",
    ]
    lines.extend(_company_table_lines(stats["company_table"]))
    return "\n".join(lines)


def run(path: Path, *, as_json: bool = False) -> str:
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist")
    records, invalid = load_records(path)
    result = compute(records)
    if as_json:
        return json.dumps({**result, "invalid": invalid}, indent=2)
    text = format_stats(result)
    if invalid:
        text += f"\n{invalid} row(s) did not parse as a ResearchRecord and were left out"
    return text
