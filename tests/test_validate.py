"""research_record/validate.py: `rr validate` (SCOPE.md 5.1)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from research_record import validate
from research_record.schema import ResearchRecord

CIK = "0000000123"
ACCESSION = "0000000123-24-000001"
DOC_TEXT = "The Company expects full year 2024 revenue of $5.0 billion to $6.0 billion."
DOC_SHA = hashlib.sha256(DOC_TEXT.encode()).hexdigest()


@pytest.fixture
def raw_dir(tmp_path):
    d = tmp_path / "raw" / CIK
    d.mkdir(parents=True)
    (d / f"{ACCESSION}.html").write_text(DOC_TEXT, encoding="utf-8")
    return tmp_path / "raw"


def good_record(record_data) -> dict:
    data = record_data()
    data["assumption"]["evidence"]["content_sha256"] = DOC_SHA
    data["assumption"]["evidence"]["accession_number"] = ACCESSION
    data["outcome"] = None
    data["status"] = "unresolved"
    data["days_to_falsifiable"] = None
    return data


# --- unflatten_csv_row -----------------------------------------------------------


def test_unflatten_builds_nested_structure_from_dotted_columns() -> None:
    row = {"record_id": "R1", "assumption.metric": "revenue", "assumption.target_low": "5000.0", "assumption.target_high": "6000.0"}
    tree = validate.unflatten_csv_row(row)
    assert tree == {"record_id": "R1", "assumption": {"metric": "revenue", "target_low": "5000.0", "target_high": "6000.0"}}


def test_unflatten_turns_an_empty_string_into_null() -> None:
    assert validate.unflatten_csv_row({"acknowledged_at": ""}) == {"acknowledged_at": None}


def test_unflatten_collapses_an_all_null_nested_object_to_null() -> None:
    row = {"outcome.reported_value": "", "outcome.reported_at": "", "outcome.evidence.source_url": ""}
    assert validate.unflatten_csv_row(row) == {"outcome": None}


def test_unflatten_drops_review_only_and_aid_columns() -> None:
    row = {"record_id": "R1", "approved": "true", "hand_verified": "false", "reviewer_note": "x",
           "conflict": "false", "empty_block": "false", "aid_proposed_status": "missed", "aid_verify": "yes"}
    assert validate.unflatten_csv_row(row) == {"record_id": "R1"}


def test_unflatten_round_trips_a_full_row_into_a_valid_research_record(record_data) -> None:
    """The same shape 05_review.py's build_row() writes to a review CSV, flattened by hand here, must load
    back as the exact record it started from."""
    record = ResearchRecord(**record_data())
    flat: dict[str, str] = {}

    def walk(value, prefix=""):
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, f"{prefix}{k}.")
        else:
            flat[prefix[:-1]] = "" if value is None else str(value)

    walk(record.model_dump(mode="json"))
    flat.update(approved="true", hand_verified="false", reviewer_note="", conflict="false", empty_block="false", aid_verify="yes")
    rebuilt = ResearchRecord(**validate.unflatten_csv_row(flat))
    assert rebuilt == record


# --- validate_row: schema ----------------------------------------------------------


def test_validate_row_reports_a_json_error(record_data) -> None:
    issues = validate.validate_row({"__json_error__": "Expecting value"}, identifier="line 3")
    assert len(issues) == 1 and issues[0].record_id == "line 3" and issues[0].check == "schema"


def test_validate_row_reports_a_schema_failure_with_the_given_identifier(record_data) -> None:
    bad = {**record_data(), "status": "not-a-status"}
    issues = validate.validate_row(bad, identifier=bad["record_id"])
    assert len(issues) == 1 and issues[0].check == "schema" and issues[0].record_id == bad["record_id"]


def test_validate_row_of_a_fully_valid_row_has_no_issues(record_data, raw_dir) -> None:
    assert validate.validate_row(good_record(record_data), raw_dir=raw_dir) == []


# --- both targets null -----------------------------------------------------------


def test_validate_row_fails_when_both_targets_are_null(record_data, raw_dir) -> None:
    data = good_record(record_data)
    data["assumption"]["target_low"] = None
    data["assumption"]["target_high"] = None
    issues = validate.validate_row(data, raw_dir=raw_dir)
    assert any(i.check == "target_range" for i in issues)


def test_validate_row_is_fine_with_one_sided_targets(record_data, raw_dir) -> None:
    data = good_record(record_data)
    data["assumption"]["target_high"] = None
    assert not any(i.check == "target_range" for i in validate.validate_row(data, raw_dir=raw_dir))


# --- date ordering -----------------------------------------------------------------


def test_validate_row_fails_when_stated_at_is_after_filed_at(record_data, raw_dir) -> None:
    data = good_record(record_data)
    data["assumption"]["stated_at"] = "2024-06-01"  # evidence.filed_at is 2024-02-01
    issues = validate.validate_row(data, raw_dir=raw_dir)
    assert any(i.check == "date_order" and "stated_at" in i.detail for i in issues)


def test_validate_row_fails_when_reported_at_is_not_after_stated_at(record_data, raw_dir) -> None:
    data = good_record(record_data)
    data["outcome"] = {**record_data()["outcome"], "reported_at": "2024-02-01"}  # same as stated_at
    data["status"] = "missed"
    data["days_to_falsifiable"] = 0
    issues = validate.validate_row(data, raw_dir=raw_dir)
    assert any(i.check == "date_order" and "reported_at" in i.detail for i in issues)


def test_validate_row_fails_when_acknowledged_before_reported(record_data, raw_dir) -> None:
    data = good_record(record_data)
    data["outcome"] = record_data()["outcome"]
    data["status"] = "missed"
    data["acknowledged_at"] = "2025-01-01"  # before outcome.reported_at, 2025-02-03
    data["acknowledgement_evidence"] = record_data()["outcome"]["evidence"]
    issues = validate.validate_row(data, raw_dir=raw_dir)
    assert any(i.check == "date_order" and "acknowledged_at" in i.detail for i in issues)


# --- status must match the rubric --------------------------------------------------


def test_validate_row_fails_when_status_does_not_match_the_rubric(record_data, raw_dir) -> None:
    data = good_record(record_data)
    data["outcome"] = {**record_data()["outcome"], "reported_value": 5_500.0}  # inside [5000, 6000]: should be "met"
    data["status"] = "missed"  # wrong on purpose
    issues = validate.validate_row(data, raw_dir=raw_dir)
    assert any(i.check == "status" for i in issues)


def test_validate_row_passes_when_status_matches_the_rubric(record_data, raw_dir) -> None:
    data = good_record(record_data)
    data["outcome"] = {**record_data()["outcome"], "reported_value": 5_500.0}
    data["status"] = "met"
    assert not any(i.check == "status" for i in validate.validate_row(data, raw_dir=raw_dir))


# --- cached evidence: existence, hash, excerpt ----------------------------------


def test_validate_row_fails_when_the_cached_document_is_missing(record_data, tmp_path) -> None:
    issues = validate.validate_row(good_record(record_data), raw_dir=tmp_path / "nowhere")
    assert any(i.check == "assumption.evidence.cached_document" for i in issues)


def test_validate_row_fails_when_the_hash_does_not_match(record_data, raw_dir) -> None:
    data = good_record(record_data)
    data["assumption"]["evidence"]["content_sha256"] = "f" * 64
    issues = validate.validate_row(data, raw_dir=raw_dir)
    assert any(i.check == "assumption.evidence.content_sha256" for i in issues)


def test_validate_row_fails_when_the_excerpt_is_not_in_the_document(record_data, raw_dir) -> None:
    data = good_record(record_data)
    data["assumption"]["evidence"]["excerpt"] = "this sentence is not in the cached document"
    issues = validate.validate_row(data, raw_dir=raw_dir)
    assert any(i.check == "assumption.evidence.excerpt" for i in issues)


def test_validate_row_also_checks_outcome_and_acknowledgement_evidence(record_data, tmp_path) -> None:
    raw = tmp_path / "raw"
    (raw / CIK).mkdir(parents=True)
    (raw / CIK / f"{ACCESSION}.html").write_text(DOC_TEXT, encoding="utf-8")
    data = good_record(record_data)
    data["outcome"] = {
        "reported_value": 4_800.0, "reported_at": "2025-02-03",
        "evidence": {**record_data()["outcome"]["evidence"], "accession_number": "0000000999-25-000001", "content_sha256": "e" * 64},
    }
    data["status"] = "missed"
    issues = validate.validate_row(data, raw_dir=raw)
    assert any(i.check == "outcome.evidence.cached_document" for i in issues)


# --- validate_path: jsonl -----------------------------------------------------------


def test_validate_path_jsonl_all_clean(tmp_path, record_data, raw_dir) -> None:
    path = tmp_path / "release.jsonl"
    path.write_text(json.dumps(good_record(record_data), default=str) + "\n", encoding="utf-8")
    issues, checked = validate.validate_path(path, raw_dir=raw_dir)
    assert issues == [] and checked == 1


def test_validate_path_jsonl_skips_blank_lines(tmp_path, record_data, raw_dir) -> None:
    path = tmp_path / "release.jsonl"
    path.write_text("\n" + json.dumps(good_record(record_data), default=str) + "\n\n", encoding="utf-8")
    _, checked = validate.validate_path(path, raw_dir=raw_dir)
    assert checked == 1


def test_validate_path_jsonl_reports_malformed_json_with_its_line_number(tmp_path) -> None:
    path = tmp_path / "release.jsonl"
    path.write_text("not json\n", encoding="utf-8")
    issues, checked = validate.validate_path(path)
    assert checked == 1 and issues[0].record_id == "line 1" and issues[0].check == "schema"


def test_validate_path_of_a_missing_file_raises(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        validate.validate_path(tmp_path / "nope.jsonl")


def test_validate_path_of_an_unsupported_extension_raises(tmp_path) -> None:
    path = tmp_path / "release.txt"
    path.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported"):
        validate.validate_path(path)


# --- validate_path: review-queue csv ---------------------------------------------


def _csv_row(record_data, **overrides) -> dict[str, str]:
    record = good_record(record_data)
    flat: dict[str, str] = {}

    def walk(value, prefix=""):
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, f"{prefix}{k}.")
        else:
            flat[prefix[:-1]] = "" if value is None else str(value)

    walk(record)
    flat.update(approved="true", hand_verified="false", reviewer_note="", conflict="false", empty_block="false",
                aid_verify="yes", aid_verify_reason="", aid_verify_class="")
    flat.update(overrides)
    return flat


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    import csv as csv_module

    fieldnames = sorted({k for row in rows for k in row})
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv_module.DictWriter(fh, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


def test_validate_path_csv_only_reads_approved_rows(tmp_path, record_data, raw_dir) -> None:
    approved = _csv_row(record_data, approved="true")
    unapproved = _csv_row(record_data, record_id="01HZY8Q9XMR3T7VBN2CDEFGH2K", approved="false")
    path = tmp_path / "review.csv"
    write_csv(path, [approved, unapproved])
    issues, checked = validate.validate_path(path, raw_dir=raw_dir)
    assert checked == 1 and issues == []


def test_validate_path_csv_never_reads_empty_block_rows(tmp_path, record_data) -> None:
    flag = _csv_row(record_data, approved="true", empty_block="true")
    path = tmp_path / "review.csv"
    write_csv(path, [flag])
    issues, checked = validate.validate_path(path)
    assert checked == 0 and issues == []


def test_validate_path_csv_uses_record_id_as_the_identifier(tmp_path, record_data) -> None:
    bad = _csv_row(record_data, approved="true")
    bad["assumption.target_low"] = ""  # both targets now null: still parses, fails a value check
    bad["assumption.target_high"] = ""
    path = tmp_path / "review.csv"
    write_csv(path, [bad])
    issues, _ = validate.validate_path(path, raw_dir=tmp_path / "raw")
    ids = {i.record_id for i in issues}
    assert bad["record_id"] in ids
