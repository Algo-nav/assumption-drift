"""05_review.py: the review queue CSVs."""

from __future__ import annotations

import copy
import csv
import hashlib
import importlib
import json
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

from research_record.schema import ResearchRecord
from research_record.text import html_to_text

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
    assert len(cols) == len(set(cols)) == 65
    for needed in ["record_id", "claim", "assumption.metric", "assumption.evidence.content_sha256", "outcome.reported_value",
                   "outcome.evidence.excerpt", "acknowledged_at", "acknowledgement_evidence.source_url", "reviewer"]:
        assert needed in cols
    assert cols[-23:] == ["approved", "hand_verified", "reviewer_note", "conflict", "empty_block", "ack_pending_review", "aid_proposed_status",
                          "aid_capture_method", "aid_heading", "aid_lead_in", "aid_table_header", "aid_value_column", "aid_outcome_note", "aid_flag_note",
                          "aid_withdrawal_note", "aid_verify", "aid_verify_reason", "aid_verify_class", "aid_suggested_note", "aid_ack_proposed_at", "aid_ack_proposed_excerpt",
                          "aid_ack_proposed_url", "aid_ack_proposed_evidence"]
    assert set(review.schema_columns()) == set(cols[:-23])


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
        if column in review.REVIEWER_COLUMNS or column in review.FLAG_COLUMNS or column.startswith("aid_"):
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
    for name in ("DRAFTS_DIR", "OUTCOMES_DIR", "REVIEW_DIR", "RAW_DIR"):
        monkeypatch.setattr(review, name, tmp_path / name.split("_")[0].lower())
    (tmp_path / "drafts").mkdir()
    (tmp_path / "outcomes").mkdir()
    (tmp_path / "raw").mkdir()
    return tmp_path


def seed(dirs, drafts, outcome_rows=()):
    (dirs / "drafts" / f"{COMPANY.cik}.jsonl").write_text("".join(json.dumps(d) + "\n" for d in drafts))
    (dirs / "outcomes" / f"{COMPANY.cik}.jsonl").write_text("".join(json.dumps(o) + "\n" for o in outcome_rows))


def write_cached_document(dirs, cik, accession, html_bytes):
    path = dirs / "raw" / cik / f"{accession}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(html_bytes)
    return path


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


# --- conflicts and the table header (second pilot review) -------------------


def test_a_draft_not_in_conflict_says_conflict_false(parts) -> None:
    draft, outcome_row, _ = parts
    assert review.build_row(draft, outcome_row, TODAY)["conflict"] == "false"
    assert review.build_row({**draft, "conflict": False}, outcome_row, TODAY)["conflict"] == "false"


def test_two_conflicting_drafts_are_both_written_to_the_csv_with_conflict_true_and_neither_is_approved(dirs, parts) -> None:
    draft, outcome_row, _ = parts
    other = another(draft, "Z")
    other["assumption"] = {**other["assumption"], "target_low": 7_000.0, "target_high": 8_000.0}  # a different answer for the same key
    seed(dirs, [{**draft, "conflict": True}, {**other, "conflict": True}], [outcome_row])
    rows, stats = review.review_company(COMPANY, TODAY)
    assert stats["added"] == 2
    on_disk = review.read_csv(dirs / "review" / f"{COMPANY.cik}.csv")
    assert [(r["conflict"], r["approved"]) for r in on_disk] == [("true", "false"), ("true", "false")]
    assert {r["assumption.target_low"] for r in on_disk} == {"5000.0", "7000.0"}
    assert len({(r["assumption.metric"], r["assumption.target_period"], r["assumption.stated_at"]) for r in on_disk}) == 1  # same key


def test_the_conflict_mark_survives_a_rerun_and_a_hand_edit(dirs, parts) -> None:
    draft, outcome_row, _ = parts
    seed(dirs, [{**draft, "conflict": True}], [outcome_row])
    review.review_company(COMPANY, TODAY)
    path = dirs / "review" / f"{COMPANY.cik}.csv"
    edited = review.read_csv(path)
    edited[0].update(approved="true", reviewer_note="this one is right; the other is the wrong column")
    review.write_csv(path, edited)
    review.review_company(COMPANY, TODAY)  # the same draft again, now saying conflict false: the reviewer's row is not touched
    assert [(r["conflict"], r["approved"]) for r in review.read_csv(path)] == [("true", "true")]


