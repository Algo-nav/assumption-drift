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
  conflict       true when 03_structure found two section-captured drafts for the same
                 company, metric, period and filing date with different numbers. Both rows
                 are here, both marked, and neither was chosen: the reviewer decides which
                 (if either) is right and approves at most one. Not a schema field, and it
                 never leaves this queue.
  empty_block    true on a row that is not a draft at all: 03_structure listed an 8-K outlook block
                 that the model answered but that produced no draft (an empty answer, or every item
                 rejected by a guard), and this row is the flag to go and look at it. It carries the
                 filing's source and the start of the block, and `aid_flag_note` says why it is empty.
                 It is not a record: its metric, unit and range are blank, so it cannot be published
                 whatever is put in `approved`. If the block does hold guidance, add the rows by hand.
  aid_*          context to help the reviewer. Not schema fields, and ignored downstream:
                 what the rubric would call the row, how the line was captured, the heading,
                 lead-in and table header row the period was inferred from, and why an
                 outcome is missing. On an ordinary draft row `aid_flag_note` is blank unless
                 03_structure's parentheses guard (fix_parens_sign) changed a rate metric's sign, in
                 which case it says so: reused from the empty-block flag below, since the two never
                 collide (a row is one or the other).
  aid_verify        a second machine opinion, written by pipeline/03b_verify.py, not by this script: "yes" or
                    "no" for whether the excerpt states exactly the row's metric, numbers, unit and period, with
                    aid_verify_reason saying why in one line and, on a "no", aid_verify_class saying what kind of
                    mismatch it is (wrong_metric, wrong_value, wrong_period, wrong_sign, not_guidance, other).
                    Blank until that script has run on the row. It is a prompt to look closer, never a decision:
                    it does not touch `approved` or any other reviewer column.

Every row is `open`: the rubric's word for a draft nobody has reviewed. `aid_proposed_status`
is what `rubric.resolve` says from the numbers alone, so a reviewer can see the direction
of travel. It proposes `withdrawn` when 04_outcomes found an 8-K exhibit in which the company
withdrew, suspended or stopped providing guidance covering the row's metric and period, dated
on or before the day the period closed (`rubric.withdrawn_before_close`), and then
`aid_withdrawal_note` carries the filing date, its source and the sentence, to check by hand.
A withdrawn row is still a draft: the status field is `open`, and a person decides.

This script never overwrites a review file. Rows already in a CSV are kept exactly as they
are, hand edits and all, and only drafts whose record_id is not there yet are appended.

Reads   data/drafts/{cik}.jsonl, data/drafts/{cik}.empty_blocks.jsonl, data/outcomes/{cik}.jsonl
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
FLAG_COLUMNS = ["conflict", "empty_block"]
AID_COLUMNS = ["aid_proposed_status", "aid_capture_method", "aid_heading", "aid_lead_in", "aid_table_header", "aid_outcome_note",
               "aid_flag_note", "aid_withdrawal_note", "aid_verify", "aid_verify_reason", "aid_verify_class"]


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


COLUMNS = schema_columns() + REVIEWER_COLUMNS + FLAG_COLUMNS + AID_COLUMNS
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


def proposed_status(record: ResearchRecord, *, withdrawn: bool = False) -> str:
    """What the rubric says from the numbers alone, as if the row were reviewed. `withdrawn` is a fact from a later
    filing, not from the numbers, and the rubric gives it precedence."""
    return rubric.resolve_record(record, reviewed=True, withdrawn=withdrawn)


def withdrawal_of(outcome_row: dict[str, Any] | None) -> dict[str, Any] | None:
    """The withdrawal 04_outcomes found, if it was dated on or before the day the period closed. The rubric's own date
    test is applied again here, so a withdrawal that is not one under the rubric is never proposed."""
    found = (outcome_row or {}).get("withdrawal")
    if not found:
        return None
    try:
        before_close = rubric.withdrawn_before_close(date.fromisoformat(found["withdrawn_at"]), date.fromisoformat(found["period_close"]))
    except (KeyError, TypeError, ValueError):  # a missing key, a null, or a string that is not a date
        return None
    return found if before_close else None


def build_row(draft: dict[str, Any], outcome_row: dict[str, Any] | None, today: date) -> dict[str, str]:
    record = build_record(draft, outcome_row, today)
    withdrawal = withdrawal_of(outcome_row)
    row = {c: "" for c in COLUMNS}
    # A null nested model (no outcome, no acknowledgement) walks to a single cell named after its
    # parent, which is not a column. Its real columns are the sub-fields, already blank.
    row.update({c: v for c, v in _walk(record.model_dump(mode="json")) if c in COLUMN_SET})
    row.update(
        approved="false",
        hand_verified="false",
        reviewer_note="",
        conflict="true" if draft.get("conflict") else "false",
        empty_block="false",
        aid_proposed_status=proposed_status(record, withdrawn=withdrawal is not None),
        aid_capture_method=draft.get("capture_method") or "",
        aid_heading=draft.get("heading") or "",
        aid_lead_in=draft.get("lead_in") or "",
        aid_table_header=draft.get("table_header") or "",
        aid_outcome_note="" if record.outcome else (outcome_row or {}).get("outcome_reason") or "outcome search not run",
        aid_flag_note=draft.get("parens_note") or "",
        aid_withdrawal_note=(f"filed {withdrawal['withdrawn_at']}, before the period closed on {withdrawal['period_close']} "
                             f"({withdrawal['evidence']['source_url']}): {withdrawal['evidence']['excerpt']}") if withdrawal else "",
    )
    return row


def build_empty_block_row(block: dict[str, Any]) -> dict[str, str]:
    """A flag, not a record: an outlook block that produced no draft. It names the company and the filing, gives the
    source and the start of the block as its excerpt, and leaves everything a record needs (metric, unit, range,
    period, claim) blank so that it can never be mistaken for one."""
    row = {c: "" for c in COLUMNS}
    row.update(record_id=block["block_id"], company=block["company"], ticker=block["ticker"], cik=block["cik"], status="open")
    row["assumption.stated_at"] = block["filed_at"]
    if block.get("evidence"):
        row.update({c: v for c, v in _walk(block["evidence"], "assumption.evidence.") if c in COLUMN_SET})
    row.update(
        approved="false",
        hand_verified="false",
        conflict="false",
        empty_block="true",
        aid_capture_method="section",
        aid_heading=block.get("heading") or "",
        aid_lead_in=block.get("lead_in") or "",
        aid_table_header=block.get("table_header") or "",
        aid_flag_note=block["reason"],
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
    blocks = _read_jsonl(DRAFTS_DIR / f"{company.cik}.empty_blocks.jsonl")
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
    for block in blocks:  # flags for outlook blocks that produced no draft; added once, never touched again
        if block["block_id"] not in known:
            new_rows.append(build_empty_block_row(block))
            stats["flagged"] += 1
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
        drafted = [r for r in rows if r.get("empty_block") != "true"]
        by_status = Counter(r["status"] for r in drafted)
        by_proposed = Counter(r["aid_proposed_status"] for r in drafted)
        print(f"  {company.ticker}: {len(rows):,} rows ({stats['added']} added, {stats['kept']} kept, {stats['invalid']} invalid; "
              f"{sum(r.get('empty_block') == 'true' for r in rows)} empty-block flags, {stats['flagged']} new)")
        print(f"     status:   {dict(sorted(by_status.items()))}")
        print(f"     proposed: {dict(sorted(by_proposed.items()))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
