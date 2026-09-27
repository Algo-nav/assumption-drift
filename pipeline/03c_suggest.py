"""Phase 2, step 3c: a suggested reviewer note for rows 03b_verify flagged "no".

    python -m pipeline.03c_suggest [--company TICKER ...]

For every row in data/review/{cik}.csv where `aid_verify == "no"` and `reviewer_note` is still
empty, works out from `aid_verify_class` and the row's own fields whether the "no" looks like a
false alarm (03b_verify's own second opinion did not have enough context, but the row is probably
fine) or is worth a closer look, and writes one line to `aid_suggested_note`:

  wrong_period   The row's own assumption.target_period is one of the period labels in
                 aid_table_header's columns: "false alarm: period from header column N" (columns
                 counted from 1, either aid_table_header's " | "-joined cells, or, for a single line
                 naming several periods, the periods in the order they appear in it). Otherwise
                 "CHECK: period".
  wrong_value    The excerpt spells out "point, plus or minus delta" (also "+/-" or "±"): if that
                 expands to exactly the row's own [target_low, target_high],
                 "false alarm: ± expands to <low>-<high>". Otherwise "CHECK: value".
  wrong_metric   aid_heading or aid_lead_in names the metric (every word of assumption.metric
                 appears in one or the other): "false alarm: metric in heading". Otherwise
                 "CHECK: metric".
  not_guidance   The excerpt itself has a forward term (config.yaml candidates.forward_terms), a
                 period label, and a number with a unit (candidates.number_units):
                 "false alarm: forward verb, period, number present". Otherwise "CHECK: guidance".
  wrong_sign, other, or a blank class   Always "CHECK: <class>" (or "CHECK: other" if blank):
                 nothing here recomputes those.

This is pure code, no model call, and it never touches `aid_verify`, `aid_verify_reason` or
`aid_verify_class` themselves, or any reviewer column (`approved`, `hand_verified`,
`reviewer_note`). A row that does not currently qualify (a "yes", not yet checked, or already
carrying a reviewer_note) has its aid_suggested_note cleared to blank: this is a suggestion, not a
record of one, and it is only ever right for the row's *current* state.

`rr review` shows a row's aid_suggested_note and lets Enter accept it as the reviewer_note on `y`
(see research_record/reviewer.py); this script only ever fills the column in, it never approves or
rejects anything.

Reads   data/review/{cik}.csv, pipeline/config.yaml (candidates.forward_terms, candidates.number_units)
Writes  data/review/{cik}.csv       aid_suggested_note filled in (or cleared) on every row
"""

from __future__ import annotations

import argparse
import importlib
import re
from pathlib import Path
from typing import Any

from pipeline.common import CONFIG_PATH, REVIEW_DIR, companies, load_config

candidates = importlib.import_module("pipeline.02_candidates")
review = importlib.import_module("pipeline.05_review")

SUGGESTED_NOTE = "aid_suggested_note"


# --- which rows this touches ---------------------------------------------------


def qualifies(row: dict[str, str]) -> bool:
    """aid_verify is "no" and nobody has written a reviewer_note yet."""
    return row.get("aid_verify") == "no" and not row.get("reviewer_note", "").strip()


# --- wrong_period: does the header already say this period? -------------------


def _period_tuple(text: str) -> tuple[int | None, int] | None:
    periods = candidates.parse_periods(text or "")
    return periods[0] if len(periods) == 1 else None


def _header_column(header: str, target: tuple[int | None, int]) -> int | None:
    """1-based column whose period label is `target`, or None. A " | "-joined header (cells broken
    onto separate lines) is checked cell by cell; a single line naming several periods is checked
    in the order those periods appear in it."""
    if not header:
        return None
    if " | " in header:
        for i, cell in enumerate(header.split(" | "), start=1):
            if target in candidates.parse_periods(cell):
                return i
        return None
    for i, period in enumerate(candidates.parse_periods(header), start=1):
        if period == target:
            return i
    return None


def suggest_wrong_period(row: dict[str, str]) -> str:
    target = _period_tuple(row.get("assumption.target_period", ""))
    column = _header_column(row.get("aid_table_header", ""), target) if target is not None else None
    return f"false alarm: period from header column {column}" if column is not None else "CHECK: period"


# --- wrong_value: does "point, plus or minus delta" expand to the stored range? -