def test_the_table_header_row_is_shown_as_an_aid_and_is_not_a_schema_column(parts) -> None:
    draft, outcome_row, _ = parts
    assert review.build_row({**draft, "table_header": "Q2 2023 Full Year 2023"}, outcome_row, TODAY)["aid_table_header"] == "Q2 2023 Full Year 2023"
    assert review.build_row(draft, outcome_row, TODAY)["aid_table_header"] == ""
    assert "conflict" not in review.schema_columns() and "aid_table_header" not in review.schema_columns()
    assert "conflict" not in ResearchRecord.model_fields  # never a field of the record itself


def test_a_parens_sign_correction_is_shown_as_the_flag_note_and_blank_when_there_is_none(parts) -> None:
    draft, outcome_row, _ = parts
    note = "'tax rate' is a rate: a parenthesised figure in the evidence was read as positive, not negative"
    assert review.build_row({**draft, "parens_note": note}, outcome_row, TODAY)["aid_flag_note"] == note
    assert review.build_row(draft, outcome_row, TODAY)["aid_flag_note"] == ""
    assert "aid_flag_note" not in review.schema_columns()


# --- outlook blocks that produced no draft (third pilot review) --------------------------------


def empty_block(block_id="01HZY8Q9XMR3T7VBN2CDEFGHJA", reason="the model returned no items", evidence=True):
    return {"block_id": block_id, "custom_id": "b-1", "cik": COMPANY.cik, "ticker": "EXMP", "company": COMPANY.name,
            "accession": "0000000123-24-000001", "filed_at": "2024-02-01", "char_start": 100, "heading": "Q1 FY27 Guidance",
            "lead_in": "Our outlook is as follows:", "table_header": "Q1 2027 | Full Year 2027", "block_lines": 4, "reason": reason,
            "evidence": {"source_url": "https://www.sec.gov/Archives/edgar/data/123/000000012324000001/x.htm", "accession_number": "0000000123-24-000001",
                         "filing_type": "8-K", "filed_at": "2024-02-01", "fetched_at": "2026-09-20T12:00:00Z", "content_sha256": "a" * 64,
                         "excerpt": "Tax rate\n17.0%"} if evidence else None}


def seed_blocks(dirs, blocks):
    (dirs / "drafts" / f"{COMPANY.cik}.empty_blocks.jsonl").write_text("".join(json.dumps(b) + "\n" for b in blocks))


def test_an_outlook_block_with_no_draft_is_flagged_in_the_csv_as_a_row_of_its_own(dirs, parts) -> None:
    draft, outcome_row, _ = parts
    seed(dirs, [draft], [outcome_row])
    seed_blocks(dirs, [empty_block()])
    rows, stats = review.review_company(COMPANY, TODAY)
    assert (stats["added"], stats["flagged"], len(rows)) == (1, 1, 2)
    on_disk = review.read_csv(dirs / "review" / f"{COMPANY.cik}.csv")
    assert [r["empty_block"] for r in on_disk] == ["false", "true"] and [r["conflict"] for r in on_disk] == ["false", "false"]
    flag = on_disk[1]
    assert (flag["record_id"], flag["company"], flag["ticker"], flag["cik"], flag["status"]) == ("01HZY8Q9XMR3T7VBN2CDEFGHJA", COMPANY.name, "EXMP", COMPANY.cik, "open")
    assert (flag["assumption.stated_at"], flag["assumption.evidence.accession_number"], flag["assumption.evidence.filing_type"]) == ("2024-02-01", "0000000123-24-000001", "8-K")
    assert flag["assumption.evidence.source_url"].startswith("https://www.sec.gov/") and flag["assumption.evidence.excerpt"] == "Tax rate\n17.0%"
    assert (flag["aid_capture_method"], flag["aid_heading"], flag["aid_lead_in"], flag["aid_table_header"]) == ("section", "Q1 FY27 Guidance", "Our outlook is as follows:", "Q1 2027 | Full Year 2027")
    assert flag["aid_flag_note"] == "the model returned no items" and (flag["approved"], flag["hand_verified"], flag["reviewer_note"]) == ("false", "false", "")


def test_a_flag_row_leaves_blank_everything_a_record_needs() -> None:
    flag = review.build_empty_block_row(empty_block())
    blank = ["claim", "assumption.text", "assumption.metric", "assumption.target_low", "assumption.target_high", "assumption.unit", "assumption.target_period",
             "outcome.reported_value", "outcome.reported_at", "invalidation_condition", "acknowledged_at", "days_to_falsifiable", "last_reviewed_at", "reviewer",
             "aid_proposed_status", "aid_outcome_note"]
    assert all(flag[c] == "" for c in blank) and set(flag) == set(review.COLUMNS)


