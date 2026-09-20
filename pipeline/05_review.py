"""Phase 2, step 3: the review queue.

    python -m pipeline.05_review [--company TICKER ...]

Writes data/review/{cik}.csv: one row per draft, every ResearchRecord field, then the
reviewer's columns. Navneet opens the CSV, checks each row against the cached filing,
edits what the model got wrong, and sets `approved`. Only approved rows go forward;
an unapproved row is never published.

  approved       false until a human sets it
  hand_verified  set by the human for the rows they re-found on EDGAR by hand
                 (SCOPE 4.3: at least 10% of approved rows per company)
  reviewer_note  free text for the reviewer
  aid_*          context to help the reviewer. Not schema fields, and ignored downstream:
                 what the rubric would call the row, how the line was captured, the heading
                 and lead-in the period was inferred from, and why an outcome is missing.

Every row is `open`: the rubric's word for a draft nobody has reviewed. `aid_proposed_status`
is what `rubric.resolve` says from the numbers alone, so a reviewer can see the direction
of travel. It never proposes `withdrawn`: no step looks for withdrawn guidance.

This script never overwrites a review file. Rows already in a CSV are kept exactly as they
are, hand edits and all, and only drafts whose record_id is not there yet are appended.

Reads   data/drafts/{cik}.jsonl, data/outcomes/{cik}.jsonl
Writes  data/review/{cik}.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import typing
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from pipeline.common import CONFIG_PATH, DRAFTS_DIR, OUTCOMES_DIR, REVIEW_DIR, Company, companies, load_config
from research_record import rubric
from research_record.schema import ResearchRecord

REVIEWER = "navneet"
REVIEWER_COLUMNS = ["approved", "hand_verified", "reviewer_note"]
AID_COLUMNS = ["aid_proposed_status", "aid_capture_method", "aid_heading", "aid_lead_in", "aid_outcome_note"]


# --- columns ---------------------------------------------------------------


def _submodel(annotation: Any) -> type[BaseModel] | None:
    for arg in typing.get_args(annotation) or (annotation,):
        if isinstance(arg, type) and issubclass(arg, BaseModel):
            return arg
    return None


def schema_columns(model: type[BaseModel] = ResearchRecord, prefix: str = "") -> list[str]:
    """Dotted column names for every schema field, in schema order. Follows the schema, so it cannot drift from it."""
    columns: list[str] = []
    for name, field in model.model_fields.items():
        sub = _submodel(field.annotation)
        columns += schema_columns(sub, f"{prefix}{name}.") if sub else [f"{prefix}{name}"]
    return columns


COLUMNS = schema_columns() + REVIEWER_COLUMNS + AID_COLUMNS
COLUMN_SET = frozenset(COLUMNS)


def _walk(value: Any, prefix: str = ""):
    """(dotted column, text) for every leaf of a nested record. None is an empty cell."""
    if isinstance(value, dict):
        for key, inner in value.items():
            yield from _walk(inner, f"{prefix}{key}.")
    else:
        yield prefix[:-1], "" if value is None else str(value)


# --- words the schema asks for, made by code, not by the model ------------


def _amount(x: float, unit: str) -> str:
    if unit == "USD billions":
        return f"${x:g} billion"
    if unit == "USD millions":
        return f"${x:g} million"
    if unit == "USD thousands":
        return f"${x:g} thousand"
    if unit == "USD":
        return f"${x:,.2f}"
    if unit == "USD per share":
        return f"${x:g} per share"
    if unit == "percent":
        return f"{x:g}%"
    return f"{x:g} {unit}"


def range_words(a: dict[str, Any]) -> str:
    low, high, unit = a["target_low"], a["target_high"], a["unit"]
    if low is not None and high is not None:
        return _amount(low, unit) if low == high else f"{_amount(low, unit)} to {_amount(high, unit)}"
    return f"at least {_amount(low, unit)}" if low is not None else f"no more than {_amount(high, unit)}"


def restate(company: str, a: dict[str, Any]) -> str:
    return f"{company} expects {a['metric']} of {range_words(a)} for {a['target_period']}."


def invalidation(a: dict[str, Any]) -> str:
    low, high = a["target_low"], a["target_high"]
    if low is not None and high is not None:
        if low == high:
            return f"reported value differs from {low:g} by more than {rubric.POINT_TOLERANCE * 100:g}%"
        return f"reported value falls outside [{low:g}, {high:g}]"
    return f"reported value is below {low:g}" if low is not None else f"reported value is above {high:g}"


# --- assembling a row ------------------------------------------------------


def build_record(draft: dict[str, Any], outcome_row: dict[str, Any] | None, today: date) -> ResearchRecord:
    a = draft["assumption"]
    outcome = outcome_row.get("outcome") if outcome_row else None
    ack = outcome_row.get("acknowledgement") if outcome_row else None
    stated = date.fromisoformat(a["stated_at"])
    reported = date.fromisoformat(outcome["reported_at"]) if outcome else None
    acknowledged = date.fromisoformat(ack["acknowledged_at"]) if ack else None
    return ResearchRecord(
        record_id=draft["draft_id"],
        company=draft["company"],
        ticker=draft["ticker"],
        cik=draft["cik"],
        claim=restate(draft["company"], a),
        assumption=a,
        outcome=outcome,
        invalidation_condition=invalidation(a),
        status="open",
        acknowledged_at=acknowledged,
        acknowledgement_evidence=ack["evidence"] if ack else None,
        days_to_falsifiable=rubric.days_to_falsifiable(stated, reported),
        days_to_acknowledged=rubric.days_to_acknowledged(reported, acknowledged),
        last_reviewed_at=today,
        reviewer=REVIEWER,
    )


def proposed_status(record: ResearchRecord) -> str:
    """What the rubric says from the numbers alone, as if the row were reviewed."""
    return rubric.resolve_record(record, reviewed=True, withdrawn=False)


def build_row(draft: dict[str, Any], outcome_row: dict[str, Any] | None, today: date) -> dict[str, str]:
    record = build_record(draft, outcome_row, today)
    row = {c: "" for c in COLUMNS}
    # A null nested model (no outcome, no acknowledgement) walks to a single cell named after its
    # parent, which is not a column. Its real columns are the sub-fields, already blank.
    row.update({c: v for c, v in _walk(record.model_dump(mode="json")) if c in COLUMN_SET})
    row.update(
        approved="false",
        hand_verified="false",
        reviewer_note="",
        aid_proposed_status=proposed_status(record),
        aid_capture_method=draft.get("capture_method") or "",
        aid_heading=draft.get("heading") or "",
        aid_lead_in=draft.get("lead_in") or "",
        aid_outcome_note="" if record.outcome else (outcome_row or {}).get("outcome_reason") or "outcome search not run",
    )
    return row


# --- files -----------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)


def review_company(company: Company, today: date) -> tuple[list[dict[str, str]], Counter]:
    """Merge new drafts into the company's CSV. Existing rows are never touched."""
    drafts = _read_jsonl(DRAFTS_DIR / f"{company.cik}.jsonl")
    outcomes = {r["draft_id"]: r for r in _read_jsonl(OUTCOMES_DIR / f"{company.cik}.jsonl")}
    path = REVIEW_DIR / f"{company.cik}.csv"
    existing = read_csv(path) if path.exists() else []
    known = {r["record_id"] for r in existing}
    stats: Counter = Counter(kept=len(existing))
    new_rows: list[dict[str, str]] = []
    for d in drafts:
        if d["draft_id"] in known:
            continue
        try:
            new_rows.append(build_row(d, outcomes.get(d["draft_id"]), today))
        except (ValidationError, KeyError, ValueError) as exc:
            stats["invalid"] += 1
            print(f"  {d['draft_id']}: not a valid record, left out of the queue: {str(exc)[:160]}", file=sys.stderr)
    stats["added"] = len(new_rows)
    rows = existing + new_rows
    if new_rows:  # never leave an empty queue file that looks like work was done
        write_csv(path, rows)
    return rows, stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the review queue CSVs.")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--company", action="append", metavar="TICKER")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    targets = companies(config, args.company)
    today = date.today()

    print("rows per company per status ('status' is the schema field, always open for a draft;")
    print("'proposed' is what the rubric says from the numbers alone)")
    for company in targets:
        rows, stats = review_company(company, today)
        by_status = Counter(r["status"] for r in rows)
        by_proposed = Counter(r["aid_proposed_status"] for r in rows)
        print(f"  {company.ticker}: {len(rows):,} rows ({stats['added']} added, {stats['kept']} kept, {stats['invalid']} invalid)")
        print(f"     status:   {dict(sorted(by_status.items()))}")
        print(f"     proposed: {dict(sorted(by_proposed.items()))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
