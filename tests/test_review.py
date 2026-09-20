"""05_review.py: the review queue CSVs."""

from __future__ import annotations

import copy
import csv
import importlib
import json
from datetime import date

import pytest

from research_record.schema import ResearchRecord

review = importlib.import_module("pipeline.05_review")
common = importlib.import_module("pipeline.common")

COMPANY = common.Company("Example Corporation", "EXMP", "0000000123")
TODAY = date(2026, 9, 20)


@pytest.fixture
def parts(record_data):
    """A valid draft, its outcome (a miss: 4800 against 5000 to 6000), and an acknowledgement."""
    record = ResearchRecord(**record_data())
    assumption = record.assumption.model_dump(mode="json")
    draft = {"draft_id": record.record_id, "cik": COMPANY.cik, "ticker": "EXMP", "company": COMPANY.name,
             "capture_method": "section", "heading": "Q4 FY26 Guidance", "lead_in": "Our outlook is as follows:",
             "assumption": assumption}
    outcome = record.outcome.model_dump(mode="json")
    ack = {"acknowledged_at": "2025-05-01", "evidence": {**outcome["evidence"], "filed_at": "2025-05-01", "excerpt": "Below our guidance."}}
    return draft, {"draft_id": record.record_id, "outcome": outcome, "acknowledgement": None, "outcome_reason": None}, ack


# --- columns ---------------------------------------------------------------


def test_the_columns_follow_the_schema_and_then_the_reviewers() -> None:
    cols = review.COLUMNS
    assert len(cols) == len(set(cols)) == 50
    for needed in ["record_id", "claim", "assumption.metric", "assumption.evidence.content_sha256", "outcome.reported_value",
                   "outcome.evidence.excerpt", "acknowledged_at", "acknowledgement_evidence.source_url", "reviewer"]:
        assert needed in cols
    assert cols[-8:] == ["approved", "hand_verified", "reviewer_note", "aid_proposed_status", "aid_capture_method",
                         "aid_heading", "aid_lead_in", "aid_outcome_note"]
    assert set(review.schema_columns()) == set(cols[:-8])


# --- the words code writes -------------------------------------------------


@pytest.mark.parametrize(
    "low, high, unit, expected",
    [
        (5.0, 6.0, "USD billions", "$5 billion to $6 billion"),
        (105.84, 110.16, "USD billions", "$105.84 billion to $110.16 billion"),
        (74.8, 74.8, "percent", "74.8%"),
        (7.0, 8.0, "USD per share", "$7 per share to $8 per share"),
        (5000.0, None, "USD millions", "at least $5000 million"),
        (None, 6.0, "USD billions", "no more than $6 billion"),
        (2.0, 3.0, "units", "2 units to 3 units"),
    ],
)
def test_the_range_is_put_into_words(low, high, unit, expected) -> None:
    assert review.range_words({"target_low": low, "target_high": high, "unit": unit}) == expected


def test_the_claim_and_the_invalidation_condition_are_restatements_not_judgments() -> None:
    a = {"metric": "revenue", "target_low": 5.0, "target_high": 6.0, "unit": "USD billions", "target_period": "Q4 FY2026"}
    assert review.restate("NVIDIA Corporation", a) == "NVIDIA Corporation expects revenue of $5 billion to $6 billion for Q4 FY2026."
    assert review.invalidation(a) == "reported value falls outside [5, 6]"
    assert review.invalidation({**a, "target_low": 5.0, "target_high": 5.0}) == "reported value differs from 5 by more than 0.5%"
    assert review.invalidation({**a, "target_high": None}) == "reported value is below 5"
    assert review.invalidation({**a, "target_low": None}) == "reported value is above 6"


# --- a row -----------------------------------------------------------------


def test_a_draft_row_is_open_unapproved_and_carries_the_days_and_the_proposed_status(parts) -> None:
    draft, outcome_row, _ = parts
    row = review.build_row(draft, outcome_row, TODAY)
    assert (row["status"], row["approved"], row["hand_verified"], row["reviewer_note"]) == ("open", "false", "false", "")
    assert (row["reviewer"], row["last_reviewed_at"]) == ("navneet", "2026-09-20")
    assert (row["days_to_falsifiable"], row["days_to_acknowledged"], row["acknowledged_at"]) == ("368", "", "")
    assert row["aid_proposed_status"] == "missed"  # 4800 is outside 5000 to 6000
    assert (row["aid_capture_method"], row["aid_heading"], row["aid_lead_in"]) == ("section", "Q4 FY26 Guidance", "Our outlook is as follows:")
    assert row["outcome.evidence.excerpt"] == "Full year 2024 revenue was $4.8 billion."


def test_an_acknowledged_miss_carries_its_date_evidence_and_days(parts) -> None:
    draft, outcome_row, ack = parts
    row = review.build_row(draft, {**outcome_row, "acknowledgement": ack}, TODAY)
    assert (row["acknowledged_at"], row["days_to_acknowledged"]) == ("2025-05-01", "87")
    assert row["acknowledgement_evidence.excerpt"] == "Below our guidance."


def test_a_draft_with_no_outcome_is_unresolved_and_says_why(parts) -> None:
    draft, _, _ = parts
    row = review.build_row(draft, {"draft_id": draft["draft_id"], "outcome": None, "outcome_reason": "no later 8-K release for that period found"}, TODAY)
    assert row["outcome.reported_value"] == "" and row["days_to_falsifiable"] == ""
    assert row["aid_proposed_status"] == "unresolved"
    assert row["aid_outcome_note"] == "no later 8-K release for that period found"
    assert review.build_row(draft, None, TODAY)["aid_outcome_note"] == "outcome search not run"