def test_a_flag_row_can_never_be_published_as_a_record_whatever_is_typed_in_approved() -> None:
    flag = {**review.build_empty_block_row(empty_block()), "approved": "true"}
    with pytest.raises(Exception) as caught:  # pydantic's ValidationError
        ResearchRecord(**unflatten(flag))
    assert "metric" in str(caught.value) and "target_period" in str(caught.value)


def test_a_flag_without_evidence_is_still_written_with_its_source_cells_blank() -> None:
    flag = review.build_empty_block_row(empty_block(evidence=False))
    assert flag["empty_block"] == "true" and flag["assumption.evidence.source_url"] == "" and flag["assumption.stated_at"] == "2024-02-01"


def test_a_rerun_does_not_flag_the_same_block_twice_and_leaves_a_hand_edited_flag_alone(dirs, parts) -> None:
    draft, outcome_row, _ = parts
    seed(dirs, [draft], [outcome_row])
    seed_blocks(dirs, [empty_block()])
    review.review_company(COMPANY, TODAY)
    path = dirs / "review" / f"{COMPANY.cik}.csv"
    edited = review.read_csv(path)
    edited[1].update(reviewer_note="looked: prose about margins, no figure to guide to", aid_flag_note="edited")
    review.write_csv(path, edited)
    seed_blocks(dirs, [empty_block(), empty_block("01HZY8Q9XMR3T7VBN2CDEFGHJB", "1 item(s) returned, all rejected: x")])
    rows, stats = review.review_company(COMPANY, TODAY)
    assert (stats["added"], stats["flagged"], len(rows)) == (0, 1, 3)  # only the new block is added
    kept = review.read_csv(path)
    assert (kept[1]["reviewer_note"], kept[1]["aid_flag_note"]) == ("looked: prose about margins, no figure to guide to", "edited")
    assert kept[2]["aid_flag_note"].startswith("1 item(s) returned") and [r["empty_block"] for r in kept] == ["false", "true", "true"]


def test_flags_alone_still_make_a_queue_file_and_no_flags_and_no_drafts_make_none(dirs) -> None:
    seed(dirs, [])
    review.review_company(COMPANY, TODAY)
    assert not (dirs / "review").exists()
    seed_blocks(dirs, [empty_block()])
    rows, _ = review.review_company(COMPANY, TODAY)
    assert len(rows) == 1 and (dirs / "review" / f"{COMPANY.cik}.csv").exists()


def test_the_flag_column_is_not_a_schema_field_and_the_draft_rows_say_false(parts) -> None:
    draft, outcome_row, _ = parts
    assert "empty_block" not in review.schema_columns() and "empty_block" not in ResearchRecord.model_fields
    assert review.build_row(draft, outcome_row, TODAY)["empty_block"] == "false"


def test_the_summary_counts_drafts_by_status_and_flags_apart(dirs, parts, tmp_path, capsys) -> None:
    import yaml

    draft, outcome_row, _ = parts
    seed(dirs, [draft], [outcome_row])
    seed_blocks(dirs, [empty_block()])
    cfg = common.load_config()
    cfg["companies"] = [{"name": COMPANY.name, "ticker": COMPANY.ticker, "cik": COMPANY.cik}]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    assert review.main(["--config", str(path)]) == 0
    out = capsys.readouterr().out
    assert "2 rows (1 added, 0 kept, 0 invalid; 1 empty-block flags, 1 new)" in out
    assert "status:   {'open': 1}" in out and "proposed: {'missed': 1}" in out  # the flag is in neither count


# --- withdrawn: proposed from what 04_outcomes found -----------------------------------------------------------


def withdrawn(outcome_row, withdrawn_at="2024-03-01", period_close="2024-12-31"):
    evidence = {**outcome_row["outcome"]["evidence"], "filed_at": withdrawn_at, "filing_type": "8-K", "excerpt": "The Company is withdrawing its fiscal 2024 guidance.",
                "source_url": "https://www.sec.gov/Archives/edgar/data/123/000000012324000003/release.htm"}
    return {**outcome_row, "withdrawal": {"withdrawn_at": withdrawn_at, "period_close": period_close, "evidence": evidence}}


def test_a_withdrawal_that_04_found_is_proposed_as_withdrawn_and_carries_its_evidence(parts) -> None:
    draft, outcome_row, _ = parts
    assert review.build_row(draft, outcome_row, TODAY)["aid_proposed_status"] == "missed"  # from the numbers alone: 4800 against 5000 to 6000
    row = review.build_row(draft, withdrawn(outcome_row), TODAY)
    assert row["aid_proposed_status"] == "withdrawn" and row["status"] == "open"  # a proposal: the row is still a draft
    note = row["aid_withdrawal_note"]
    assert "filed 2024-03-01, before the period closed on 2024-12-31" in note and "The Company is withdrawing its fiscal 2024 guidance." in note
    assert "https://www.sec.gov/Archives/edgar/data/123/000000012324000003/release.htm" in note


