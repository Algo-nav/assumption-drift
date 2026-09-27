"""`rr validate <path>` (SCOPE.md section 5.1): every check a published row must pass.

`path` is either a JSONL file of `ResearchRecord` rows (one per line, `data/release/assumption_drift.jsonl`)
or a review-queue CSV (`data/review/{cik}.csv`): from a CSV, only `approved=true` rows are read, and a
row with `empty_block=true` is never read, since it is a flag, not a record.

Checks, on every row read:
- it parses as `ResearchRecord` (which itself enforces the `source_url` host and the accession number
  format: a row that fails either of those fails to parse, and is reported as a schema issue)
- `target_low` and `target_high` are not both null
- `assumption.stated_at <= assumption.evidence.filed_at`
- when there is an outcome: `outcome.reported_at > assumption.stated_at`
- when there is an acknowledgement: `acknowledged_at >= outcome.reported_at`
- `status` matches what `research_record.rubric.resolve_record` returns from the row's own values
- for every evidence block the row carries (assumption, outcome, acknowledgement): the cached document
  at `data/raw/{cik}/{accession_number}.html` exists, its sha256 matches `content_sha256`, and `excerpt`
  appears verbatim in its extracted text (`research_record.text.html_to_text`)

This module only ever reads `data/raw/`; it never fetches anything, and it does not import `pipeline`
(a standalone install of this package has no `pipeline/` to import): the raw cache's location is a
plain relative path, `data/raw`, resolved from wherever `rr validate` is run.
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from pydantic import ValidationError

from research_record import rubric
from research_record.schema import Evidence, ResearchRecord
from research_record.text import html_to_text

__all__ = ["Issue", "DEFAULT_RAW_DIR", "validate_path", "validate_row", "unflatten_csv_row"]

#: Relative to the current working directory, the same as `rr validate` is meant to be run: from the
#: repo root. Not `pipeline.common.RAW_DIR`: this package does not depend on `pipeline`.
DEFAULT_RAW_DIR = Path("data/raw")

#: A review CSV's own columns: never part of a `ResearchRecord`, and never read as one.
_REVIEW_ONLY_COLUMNS = frozenset({"approved", "hand_verified", "reviewer_note", "conflict", "empty_block"})


@dataclass(frozen=True)
class Issue:
    """One failed check, always naming the row and which check it was (SCOPE 5.1)."""

    record_id: str
    check: str
    detail: str

    def __str__(self) -> str:
        return f"{self.record_id}: {self.check}: {self.detail}"


# --- reading rows, whatever the file is ---------------------------------------


def unflatten_csv_row(row: dict[str, str]) -> dict[str, Any]:
    """A flat review-CSV row (dotted column names, every value a string) back into the nested shape
    `ResearchRecord` accepts. The review-only columns (`approved`, `aid_*`, ...) are dropped; an empty
    string becomes null; a nested object that is null all the way down collapses to null itself, so an
    absent outcome round-trips as `None` rather than `{"reported_value": None, ...}`."""
    tree: dict[str, Any] = {}
    for column, text in row.items():
        if column in _REVIEW_ONLY_COLUMNS or column.startswith("aid_"):
            continue
        node = tree
        *path, leaf = column.split(".")
        for key in path:
            node = node.setdefault(key, {})
        node[leaf] = None if text == "" else text

    def collapse(node: Any) -> Any:
        if isinstance(node, dict):
            node = {k: collapse(v) for k, v in node.items()}
            return None if all(v is None for v in node.values()) else node
        return node

    return {k: collapse(v) for k, v in tree.items()}


def _rows_from_jsonl(path: Path) -> Iterator[tuple[str, dict[str, Any] | None]]:
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            yield f"line {n}", {"__json_error__": str(exc)}
            continue
        identifier = payload.get("record_id") if isinstance(payload, dict) else None
        yield identifier or f"line {n}", payload


def _rows_from_csv(path: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("empty_block") == "true" or row.get("approved") != "true":
                continue
            yield row.get("record_id") or "(no record_id)", unflatten_csv_row(row)


def _load_rows(path: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    if not path.exists():
        raise FileNotFoundError(f"{path}: no such file")
    if path.suffix == ".csv":
        yield from _rows_from_csv(path)
    elif path.suffix in (".jsonl", ".ndjson"):
        yield from _rows_from_jsonl(path)
    else:
        raise ValueError(f"{path}: unsupported file type (expected .jsonl or a review-queue .csv)")


# --- the checks on one row -----------------------------------------------------


def _evidences(record: ResearchRecord) -> Iterator[tuple[str, Evidence]]:
    yield "assumption.evidence", record.assumption.evidence
    if record.outcome is not None:
        yield "outcome.evidence", record.outcome.evidence
    if record.acknowledgement_evidence is not None:
        yield "acknowledgement_evidence", record.acknowledgement_evidence


def _check_evidence(record_id: str, label: str, cik: str, evidence: Evidence, raw_dir: Path) -> list[Issue]:
    html_path = raw_dir / cik / f"{evidence.accession_number}.html"
    if not html_path.exists():
        return [Issue(record_id, f"{label}.cached_document", f"no cached document at {html_path}")]
    content = html_path.read_bytes()
    issues = []
    if hashlib.sha256(content).hexdigest() != evidence.content_sha256:
        issues.append(Issue(record_id, f"{label}.content_sha256", f"cached document at {html_path} does not match content_sha256"))
    if evidence.excerpt not in html_to_text(content):
        issues.append(Issue(record_id, f"{label}.excerpt", f"excerpt does not appear verbatim in {html_path}"))
    return issues


def validate_row(raw: dict[str, Any], *, raw_dir: Path = DEFAULT_RAW_DIR, identifier: str = "(unknown)") -> list[Issue]:
    """Every check for one already-parsed-JSON row. `identifier` is used only if the row fails to parse
    as a `ResearchRecord` at all; once it parses, its own `record_id` is used instead."""
    if "__json_error__" in raw:
        return [Issue(identifier, "schema", f"not valid JSON: {raw['__json_error__']}")]
    try:
        record = ResearchRecord.model_validate(raw)
    except ValidationError as exc:
        return [Issue(identifier, "schema", str(exc).replace("\n", " "))]

    rid = record.record_id
    issues: list[Issue] = []
    a = record.assumption

    if a.target_low is None and a.target_high is None:
        issues.append(Issue(rid, "target_range", "target_low and target_high are both null"))

    if a.stated_at > a.evidence.filed_at:
        issues.append(Issue(rid, "date_order", f"assumption.stated_at ({a.stated_at}) is after assumption.evidence.filed_at ({a.evidence.filed_at})"))

    if record.outcome is not None:
        if record.outcome.reported_at <= a.stated_at:
            issues.append(Issue(rid, "date_order", f"outcome.reported_at ({record.outcome.reported_at}) is not after assumption.stated_at ({a.stated_at})"))
        if record.acknowledged_at is not None and record.acknowledged_at < record.outcome.reported_at:
            issues.append(Issue(rid, "date_order", f"acknowledged_at ({record.acknowledged_at}) is before outcome.reported_at ({record.outcome.reported_at})"))

    expected = rubric.resolve_record(record)
    if expected != record.status:
        issues.append(Issue(rid, "status", f"stored status {record.status!r} does not match what the rubric gives from the values, {expected!r}"))

    for label, evidence in _evidences(record):
        issues.extend(_check_evidence(rid, label, record.cik, evidence, raw_dir))

    return issues


# --- the whole file -------------------------------------------------------------


def validate_path(path: Path, *, raw_dir: Path = DEFAULT_RAW_DIR) -> tuple[list[Issue], int]:
    """(issues, rows checked) for every row `path` contains, per the rules at the top of this module."""
    issues: list[Issue] = []
    checked = 0
    for identifier, raw in _load_rows(path):
        issues.extend(validate_row(raw, raw_dir=raw_dir, identifier=identifier))
        checked += 1
    return issues, checked