def test_a_met_row_is_proposed_as_met(parts) -> None:
    draft, outcome_row, _ = parts
    outcome_row = copy.deepcopy(outcome_row)
    outcome_row["outcome"]["reported_value"] = 5500.0
    assert review.build_row(draft, outcome_row, TODAY)["aid_proposed_status"] == "met"


def test_every_row_has_exactly_the_columns_whatever_is_null(parts) -> None:
    draft, outcome_row, ack = parts
    for outcome in (outcome_row, {**outcome_row, "acknowledgement": ack}, {"draft_id": draft["draft_id"], "outcome": None}, None):
        assert set(review.build_row(draft, outcome, TODAY)) == set(review.COLUMNS)


def unflatten(row: dict[str, str]) -> dict:
    tree: dict = {}
    for column, text in row.items():
        if column in review.REVIEWER_COLUMNS or column.startswith("aid_"):
            continue
        node = tree
        *path, leaf = column.split(".")
        for key in path:
            node = node.setdefault(key, {})
        node[leaf] = None if text == "" else text

    def collapse(node):
        if isinstance(node, dict):
            node = {k: collapse(v) for k, v in node.items()}
            return None if all(v is None for v in node.values()) else node
        return node

    return {k: collapse(v) for k, v in tree.items()}


def test_every_row_loads_back_as_a_valid_research_record(parts) -> None:
    draft, outcome_row, ack = parts
    for outcome in (outcome_row, {**outcome_row, "acknowledgement": ack}, None):
        row = review.build_row(draft, outcome, TODAY)
        record = ResearchRecord(**unflatten(row))
        assert record.status == "open" and record.record_id == draft["draft_id"]
        assert (record.outcome is None) == (not row["outcome.reported_value"])


# --- the files -------------------------------------------------------------


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    for name in ("DRAFTS_DIR", "OUTCOMES_DIR", "REVIEW_DIR"):
        monkeypatch.setattr(review, name, tmp_path / name.split("_")[0].lower())
    (tmp_path / "drafts").mkdir()
    (tmp_path / "outcomes").mkdir()
    return tmp_path


def seed(dirs, drafts, outcome_rows=()):
    (dirs / "drafts" / f"{COMPANY.cik}.jsonl").write_text("".join(json.dumps(d) + "\n" for d in drafts))
    (dirs / "outcomes" / f"{COMPANY.cik}.jsonl").write_text("".join(json.dumps(o) + "\n" for o in outcome_rows))


def another(draft, suffix):
    d = copy.deepcopy(draft)
    d["draft_id"] = draft["draft_id"][:-1] + suffix
    return d


def test_rows_are_written_once_and_a_rerun_appends_only_new_drafts_leaving_hand_edits_alone(dirs, parts) -> None:
    draft, outcome_row, _ = parts
    seed(dirs, [draft], [outcome_row])
    rows, stats = review.review_company(COMPANY, TODAY)
    path = dirs / "review" / f"{COMPANY.cik}.csv"
    assert (stats["added"], stats["kept"], len(rows)) == (1, 0, 1) and path.exists()

    # Navneet approves the row and edits a field by hand.
    edited = review.read_csv(path)
    edited[0].update(approved="true", reviewer_note="checked on EDGAR", **{"assumption.target_high": "6100.0"})
    review.write_csv(path, edited)

    # The pipeline runs again, with one more draft.
    seed(dirs, [draft, another(draft, "Z")], [outcome_row])
    rows, stats = review.review_company(COMPANY, TODAY)
    assert (stats["added"], stats["kept"], len(rows)) == (1, 1, 2)
    kept = review.read_csv(path)
    assert (kept[0]["approved"], kept[0]["reviewer_note"], kept[0]["assumption.target_high"]) == ("true", "checked on EDGAR", "6100.0")
    assert kept[1]["approved"] == "false"


def test_nothing_is_written_when_there_are_no_drafts(dirs) -> None:
    seed(dirs, [])
    rows, stats = review.review_company(COMPANY, TODAY)
    assert rows == [] and not (dirs / "review").exists()


def test_a_rerun_with_nothing_new_leaves_the_file_untouched(dirs, parts) -> None:
    draft, outcome_row, _ = parts
    seed(dirs, [draft], [outcome_row])
    review.review_company(COMPANY, TODAY)
    path = dirs / "review" / f"{COMPANY.cik}.csv"
    before = path.read_bytes()
    _, stats = review.review_company(COMPANY, date(2030, 1, 1))  # a different "today" must not rewrite old rows
    assert stats["added"] == 0 and path.read_bytes() == before


def test_a_draft_that_is_not_a_valid_record_is_left_out_and_counted(dirs, parts, capsys) -> None:
    draft, outcome_row, _ = parts
    broken = another(draft, "Y")
    broken["assumption"] = {**broken["assumption"], "unit": ""}
    seed(dirs, [draft, broken], [outcome_row])
    rows, stats = review.review_company(COMPANY, TODAY)
    assert (stats["added"], stats["invalid"]) == (1, 1) and len(rows) == 1
    assert "not a valid record" in capsys.readouterr().err


def test_the_csv_is_plain_utf8_with_one_header_row(dirs, parts) -> None:
    draft, outcome_row, _ = parts
    seed(dirs, [draft], [outcome_row])
    review.review_company(COMPANY, TODAY)
    text = (dirs / "review" / f"{COMPANY.cik}.csv").read_text(encoding="utf-8")
    assert not text.startswith("\ufeff") and text.splitlines()[0].split(",")[0] == "record_id" and "\r" not in text
    assert len(list(csv.reader(text.splitlines()))) == 2