def test_a_row_with_no_withdrawal_has_no_note(parts) -> None:
    draft, outcome_row, _ = parts
    assert review.build_row(draft, outcome_row, TODAY)["aid_withdrawal_note"] == ""
    assert review.build_row(draft, {**outcome_row, "withdrawal": None}, TODAY)["aid_withdrawal_note"] == ""
    assert review.build_row(draft, None, TODAY)["aid_withdrawal_note"] == ""


def test_a_withdrawal_outranks_the_numbers_and_a_row_with_no_outcome_is_withdrawn_not_unresolved(parts) -> None:
    draft, outcome_row, _ = parts
    hit = {**outcome_row, "outcome": {**outcome_row["outcome"], "reported_value": 5500.0}}  # would be met
    assert review.build_row(draft, hit, TODAY)["aid_proposed_status"] == "met"
    assert review.build_row(draft, withdrawn(hit), TODAY)["aid_proposed_status"] == "withdrawn"
    none = {"draft_id": draft["draft_id"], "outcome": None, "outcome_reason": "no later 8-K release for that period found",
            "acknowledgement": None, "withdrawal": withdrawn(outcome_row)["withdrawal"]}
    assert review.build_row(draft, none, TODAY)["aid_proposed_status"] == "withdrawn"
    assert review.build_row(draft, {**none, "withdrawal": None}, TODAY)["aid_proposed_status"] == "unresolved"


def test_the_last_day_of_the_period_still_counts_and_the_day_after_does_not(parts) -> None:
    """The rubric's own date test (`withdrawn_before_close`), applied again to what 04 wrote."""
    draft, outcome_row, _ = parts
    assert review.build_row(draft, withdrawn(outcome_row, "2024-12-31", "2024-12-31"), TODAY)["aid_proposed_status"] == "withdrawn"
    late = review.build_row(draft, withdrawn(outcome_row, "2025-01-01", "2024-12-31"), TODAY)
    assert late["aid_proposed_status"] == "missed" and late["aid_withdrawal_note"] == ""  # not a withdrawal under the rubric: it resolves on the numbers


@pytest.mark.parametrize("bad", [{}, {"withdrawn_at": "2024-03-01"}, {"withdrawn_at": "soon", "period_close": "2024-12-31"}, {"withdrawn_at": "2024-03-01", "period_close": None}])
def test_a_withdrawal_that_is_malformed_is_ignored(parts, bad) -> None:
    draft, outcome_row, _ = parts
    row = review.build_row(draft, {**outcome_row, "withdrawal": bad}, TODAY)
    assert row["aid_proposed_status"] == "missed" and row["aid_withdrawal_note"] == ""


# --- refreshing pipeline-owned columns on untouched rows --------------------


def test_is_pipeline_owned_covers_assumption_outcome_and_aid_columns_but_not_verify() -> None:
    for column in ["assumption.metric", "assumption.target_low", "assumption.evidence.excerpt", "claim", "invalidation_condition",
                    "outcome.reported_value", "acknowledged_at", "acknowledgement_evidence.excerpt",
                    "days_to_falsifiable", "days_to_acknowledged", "aid_proposed_status", "aid_outcome_note", "aid_withdrawal_note"]:
        assert review.is_pipeline_owned(column)
    for column in ["record_id", "status", "approved", "hand_verified", "reviewer_note", "conflict",
                    "empty_block", "aid_verify", "aid_verify_reason", "aid_verify_class", "aid_suggested_note"]:
        assert not review.is_pipeline_owned(column)


def test_untouched_by_a_human() -> None:
    assert review.untouched_by_a_human({"approved": "false", "hand_verified": "false", "reviewer_note": ""})
    assert not review.untouched_by_a_human({"approved": "true", "hand_verified": "false", "reviewer_note": ""})
    assert not review.untouched_by_a_human({"approved": "false", "hand_verified": "true", "reviewer_note": ""})
    assert not review.untouched_by_a_human({"approved": "false", "hand_verified": "false", "reviewer_note": "checked on EDGAR"})


def test_refresh_row_replaces_only_pipeline_owned_columns() -> None:
    row = {c: "old" for c in review.COLUMNS}
    fresh = {c: "new" for c in review.COLUMNS}
    merged = review.refresh_row(row, fresh)
    for c in review.COLUMNS:
        assert merged[c] == ("new" if review.is_pipeline_owned(c) or c == "conflict" else "old")