_NUMBER_TOKEN = r"[$€£]?\s*\d[\d,]*(?:\.\d+)?\s*%?"
_PLUS_MINUS = re.compile(
    rf"(?P<point>{_NUMBER_TOKEN})[^0-9]{{0,40}}?(?:plus or minus|\+/-|±)\s*(?P<delta>{_NUMBER_TOKEN})",
    re.IGNORECASE,
)


def _to_float(token: str) -> float:
    return float(re.sub(r"[^0-9.]", "", token))


def suggest_wrong_value(row: dict[str, str]) -> str:
    match = _PLUS_MINUS.search(row.get("assumption.evidence.excerpt", ""))
    if not match:
        return "CHECK: value"
    point, delta = _to_float(match["point"]), _to_float(match["delta"])
    low, high = point - delta, point + delta
    try:
        stored_low = float(row.get("assumption.target_low") or "nan")
        stored_high = float(row.get("assumption.target_high") or "nan")
    except ValueError:
        return "CHECK: value"
    tolerance = max(1e-6, abs(stored_low) * 1e-4, abs(stored_high) * 1e-4)
    if abs(low - stored_low) <= tolerance and abs(high - stored_high) <= tolerance:
        return f"false alarm: ± expands to {low:g}-{high:g}"
    return "CHECK: value"


# --- wrong_metric: does the heading or lead-in already name it? ---------------


_WORD = re.compile(r"[a-z0-9]+")


def suggest_wrong_metric(row: dict[str, str]) -> str:
    words = set(_WORD.findall(row.get("assumption.metric", "").lower()))
    label = f"{row.get('aid_heading', '')} {row.get('aid_lead_in', '')}".lower()
    if words and all(word in label for word in words):
        return "false alarm: metric in heading"
    return "CHECK: metric"


# --- not_guidance: does the excerpt itself have a verb, a period, a number? ----


def suggest_not_guidance(row: dict[str, str], forward: re.Pattern[str], number: re.Pattern[str]) -> str:
    excerpt = row.get("assumption.evidence.excerpt", "")
    if forward.search(excerpt) and candidates.parse_periods(excerpt) and number.search(excerpt):
        return "false alarm: forward verb, period, number present"
    return "CHECK: guidance"


# --- dispatch --------------------------------------------------------------


def suggest_note(row: dict[str, str], forward: re.Pattern[str], number: re.Pattern[str]) -> str:
    klass = row.get("aid_verify_class", "")
    if klass == "wrong_period":
        return suggest_wrong_period(row)
    if klass == "wrong_value":
        return suggest_wrong_value(row)
    if klass == "wrong_metric":
        return suggest_wrong_metric(row)
    if klass == "not_guidance":
        return suggest_not_guidance(row, forward, number)
    return f"CHECK: {klass or 'other'}"  # wrong_sign, other, or (should not happen) a blank class


# --- a company's file --------------------------------------------------------


def suggest_company(rows: list[dict[str, str]], forward: re.Pattern[str], number: re.Pattern[str]) -> tuple[list[dict[str, str]], int, int]:
    """(rows with aid_suggested_note filled in or cleared, false-alarm count, CHECK count)."""
    updated: list[dict[str, str]] = []
    false_alarms = checks = 0
    for row in rows:
        if not qualifies(row):
            updated.append({**row, SUGGESTED_NOTE: ""})
            continue
        note = suggest_note(row, forward, number)
        if note.startswith("false alarm"):
            false_alarms += 1
        else:
            checks += 1
        updated.append({**row, SUGGESTED_NOTE: note})
    return updated, false_alarms, checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Suggest a reviewer note for every row 03b_verify flagged \"no\".")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--company", action="append", metavar="TICKER")
    args = parser.parse_args(argv)

    config: dict[str, Any] = load_config(args.config)
    targets = companies(config, args.company)
    forward, number = candidates.compile_matchers(config["candidates"])

    for company in targets:
        path = REVIEW_DIR / f"{company.cik}.csv"
        if not path.exists():
            continue
        rows = review.read_csv(path)
        updated, false_alarms, checks = suggest_company(rows, forward, number)
        review.write_csv(path, updated)
        print(f"  {company.ticker}: {false_alarms:,} false alarm, {checks:,} CHECK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
