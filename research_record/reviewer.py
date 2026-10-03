"""`rr review <csv>`: a terminal, one-row-at-a-time reviewer for a review queue CSV
(data/review/{cik}.csv, written by pipeline/05_review.py and pipeline/03b_verify.py).

Each row is shown with its target numbers highlighted in the excerpt, the outcome and its own excerpt
below that, and the EDGAR URL the evidence came from. Keys:

  y  approve                  approved -> true
  n  reject, with a note      approved -> false, reviewer_note -> what you type
  e  edit a field             type a column name, then its new value; stays on the row
  s  skip                     moves on, changes nothing
  v  mark hand-verified       hand_verified -> true; stays on the row
  q  quit                     stop here; everything already written is already on disk

A row with `empty_block=true` is not a draft record, so `y` on one refuses: it prints "empty block,
use s" and leaves the row untouched. Use `s` to move past it.

Every keypress writes the file back to disk before the next row is shown, so an interrupted session
loses nothing already decided. SCOPE 4.3 requires hand-verifying at least 10% of approved rows per
company: on every tenth approval (this file's running total, not just this session) the tool stops,
prints the EDGAR URL again, and asks whether to mark that row hand-verified on the spot.

`run(path, filters=[...])` restricts which rows are shown. Each filter spec is one of:

  verify-no      only rows where aid_verify == "no"
  no-note        only rows with an empty reviewer_note
  ids=A,B,C      only rows whose record_id is A, B, or C
  ack-pending    only rows with `ack_pending_review=true`: 04 found an acknowledgement for a row that was
                 already reviewed, and it waits in the `aid_ack_proposed_*` columns
  change-pending only rows with `change_pending_review=true`: a re-derive changed an assumption field, a flag or
                 a sign on a row that was already touched, and the new values wait in `aid_proposed_change`

Multiple specs combine with AND. Rows that do not match are never shown and never written to
differently than they already were. When any filter is active, approving a row requires a non-empty
reviewer_note: if `y` is pressed and the note is still empty, the tool prompts for one and saves it
before approving.

On a row with `ack_pending_review=true` the proposed acknowledgement is shown apart from the real one, and
the keys mean something narrower: `y` copies the proposal into the real acknowledgement columns and clears
the flag (approval is left as it is), `n` clears the proposal and records the reason in `reviewer_note`
with the proposal's date and URL, so a later refresh does not propose the same one again.

On a row with `change_pending_review=true` a "PROPOSED CHANGE (not yet reviewed)" block shows each proposed
column with its current value and its proposed value side by side; the real columns are untouched until you
decide. `y` writes the proposed values into the real columns, clears the proposal and adds "proposed change
applied" to `reviewer_note` (approval is left as it is: the reviewer has just seen both values), `n` clears the
proposal and records the reason in `reviewer_note` with the change's `[hash]`, so a later refresh does not propose
the same change again. A row with both a change and an acknowledgement pending settles the change first and stays
on the row for the acknowledgement.

A row with an `aid_suggested_note` (written by pipeline/03c_suggest.py, on a row 03b_verify flagged
"no") shows it, and `y` on that row prompts for a note with the suggestion as the default: press
Enter to accept it as reviewer_note, or type something else to use that instead. It never approves
by itself; the reviewer still has to press `y`.

This module has no network calls and reads no other part of the pipeline: it works on any CSV that has
a `record_id` column, so it does not need to import pipeline code, and nothing in the pipeline needs to
import this. Its interactive loop takes its I/O (reading a key, reading a line, printing) as arguments,
so it can be driven by a script or a test without a real terminal.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, TextIO

RECORD_ID = "record_id"
APPROVED = "approved"
HAND_VERIFIED = "hand_verified"
REVIEWER_NOTE = "reviewer_note"
EMPTY_BLOCK = "empty_block"
SUGGESTED_NOTE = "aid_suggested_note"
EDGAR_URL_COLUMN = "assumption.evidence.source_url"
EXCERPT_COLUMN = "assumption.evidence.excerpt"
OUTCOME_VALUE_COLUMN = "outcome.reported_value"
OUTCOME_DATE_COLUMN = "outcome.reported_at"
OUTCOME_EXCERPT_COLUMN = "outcome.evidence.excerpt"
ACK_DATE_COLUMN = "acknowledged_at"
ACK_EXCERPT_COLUMN = "acknowledgement_evidence.excerpt"
ACK_URL_COLUMN = "acknowledgement_evidence.source_url"
ACK_PENDING = "ack_pending_review"
PROPOSED_AT, PROPOSED_EXCERPT, PROPOSED_URL, PROPOSED_EVIDENCE = (
    "aid_ack_proposed_at", "aid_ack_proposed_excerpt", "aid_ack_proposed_url", "aid_ack_proposed_evidence")
ACK_EVIDENCE_PREFIX = "acknowledgement_evidence."
CHANGE_PENDING = "change_pending_review"
PROPOSED_CHANGE = "aid_proposed_change"

HAND_VERIFY_EVERY = 10  # SCOPE 4.3: at least 10% of approved rows per company

PROTECTED_FIELDS = frozenset({RECORD_ID})  # editing the row's own identity would desync it from its evidence

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?%?")


def highlight_numbers(text: str, wrap: Callable[[str], str] | None = None) -> str:
    """Every numeric token in `text` (the figures a reviewer has to check), wrapped for display. `wrap`
    defaults to bold yellow ANSI; pass a plain wrapper (e.g. `lambda s: f"[{s}]"`) to test, or to run
    somewhere ANSI escapes are not wanted."""
    if not text:
        return text
    wrap = wrap or (lambda s: f"\x1b[1;33m{s}\x1b[0m")
    return _NUMBER.sub(lambda m: wrap(m.group()), text)


# --- the file ------------------------------------------------------------------


@dataclass
class ReviewFile:
    """A CSV read and held in memory, whatever its columns are: what `record_id` needs, plus whatever
    else the row carries. Saved back with its own columns, in their own order, unchanged."""

    path: Path
    fieldnames: list[str]
    rows: list[dict[str, str]]

    @classmethod
    def load(cls, path: Path) -> "ReviewFile":
        if not path.exists():
            raise FileNotFoundError(f"{path}: no such file")
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
            fieldnames = list(reader.fieldnames or [])
        if RECORD_ID not in fieldnames:
            raise ValueError(f"{path}: no {RECORD_ID!r} column; this does not look like a review queue CSV")
        return cls(path=path, fieldnames=fieldnames, rows=rows)

    def save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        with tmp.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=self.fieldnames, lineterminator="\n")
            writer.writeheader()
            writer.writerows(self.rows)
        os.replace(tmp, self.path)


# --- what each key does, pure -------------------------------------------------


def approve(row: dict[str, str]) -> dict[str, str]:
    return {**row, APPROVED: "true"}


def reject(row: dict[str, str], note: str) -> dict[str, str]:
    return {**row, APPROVED: "false", REVIEWER_NOTE: note}


def is_ack_pending(row: dict[str, str]) -> bool:
    return row.get(ACK_PENDING) == "true" and row.get(EMPTY_BLOCK) != "true"


def _clear_proposal(row: dict[str, str]) -> dict[str, str]:
    return {**row, ACK_PENDING: "false", PROPOSED_AT: "", PROPOSED_EXCERPT: "", PROPOSED_URL: "", PROPOSED_EVIDENCE: ""}


def accept_acknowledgement(row: dict[str, str]) -> dict[str, str]:
    """The proposal copied into the real acknowledgement columns (the date, every evidence column, and
    days_to_acknowledged from the row's own outcome date), the proposal and the flag cleared. Approval is
    not touched. A row missing the evidence JSON (an older proposal) still gets its date, excerpt and URL."""
    try:
        evidence = json.loads(row.get(PROPOSED_EVIDENCE) or "{}")
    except json.JSONDecodeError:
        evidence = {}
    evidence.setdefault("excerpt", row.get(PROPOSED_EXCERPT, ""))
    evidence.setdefault("source_url", row.get(PROPOSED_URL, ""))
    updated = {**row, ACK_DATE_COLUMN: row.get(PROPOSED_AT, "")}
    updated.update({f"{ACK_EVIDENCE_PREFIX}{k}": v for k, v in evidence.items() if f"{ACK_EVIDENCE_PREFIX}{k}" in row})
    try:
        gap = (date.fromisoformat(updated[ACK_DATE_COLUMN]) - date.fromisoformat(row[OUTCOME_DATE_COLUMN])).days
        updated["days_to_acknowledged"] = str(gap)
    except (KeyError, ValueError):
        pass
    return _clear_proposal(updated)


def reject_acknowledgement(row: dict[str, str], reason: str) -> dict[str, str]:
    """The proposal cleared, the reason added to `reviewer_note` with the proposal's (date, url), which is
    what tells a later refresh not to propose the same acknowledgement again. Approval is not touched."""
    note = f"acknowledgement proposal rejected ({row.get(PROPOSED_AT, '')}, {row.get(PROPOSED_URL, '')}): {reason}".rstrip(": ")
    existing = row.get(REVIEWER_NOTE, "").strip()
    return {**_clear_proposal(row), REVIEWER_NOTE: f"{existing}; {note}" if existing else note}


def is_change_pending(row: dict[str, str]) -> bool:
    return row.get(CHANGE_PENDING) == "true" and row.get(EMPTY_BLOCK) != "true"


def proposed_changes(row: dict[str, str]) -> dict[str, str]:
    """The proposed columns and their new values, {} when there is nothing readable."""
    try:
        changes = json.loads(row.get(PROPOSED_CHANGE) or "{}")
    except json.JSONDecodeError:
        return {}
    return {k: "" if v is None else str(v) for k, v in changes.items()} if isinstance(changes, dict) else {}


def change_hash(changes: dict[str, str]) -> str:
    """The same name pipeline/05_review.py gives this change, so a rejection is remembered across refreshes."""
    return hashlib.sha1(json.dumps(changes, sort_keys=True).encode("utf-8")).hexdigest()[:10]


def _clear_change(row: dict[str, str]) -> dict[str, str]:
    return {**row, CHANGE_PENDING: "false", PROPOSED_CHANGE: ""}


def _add_note(row: dict[str, str], note: str) -> str:
    existing = row.get(REVIEWER_NOTE, "").strip()
    return f"{existing}; {note}" if existing else note


def accept_change(row: dict[str, str]) -> dict[str, str]:
    """The proposed values copied into the real columns (only columns this file has), the proposal and the flag
    cleared, "proposed change applied" added to the note. Approval is not touched."""
    changes = proposed_changes(row)
    updated = {**row, **{c: v for c, v in changes.items() if c in row}}
    updated[REVIEWER_NOTE] = _add_note(row, f"proposed change applied [{change_hash(changes)}]: {', '.join(sorted(changes))}")
    return _clear_change(updated)


def reject_change(row: dict[str, str], reason: str) -> dict[str, str]:
    """The proposal cleared, the reason added to `reviewer_note` with the change's `[hash]`, which is what tells a
    later refresh not to propose this very change again. The real columns and approval are not touched."""
    note = f"proposed change rejected [{change_hash(proposed_changes(row))}]: {reason}".rstrip(": ")
    return {**_clear_change(row), REVIEWER_NOTE: _add_note(row, note)}


def hand_verify(row: dict[str, str]) -> dict[str, str]:
    return {**row, HAND_VERIFIED: "true"}


def edit_field(row: dict[str, str], field: str, value: str) -> dict[str, str]:
    if field in PROTECTED_FIELDS:
        raise KeyError(f"{field!r} cannot be edited")
    if field not in row:
        raise KeyError(f"{field!r} is not a column in this file")
    return {**row, field: value}


def approved_count(rows: list[dict[str, str]]) -> int:
    return sum(1 for r in rows if r.get(APPROVED) == "true")


def due_for_hand_verify(rows: list[dict[str, str]]) -> bool:
    """True right when an approval has just made the running total a multiple of ten (SCOPE 4.3)."""
    n = approved_count(rows)
    return n > 0 and n % HAND_VERIFY_EVERY == 0


# --- filters ---------------------------------------------------------------


AID_VERIFY_COLUMN = "aid_verify"


def parse_filter(spec: str) -> Callable[[dict[str, str]], bool]:
    """One `--filter` spec into a predicate over a row. Raises ValueError on an unrecognised spec."""
    if spec == "verify-no":
        return lambda row: row.get(AID_VERIFY_COLUMN) == "no"
    if spec == "no-note":
        return lambda row: not row.get(REVIEWER_NOTE)
    if spec == "ack-pending":
        return lambda row: row.get(ACK_PENDING) == "true"
    if spec == "change-pending":
        return lambda row: row.get(CHANGE_PENDING) == "true"
    if spec.startswith("ids="):
        ids = {part.strip() for part in spec[len("ids="):].split(",") if part.strip()}
        return lambda row: row.get(RECORD_ID) in ids
    raise ValueError(f"unrecognised filter: {spec!r}")


def combine_filters(specs: list[str]) -> Callable[[dict[str, str]], bool]:
    """All given specs ANDed together; an empty list matches every row."""
    predicates = [parse_filter(spec) for spec in specs]
    return lambda row: all(p(row) for p in predicates)


# --- showing a row -------------------------------------------------------------


def _range_text(row: dict[str, str]) -> str:
    low, high, unit = row.get("assumption.target_low") or None, row.get("assumption.target_high") or None, row.get("assumption.unit", "")
    if low and high:
        body = low if low == high else f"{low} to {high}"
    elif low:
        body = f"at least {low}"
    elif high:
        body = f"no more than {high}"
    else:
        body = "(none)"
    return f"{body} {unit}".strip()


def render_row(row: dict[str, str], position: int, total: int, wrap: Callable[[str], str] | None = None) -> str:
    lines = [
        f"── {position}/{total} ── {row.get('ticker', '')}  {row.get('assumption.metric', '')}  "
        f"{row.get('assumption.target_period', '')}  (stated {row.get('assumption.stated_at', '')})"
    ]
    if row.get(EMPTY_BLOCK) == "true":
        lines.append(f"OUTLOOK BLOCK WITH NO DRAFT, not a record: {row.get('aid_flag_note', '')}")
    else:
        lines.append(f"target: {_range_text(row)}")
    url = row.get(EDGAR_URL_COLUMN, "")
    if url:
        lines.append(f"EDGAR: {url}")
    lines += ["", "excerpt:", f"  {highlight_numbers(row.get(EXCERPT_COLUMN, ''), wrap) or '(none)'}"]
    value = row.get(OUTCOME_VALUE_COLUMN, "")
    if value:
        lines += ["", f"outcome: reported {value} on {row.get(OUTCOME_DATE_COLUMN, '')}",
                  f"  {highlight_numbers(row.get(OUTCOME_EXCERPT_COLUMN, ''), wrap) or '(none)'}"]
    else:
        lines += ["", f"outcome: none ({row.get('aid_outcome_note', '') or 'no reason recorded'})"]
    if row.get(EMPTY_BLOCK) == "true":
        pass  # a flag row is not a record: no outcome or acknowledgement to show
    elif row.get(ACK_DATE_COLUMN) or row.get(ACK_EXCERPT_COLUMN):
        lines += ["", f"acknowledgement: {row.get(ACK_DATE_COLUMN, '') or '(no date)'}",
                  f"  {highlight_numbers(row.get(ACK_EXCERPT_COLUMN, ''), wrap) or '(none)'}",
                  f"  EDGAR: {row.get(ACK_URL_COLUMN, '') or '(none)'}"]
    else:
        lines += ["", "acknowledgement: none"]
    if is_ack_pending(row):
        lines += ["", "ACKNOWLEDGEMENT (proposed, not yet reviewed)",
                  f"  {row.get(PROPOSED_AT, '') or '(no date)'}",
                  f"  {highlight_numbers(row.get(PROPOSED_EXCERPT, ''), wrap) or '(none)'}",
                  f"  EDGAR: {row.get(PROPOSED_URL, '') or '(none)'}"]
    if is_change_pending(row):
        changes = proposed_changes(row)
        width = max([len("field")] + [len(c) for c in changes])
        now_width = min(40, max([len("now")] + [len(row.get(c, "")) for c in changes]))
        lines += ["", "PROPOSED CHANGE (not yet reviewed)",
                  f"  {'field':<{width}}  {'now':<{now_width}}  proposed"]
        for column in sorted(changes):
            now = _clip(row.get(column, "") or "(blank)", 40)
            lines.append(f"  {column:<{width}}  {now:<{now_width}}  {_clip(changes[column] or '(blank)', 70)}")
    lines += [
        "",
        f"proposed: {row.get('aid_proposed_status', '')}  verify: {row.get('aid_verify', '') or '(not checked)'}"
        + (f" ({row['aid_verify_reason']})" if row.get('aid_verify') == 'no' and row.get('aid_verify_reason') else "")
        + f"  conflict: {row.get('conflict', 'false')}",
    ]
    if row.get(SUGGESTED_NOTE):
        lines.append(f"suggested note: {row[SUGGESTED_NOTE]}")
    lines.append(
        f"approved: {row.get(APPROVED, 'false')}  hand_verified: {row.get(HAND_VERIFIED, 'false')}  note: {row.get(REVIEWER_NOTE, '') or '(none)'}"
    )
    return "\n".join(lines)


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


PROMPT = "[y] approve  [n] reject  [e] edit  [s] skip  [v] hand-verify  [q] quit"
CHANGE_PROMPT = "[y] apply proposed change  [n] reject it, with a note  [e] edit  [s] skip  [v] hand-verify  [q] quit"
ACK_PROMPT = "[y] accept proposed acknowledgement  [n] reject it, with a note  [e] edit  [s] skip  [v] hand-verify  [q] quit"


# --- reading a key without needing Enter, where the terminal allows it --------


def default_read_key(stream: TextIO = sys.stdin) -> str:
    """One key, no Enter needed, on a real terminal. Falls back to one line (Enter needed) when `stream`
    is not a tty, so a pipe, a redirect, or a test can drive it with plain text."""
    if not stream.isatty():
        line = stream.readline()
        return line[:1].lower() if line else "q"
    import termios
    import tty

    fd = stream.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = stream.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return ch.lower()


# --- the loop --------------------------------------------------------------


def run(
    path: Path,
    *,
    filters: list[str] | None = None,
    read_key: Callable[[], str] | None = None,
    read_line: Callable[[str], str] = input,
    write: Callable[[str], None] = print,
    wrap: Callable[[str], str] | None = None,
) -> None:
    read_key = read_key or default_read_key
    rf = ReviewFile.load(path)
    matches = combine_filters(filters) if filters else None
    indices = [n for n, r in enumerate(rf.rows) if matches is None or matches(r)]
    pos = 0
    while pos < len(indices):
        idx = indices[pos]
        row = rf.rows[idx]
        write(render_row(row, pos + 1, len(indices), wrap))
        write(CHANGE_PROMPT if is_change_pending(row) else ACK_PROMPT if is_ack_pending(row) else PROMPT)
        key = read_key()
        if key in ("y", "n") and is_change_pending(row):
            rf.rows[idx] = accept_change(row) if key == "y" else reject_change(row, read_line("Reason: ").strip())
            rf.save()
            if not is_ack_pending(rf.rows[idx]):  # an acknowledgement still pending is settled on the same row next
                pos += 1
        elif key in ("y", "n") and is_ack_pending(row):
            rf.rows[idx] = accept_acknowledgement(row) if key == "y" else reject_acknowledgement(row, read_line("Reason: ").strip())
            rf.save()
            pos += 1
        elif key == "y":
            if row.get(EMPTY_BLOCK) == "true":
                write("empty block, use s")
                continue
            if row.get(SUGGESTED_NOTE) and not row.get(REVIEWER_NOTE):
                typed = read_line(f"Note [{row[SUGGESTED_NOTE]}]: ").strip()
                row = edit_field(row, REVIEWER_NOTE, typed or row[SUGGESTED_NOTE])
            elif matches is not None and not row.get(REVIEWER_NOTE):
                row = edit_field(row, REVIEWER_NOTE, read_line("Note required before approving: "))
            rf.rows[idx] = approve(row)
            rf.save()
            if due_for_hand_verify(rf.rows):
                write(f"\n{approved_count(rf.rows)} approvals for {row.get('ticker', 'this company')}. SCOPE 4.3: at least "
                      f"10% of approved rows must be independently hand-verified on EDGAR.")
                write(f"EDGAR: {row.get(EDGAR_URL_COLUMN, '')}")
                if read_line("Hand-verify this row now? [y/N] ").strip().lower().startswith("y"):
                    rf.rows[idx] = hand_verify(rf.rows[idx])
                    rf.save()
            pos += 1
        elif key == "n":
            rf.rows[idx] = reject(row, read_line("Reason: "))
            rf.save()
            pos += 1
        elif key == "e":
            field = read_line("Field to edit: ").strip()
            try:
                value = read_line(f"New value for {field}: ")
                rf.rows[idx] = edit_field(row, field, value)
                rf.save()
            except KeyError as exc:
                write(str(exc))
            # stays on the same row: check the edit before deciding
        elif key == "v":
            rf.rows[idx] = hand_verify(row)
            rf.save()
            # stays on the same row: hand-verifying is not itself a decision
        elif key == "s":
            rf.save()
            pos += 1
        elif key == "q":
            write(f"stopped at row {pos + 1}/{len(indices)}; {path} is up to date.")
            return
        else:
            write(f"unrecognised key {key!r}")
    write(f"done: {len(indices)} rows reviewed.")