def test_refresh_row_follows_the_drafts_conflict_flag_both_ways_and_keeps_the_other_flags() -> None:
    """Micron's conflicts went away once the table's columns set each figure's basis; the flag the CSV row was created with
    stayed. It comes from the draft, so a refresh of an untouched row rewrites it, and nothing else in the flag group."""
    row = {c: "" for c in review.COLUMNS} | {"conflict": "true", "empty_block": "false", "ack_pending_review": "true"}
    fresh = {c: "" for c in review.COLUMNS} | {"conflict": "false", "empty_block": "false", "ack_pending_review": "false"}
    merged = review.refresh_row(row, fresh)
    assert merged["conflict"] == "false" and merged["ack_pending_review"] == "true"
    assert review.refresh_row({**row, "conflict": "false"}, {**fresh, "conflict": "true"})["conflict"] == "true"


def test_recompute_content_sha256_hashes_the_extracted_text_not_the_raw_bytes(dirs) -> None:
    html = b"<html><body><p>Revenue guidance <script>var x = Math.random();</script>of $5 billion.</p></body></html>"
    write_cached_document(dirs, COMPANY.cik, "0000000123-24-000001", html)
    row = {"cik": COMPANY.cik, "assumption.evidence.accession_number": "0000000123-24-000001", "assumption.evidence.content_sha256": "0" * 64}
    updated = review.recompute_content_sha256(row, dirs / "raw")
    expected = hashlib.sha256(html_to_text(html).encode("utf-8")).hexdigest()
    assert updated["assumption.evidence.content_sha256"] == expected
    assert expected != hashlib.sha256(html).hexdigest()  # provably not a hash of the raw bytes


def test_recompute_content_sha256_updates_outcome_and_acknowledgement_evidence_too(dirs) -> None:
    write_cached_document(dirs, COMPANY.cik, "0000000123-24-000002", b"<p>Full year revenue was $4.8 billion.</p>")
    write_cached_document(dirs, COMPANY.cik, "0000000123-24-000003", b"<p>Below our guidance.</p>")
    row = {
        "cik": COMPANY.cik,
        "outcome.evidence.accession_number": "0000000123-24-000002", "outcome.evidence.content_sha256": "0" * 64,
        "acknowledgement_evidence.accession_number": "0000000123-24-000003", "acknowledgement_evidence.content_sha256": "0" * 64,
    }
    updated = review.recompute_content_sha256(row, dirs / "raw")
    assert updated["outcome.evidence.content_sha256"] not in ("0" * 64, "")
    assert updated["acknowledgement_evidence.content_sha256"] not in ("0" * 64, "")


def test_recompute_content_sha256_leaves_a_row_alone_when_the_document_is_missing(dirs) -> None:
    row = {"cik": COMPANY.cik, "assumption.evidence.accession_number": "no-such-accession", "assumption.evidence.content_sha256": "0" * 64}
    assert review.recompute_content_sha256(row, dirs / "raw") == row


def test_recompute_content_sha256_leaves_a_row_alone_without_a_cik() -> None:
    row = {"assumption.evidence.accession_number": "x", "assumption.evidence.content_sha256": "0" * 64}
    assert review.recompute_content_sha256(row, Path("/nonexistent")) == row


def test_recompute_content_sha256_leaves_a_row_alone_without_an_accession(dirs) -> None:
    row = {"cik": COMPANY.cik, "assumption.evidence.content_sha256": "0" * 64}
    assert review.recompute_content_sha256(row, dirs / "raw") == row


def test_refresh_pipeline_fields_recomputes_content_sha256_even_on_an_approved_row(dirs, parts) -> None:
    """content_sha256 is a fact about the cached document, not a human's judgement call, so it is
    recomputed even on a row a human has already approved -- unlike every other pipeline-owned field."""
    draft, outcome_row, _ = parts
    seed(dirs, [draft], [outcome_row])
    review.review_company(COMPANY, TODAY)
    path = dirs / "review" / f"{COMPANY.cik}.csv"
    html = b"<p>Revenue is expected to be $5.0 billion to $6.0 billion.</p>"
    write_cached_document(dirs, COMPANY.cik, "0000000123-24-000001", html)
    edited = review.read_csv(path)
    edited[0].update(approved="true", reviewer_note="checked on EDGAR")
    review.write_csv(path, edited)

    rows, stats = review.refresh_pipeline_fields(COMPANY, TODAY)
    assert stats["content_sha256_recomputed"] == 1
    assert (rows[0]["approved"], rows[0]["reviewer_note"]) == ("true", "checked on EDGAR")  # human edits intact
    assert rows[0]["assumption.metric"] == edited[0]["assumption.metric"]  # not rebuilt, only rehashed
    expected = hashlib.sha256(html_to_text(html).encode("utf-8")).hexdigest()
    assert rows[0]["assumption.evidence.content_sha256"] == expected
    assert review.read_csv(path)[0]["assumption.evidence.content_sha256"] == expected  # written back


def test_refresh_pipeline_fields_recomputes_content_sha256_on_an_empty_block_row(dirs) -> None:
    seed(dirs, [], [])
    seed_blocks(dirs, [empty_block()])
    review.review_company(COMPANY, TODAY)
    html = b"<p>Tax rate 17.0%</p>"
    write_cached_document(dirs, COMPANY.cik, "0000000123-24-000001", html)

    rows, stats = review.refresh_pipeline_fields(COMPANY, TODAY)
    flag = next(r for r in rows if r["empty_block"] == "true")
    expected = hashlib.sha256(html_to_text(html).encode("utf-8")).hexdigest()
    assert flag["assumption.evidence.content_sha256"] == expected
    assert stats["content_sha256_recomputed"] == 1


def test_refresh_pipeline_fields_updates_an_untouched_row(dirs, parts) -> None:
    draft, outcome_row, ack = parts
    seed(dirs, [draft], [outcome_row])
    review.review_company(COMPANY, TODAY)
    path = dirs / "review" / f"{COMPANY.cik}.csv"
    assert review.read_csv(path)[0]["acknowledged_at"] == ""

    seed(dirs, [draft], [{**outcome_row, "acknowledgement": ack}])  # 04_outcomes now finds an acknowledgement
    rows, stats = review.refresh_pipeline_fields(COMPANY, TODAY)
    assert (stats["changed"], stats["unchanged"], stats["kept"]) == (1, 0, 0)
    assert rows[0]["acknowledged_at"] == "2025-05-01" and rows[0]["acknowledgement_evidence.excerpt"] == "Below our guidance."
    assert review.read_csv(path)[0]["acknowledged_at"] == "2025-05-01"  # written back


def test_refresh_pipeline_fields_updates_assumption_claim_and_invalidation_condition(dirs, parts) -> None:
    """From the "expense of" sign fix: 03_structure changed a draft's target_low/target_high after it was
    already queued, and the review CSV needs to pick that up on a row nobody has touched."""
    draft, outcome_row, _ = parts
    seed(dirs, [draft], [outcome_row])
    review.review_company(COMPANY, TODAY)
    path = dirs / "review" / f"{COMPANY.cik}.csv"
    before = review.read_csv(path)[0]
    assert before["assumption.target_low"] == "5000.0" and before["invalidation_condition"] == "reported value falls outside [5000, 6000]"

    revised = copy.deepcopy(draft)
    revised["assumption"] = {**revised["assumption"], "target_low": -6000.0, "target_high": -5000.0}
    seed(dirs, [revised], [outcome_row])
    rows, stats = review.refresh_pipeline_fields(COMPANY, TODAY)
    assert stats["changed"] == 1
    assert rows[0]["assumption.target_low"] == "-6000.0" and rows[0]["assumption.target_high"] == "-5000.0"
    assert "-6000" in rows[0]["invalidation_condition"] and "-6000" in rows[0]["claim"]
    assert review.read_csv(path)[0]["assumption.target_low"] == "-6000.0"  # written back


def test_refresh_pipeline_fields_never_touches_a_row_with_a_human_edit(dirs, parts) -> None:
    draft, outcome_row, ack = parts
    seed(dirs, [draft], [outcome_row])
    review.review_company(COMPANY, TODAY)
    path = dirs / "review" / f"{COMPANY.cik}.csv"
    edited = review.read_csv(path)
    edited[0].update(approved="true", reviewer_note="checked on EDGAR")
    review.write_csv(path, edited)

    seed(dirs, [draft], [{**outcome_row, "acknowledgement": ack}])
    rows, stats = review.refresh_pipeline_fields(COMPANY, TODAY)
    assert stats["kept"] == 1 and stats["changed"] == 0
    assert rows[0]["acknowledged_at"] == "" and rows[0]["approved"] == "true"  # untouched, hand edit intact


def test_refresh_pipeline_fields_never_touches_a_hand_verified_row(dirs, parts) -> None:
    """hand_verified can be set without approved or reviewer_note ever being touched (SCOPE 4.3: Navneet
    independently re-finds a row on EDGAR by hand), and that alone must stop a refresh."""
    draft, outcome_row, ack = parts
    seed(dirs, [draft], [outcome_row])
    review.review_company(COMPANY, TODAY)
    path = dirs / "review" / f"{COMPANY.cik}.csv"
    edited = review.read_csv(path)
    edited[0].update(hand_verified="true")
    review.write_csv(path, edited)
    before = review.read_csv(path)[0]

    revised = copy.deepcopy(draft)
    revised["assumption"] = {**revised["assumption"], "target_low": -6000.0, "target_high": -5000.0}
    seed(dirs, [revised], [{**outcome_row, "acknowledgement": ack}])
    rows, stats = review.refresh_pipeline_fields(COMPANY, TODAY)
    assert stats["kept"] == 1 and stats["changed"] == 0
    # untouched except for the parked acknowledgement: every column but the proposal ones is byte for byte
    proposal = {c for c in review.COLUMNS if c.startswith("aid_ack_proposed_") or c == "ack_pending_review"}
    assert {c: v for c, v in rows[0].items() if c not in proposal} == {c: v for c, v in before.items() if c not in proposal}


def test_refresh_pipeline_fields_never_touches_aid_verify_columns(dirs, parts) -> None:
    draft, outcome_row, ack = parts
    seed(dirs, [draft], [outcome_row])
    review.review_company(COMPANY, TODAY)
    path = dirs / "review" / f"{COMPANY.cik}.csv"
    edited = review.read_csv(path)
    edited[0].update(aid_verify="yes", aid_verify_reason="matches the excerpt", aid_verify_class="")
    review.write_csv(path, edited)

    seed(dirs, [draft], [{**outcome_row, "acknowledgement": ack}])
    rows, _ = review.refresh_pipeline_fields(COMPANY, TODAY)
    assert (rows[0]["aid_verify"], rows[0]["aid_verify_reason"]) == ("yes", "matches the excerpt")
    assert rows[0]["acknowledged_at"] == "2025-05-01"  # the pipeline-owned column still refreshed


def test_refresh_pipeline_fields_skips_empty_block_rows(dirs, parts) -> None:
    draft, outcome_row, _ = parts
    seed(dirs, [draft], [outcome_row])
    seed_blocks(dirs, [empty_block()])
    review.review_company(COMPANY, TODAY)
    path = dirs / "review" / f"{COMPANY.cik}.csv"
    before = review.read_csv(path)
    rows, stats = review.refresh_pipeline_fields(COMPANY, TODAY)
    flags = [r for r in rows if r["empty_block"] == "true"]
    assert len(flags) == 1 and flags[0] == next(r for r in before if r["empty_block"] == "true")
    assert stats["kept"] >= 1


def test_refresh_pipeline_fields_leaves_a_row_alone_when_its_draft_is_gone(dirs, parts) -> None:
    draft, outcome_row, _ = parts
    seed(dirs, [draft], [outcome_row])
    review.review_company(COMPANY, TODAY)
    seed(dirs, [], [])  # the draft is no longer produced by 03_structure
    rows, stats = review.refresh_pipeline_fields(COMPANY, TODAY)
    assert stats["kept"] == 1 and rows[0]["record_id"] == draft["draft_id"]


def test_refresh_pipeline_fields_on_a_missing_file_does_nothing(dirs) -> None:
    rows, stats = review.refresh_pipeline_fields(COMPANY, TODAY)
    assert rows == [] and stats == Counter()


def test_main_refresh_pipeline_fields_flag(dirs, parts, tmp_path, capsys) -> None:
    import yaml

    draft, outcome_row, ack = parts
    seed(dirs, [draft], [outcome_row])
    review.review_company(COMPANY, TODAY)
    seed(dirs, [draft], [{**outcome_row, "acknowledgement": ack}])
    cfg = common.load_config()
    cfg["companies"] = [{"name": COMPANY.name, "ticker": COMPANY.ticker, "cik": COMPANY.cik}]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    assert review.main(["--config", str(path), "--refresh-pipeline-fields"]) == 0
    out = capsys.readouterr().out
    assert "refreshing assumption/outcome/acknowledgement/aid_ columns" in out and "1 changed" in out
    assert review.read_csv(dirs / "review" / f"{COMPANY.cik}.csv")[0]["acknowledged_at"] == "2025-05-01"


def test_a_withdrawn_row_reaches_the_csv_and_the_summary_counts_it(dirs, parts, tmp_path, capsys) -> None:
    import yaml

    draft, outcome_row, _ = parts
    seed(dirs, [draft, another(draft, "Z")], [withdrawn(outcome_row), {**outcome_row, "draft_id": another(draft, "Z")["draft_id"]}])
    cfg = common.load_config()
    cfg["companies"] = [{"name": COMPANY.name, "ticker": COMPANY.ticker, "cik": COMPANY.cik}]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    assert review.main(["--config", str(path)]) == 0
    rows = review.read_csv(dirs / "review" / f"{COMPANY.cik}.csv")
    assert sorted(r["aid_proposed_status"] for r in rows) == ["missed", "withdrawn"] and {r["status"] for r in rows} == {"open"}
    assert "proposed: {'missed': 1, 'withdrawn': 1}" in capsys.readouterr().out


# --- an acknowledgement 04 finds for a human-touched row is proposed, not dropped ------------------


def touched_row(dirs, parts, **edits):
    draft, outcome_row, ack = parts
    seed(dirs, [draft], [outcome_row])
    review.review_company(COMPANY, TODAY)
    path = dirs / "review" / f"{COMPANY.cik}.csv"
    edited = review.read_csv(path)
    edited[0].update({"approved": "true", **edits})
    review.write_csv(path, edited)
    seed(dirs, [draft], [{**outcome_row, "acknowledgement": ack}])
    return ack


def test_refresh_proposes_a_new_acknowledgement_on_an_approved_row_and_leaves_the_real_columns_alone(dirs, parts) -> None:
    ack = touched_row(dirs, parts)
    before = review.read_csv(dirs / "review" / f"{COMPANY.cik}.csv")[0]
    rows, stats = review.refresh_pipeline_fields(COMPANY, TODAY)
    row = rows[0]
    assert stats["ack_proposed"] == 1 and row["ack_pending_review"] == "true"
    assert row["aid_ack_proposed_at"] == ack["acknowledged_at"]
    assert row["aid_ack_proposed_excerpt"] == ack["evidence"]["excerpt"]
    assert row["aid_ack_proposed_url"] == ack["evidence"]["source_url"]
    evidence = json.loads(row["aid_ack_proposed_evidence"])
    assert evidence["accession_number"] == ack["evidence"]["accession_number"] and evidence["excerpt"] == ack["evidence"]["excerpt"]
    for column in ("acknowledged_at", "days_to_acknowledged", "approved", "reviewer_note", *(c for c in review.COLUMNS if c.startswith("acknowledgement_evidence."))):
        assert row[column] == before[column] == ("true" if column == "approved" else "")


def test_refresh_proposes_on_a_row_with_only_a_note_or_a_hand_verification_too(dirs, parts) -> None:
    touched_row(dirs, parts, reviewer_note="checked", approved="false")
    rows, _ = review.refresh_pipeline_fields(COMPANY, TODAY)
    assert rows[0]["ack_pending_review"] == "true" and rows[0]["approved"] == "false"


def test_refresh_does_not_propose_what_the_row_already_has(dirs, parts) -> None:
    draft, outcome_row, ack = parts
    seed(dirs, [draft], [{**outcome_row, "acknowledgement": ack}])
    review.review_company(COMPANY, TODAY)
    path = dirs / "review" / f"{COMPANY.cik}.csv"
    edited = review.read_csv(path)
    edited[0].update(approved="true")
    review.write_csv(path, edited)
    rows, stats = review.refresh_pipeline_fields(COMPANY, TODAY)
    assert stats["ack_proposed"] == 0 and rows[0]["ack_pending_review"] == "false" and rows[0]["aid_ack_proposed_at"] == ""


def test_refresh_does_not_re_propose_an_acknowledgement_the_reviewer_rejected(dirs, parts) -> None:
    ack = touched_row(dirs, parts)
    note = f"acknowledgement proposal rejected ({ack['acknowledged_at']}, {ack['evidence']['source_url']}): wrong metric"
    path = dirs / "review" / f"{COMPANY.cik}.csv"
    edited = review.read_csv(path)
    edited[0].update(reviewer_note=note)
    review.write_csv(path, edited)
    rows, stats = review.refresh_pipeline_fields(COMPANY, TODAY)
    assert stats["ack_proposed"] == 0 and rows[0]["ack_pending_review"] == "false"


def test_refresh_leaves_a_row_alone_when_04_found_no_acknowledgement(dirs, parts) -> None:
    draft, outcome_row, _ = parts
    seed(dirs, [draft], [outcome_row])
    review.review_company(COMPANY, TODAY)
    path = dirs / "review" / f"{COMPANY.cik}.csv"
    edited = review.read_csv(path)
    edited[0].update(approved="true")
    review.write_csv(path, edited)
    rows, stats = review.refresh_pipeline_fields(COMPANY, TODAY)
    assert stats["ack_proposed"] == 0 and rows[0]["ack_pending_review"] == "false"
