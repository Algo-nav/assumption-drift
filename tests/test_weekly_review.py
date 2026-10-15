"""05_review --attach-outcomes and 06_publish's pending count and status line."""

from __future__ import annotations

import importlib
import json
from datetime import date

import pytest
import yaml

from pipeline import common, manifest as mf
from test_publish import COMPANY as PUBLISHED, OTHER, csv_row, isolated_dirs, seed, world  # noqa: F401  (isolated_dirs and world are fixtures)
from test_review import COMPANY, TODAY, parts  # noqa: F401  (fixture)

review = importlib.import_module("pipeline.05_review")
publish = importlib.import_module("pipeline.06_publish")


@pytest.fixture
def queue(tmp_path, monkeypatch):
    (tmp_path / "review").mkdir(exist_ok=True)
    (tmp_path / "outcomes").mkdir(exist_ok=True)
    monkeypatch.setattr(review, "REVIEW_DIR", tmp_path / "review")
    monkeypatch.setattr(review, "OUTCOMES_DIR", tmp_path / "outcomes")
    return tmp_path


def put_rows(queue, *rows):
    review.write_csv(queue / "review" / f"{COMPANY.cik}.csv", list(rows))


def put_outcomes(queue, *rows):
    (queue / "outcomes" / f"{COMPANY.cik}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def read_rows(queue):
    return review.read_csv(queue / "review" / f"{COMPANY.cik}.csv")


def changed(before, after):
    return {c for c in before if before[c] != after[c]}


OUTCOME_COLUMNS = set(review.OUTCOME_FILL_COLUMNS) | {"aid_outcome_note", "aid_proposed_status"}


def with_ack(parts):
    draft, outcome_row, ack = parts
    return draft, {**outcome_row, "acknowledgement": ack}


def with_withdrawal(parts):
    draft, outcome_row, _ = parts
    evidence = {**outcome_row["outcome"]["evidence"], "excerpt": "The Company is withdrawing its guidance."}
    return draft, {**outcome_row, "outcome": None, "outcome_reason": "period not closed",
                   "withdrawal": {"withdrawn_at": "2024-06-01", "period_close": "2024-12-31", "evidence": evidence}}


# --- an untouched row ----------------------------------------------------------------------------------------------------


def test_an_untouched_row_gets_the_outcome_and_nothing_else_changes(queue, parts) -> None:
    draft, outcome_row, _ = parts
    before = review.build_row(draft, None, TODAY)
    assert before["outcome.reported_value"] == "" and before["aid_outcome_note"] == "outcome search not run"
    put_rows(queue, before)
    put_outcomes(queue, outcome_row)
    rows, stats = review.attach_outcomes(COMPANY, TODAY)
    fresh = review.build_row(draft, outcome_row, TODAY)
    assert stats["outcome_filled"] == 1
    assert changed(before, rows[0]) <= OUTCOME_COLUMNS and rows[0]["outcome.reported_value"] == "4800.0"
    assert all(rows[0][c] == fresh[c] for c in review.OUTCOME_FILL_COLUMNS + ["aid_outcome_note", "aid_proposed_status"])
    assert rows[0]["aid_proposed_status"] == "missed" and rows[0] == read_rows(queue)[0]


def test_row_order_is_kept_and_a_file_with_nothing_to_attach_is_not_rewritten(queue, parts) -> None:
    draft, outcome_row, _ = parts
    first = review.build_row(draft, None, TODAY)
    other = {**first, "record_id": "01HZY8Q9XMR3T7VBN2CDEFGHJM"}
    put_rows(queue, other, first)
    put_outcomes(queue, outcome_row)
    review.attach_outcomes(COMPANY, TODAY)
    after = read_rows(queue)
    assert [r["record_id"] for r in after] == [other["record_id"], first["record_id"]]  # the order a reviewer sees
    assert after[0] == other and after[1]["outcome.reported_value"] == "4800.0"
    path = queue / "review" / f"{COMPANY.cik}.csv"
    bytes_before = path.read_bytes()
    stat_before = path.stat().st_mtime_ns
    _, stats = review.attach_outcomes(COMPANY, TODAY)  # run again: everything is already there
    assert path.read_bytes() == bytes_before and path.stat().st_mtime_ns == stat_before and not stats["outcome_filled"]


def test_an_outcome_a_row_already_has_is_never_replaced(queue, parts) -> None:
    draft, outcome_row, _ = parts
    has = review.build_row(draft, outcome_row, TODAY)
    other_outcome = {**outcome_row, "outcome": {**outcome_row["outcome"], "reported_value": 5500.0}}
    put_rows(queue, has)
    put_outcomes(queue, other_outcome)
    rows, _ = review.attach_outcomes(COMPANY, TODAY)
    assert rows[0] == has


def test_an_acknowledgement_is_filled_on_an_untouched_row_with_its_days(queue, parts) -> None:
    draft, outcome_row, _ = parts
    draft2, with_it = with_ack(parts)
    before = review.build_row(draft, outcome_row, TODAY)
    put_rows(queue, before)
    put_outcomes(queue, with_it)
    rows, stats = review.attach_outcomes(COMPANY, TODAY)
    assert stats["ack_filled"] == 1
    assert rows[0]["acknowledged_at"] == "2025-05-01" and rows[0]["acknowledgement_evidence.excerpt"] == "Below our guidance."
    assert rows[0]["days_to_acknowledged"] != "" and changed(before, rows[0]) <= set(review.ACK_FILL_COLUMNS)


def test_while_a_row_has_no_outcome_its_reason_is_kept_up_to_date(queue, parts) -> None:
    draft, outcome_row, _ = parts
    before = review.build_row(draft, None, TODAY)
    put_rows(queue, before)
    put_outcomes(queue, {"draft_id": draft["draft_id"], "outcome": None, "outcome_reason": "period not closed", "acknowledgement": None, "withdrawal": None})
    rows, stats = review.attach_outcomes(COMPANY, TODAY)
    assert rows[0]["aid_outcome_note"] == "period not closed" and changed(before, rows[0]) == {"aid_outcome_note"} and stats["reason_updated"] == 1


def test_a_withdrawal_sets_the_note_and_the_proposed_status_on_an_untouched_row(queue, parts) -> None:
    draft, outcome_row = with_withdrawal(parts)
    before = review.build_row(draft, None, TODAY)
    put_rows(queue, before)
    put_outcomes(queue, outcome_row)
    rows, stats = review.attach_outcomes(COMPANY, TODAY)
    assert stats["withdrawal_filled"] == 1 and "withdrawing its guidance" in rows[0]["aid_withdrawal_note"]
    assert rows[0]["aid_proposed_status"] == "withdrawn" and rows[0]["status"] == "open"  # a person still decides


# --- a touched row -------------------------------------------------------------------------------------------------------


def test_a_touched_row_is_not_filled_it_gets_a_proposal_and_nothing_else_moves(queue, parts) -> None:
    draft, outcome_row, _ = parts
    before = {**review.build_row(draft, None, TODAY), "reviewer_note": "checked against the filing", "approved": "true", "hand_verified": "true",
              "aid_verify": "yes", "aid_verify_reason": "matches", "claim": "edited claim", "assumption.target_high": "6100.0"}
    put_rows(queue, before)
    put_outcomes(queue, outcome_row)
    rows, stats = review.attach_outcomes(COMPANY, TODAY)
    after = rows[0]
    assert stats["outcome_proposed"] == 1
    assert changed(before, after) == {"change_pending_review", "aid_proposed_change"}  # the edited claim and number, the note, the verdict: all as they were
    proposal = json.loads(after["aid_proposed_change"])
    assert proposal["outcome.reported_value"] == "4800.0" and "claim" not in proposal and "assumption.target_high" not in proposal
    assert after["outcome.reported_value"] == "" and after["change_pending_review"] == "true"
    assert after["aid_derived_snapshot"] == before["aid_derived_snapshot"]  # propose_change would have rebuilt it from the row itself


def test_an_earlier_pending_proposal_is_kept_and_added_to(queue, parts) -> None:
    draft, outcome_row, _ = parts
    earlier = {"claim": "a re-derived claim"}
    before = {**review.build_row(draft, None, TODAY), "reviewer_note": "n", "change_pending_review": "true", "aid_proposed_change": json.dumps(earlier)}
    put_rows(queue, before)
    put_outcomes(queue, outcome_row)
    rows, _ = review.attach_outcomes(COMPANY, TODAY)
    proposal = json.loads(rows[0]["aid_proposed_change"])
    assert proposal["claim"] == "a re-derived claim" and proposal["outcome.reported_value"] == "4800.0"


def test_a_rejected_outcome_is_not_proposed_again(queue, parts) -> None:
    draft, outcome_row, _ = parts
    fresh = review.build_row(draft, outcome_row, TODAY)
    changes = {c: fresh[c] for c in review.OUTCOME_FILL_COLUMNS + ["aid_outcome_note"] if fresh[c] != review.build_row(draft, None, TODAY)[c]}
    before = {**review.build_row(draft, None, TODAY), "reviewer_note": f"proposed change rejected [{review.change_hash(changes)}]: wrong figure"}
    put_rows(queue, before)
    put_outcomes(queue, outcome_row)
    rows, stats = review.attach_outcomes(COMPANY, TODAY)
    assert rows[0] == before and not stats["outcome_proposed"]  # nothing changed, and nothing is counted as proposed


def test_a_touched_row_gets_an_acknowledgement_proposal_in_its_own_columns(queue, parts) -> None:
    draft, outcome_row, _ = parts
    _, with_it = with_ack(parts)
    before = {**review.build_row(draft, outcome_row, TODAY), "approved": "true"}
    put_rows(queue, before)
    put_outcomes(queue, with_it)
    rows, stats = review.attach_outcomes(COMPANY, TODAY)
    after = rows[0]
    assert stats["ack_proposed"] == 1 and after["ack_pending_review"] == "true" and after["aid_ack_proposed_at"] == "2025-05-01"
    assert after["acknowledged_at"] == "" and changed(before, after) <= {"ack_pending_review", "aid_ack_proposed_at", "aid_ack_proposed_excerpt",
                                                                           "aid_ack_proposed_url", "aid_ack_proposed_evidence"}


def test_a_touched_row_gets_a_withdrawal_in_its_proposal_not_in_its_note(queue, parts) -> None:
    draft, outcome_row = with_withdrawal(parts)
    before = {**review.build_row(draft, None, TODAY), "reviewer_note": "seen"}
    put_rows(queue, before)
    put_outcomes(queue, outcome_row)
    rows, stats = review.attach_outcomes(COMPANY, TODAY)
    assert stats["withdrawal_proposed"] == 1 and rows[0]["aid_withdrawal_note"] == ""
    proposal = json.loads(rows[0]["aid_proposed_change"])
    assert "withdrawing its guidance" in proposal["aid_withdrawal_note"] and proposal["aid_proposed_status"] == "withdrawn"


# --- what is left alone --------------------------------------------------------------------------------------------------


def test_flags_rows_with_no_outcome_entry_and_rows_that_are_not_valid_records_are_left_exactly_as_they_were(queue, parts) -> None:
    draft, outcome_row, _ = parts
    flag = {**review.build_row(draft, None, TODAY), "record_id": "01HZY8Q9XMR3T7VBN2CDEFGHJN", "empty_block": "true", "assumption.metric": ""}
    unknown = {**review.build_row(draft, None, TODAY), "record_id": "01HZY8Q9XMR3T7VBN2CDEFGHJP"}
    broken = {**review.build_row(draft, None, TODAY), "record_id": outcome_row["draft_id"], "assumption.stated_at": "not a date"}
    put_rows(queue, flag, unknown, broken)
    put_outcomes(queue, outcome_row, {**outcome_row, "draft_id": flag["record_id"]})
    rows, stats = review.attach_outcomes(COMPANY, TODAY)
    assert rows == [flag, unknown, broken] and read_rows(queue) == rows


def test_it_needs_no_drafts_candidates_or_raw_filings(queue, parts, tmp_path, monkeypatch) -> None:
    """A runner has data/review/ and data/outcomes/ and nothing else."""
    draft, outcome_row, _ = parts
    put_rows(queue, review.build_row(draft, None, TODAY))
    put_outcomes(queue, outcome_row)
    monkeypatch.setattr(review, "DRAFTS_DIR", tmp_path / "no-drafts")
    monkeypatch.setattr(review, "CANDIDATES_DIR", tmp_path / "no-candidates")
    monkeypatch.setattr(review, "RAW_DIR", tmp_path / "no-raw")
    review.attach_outcomes(COMPANY, TODAY)
    assert read_rows(queue)[0]["outcome.reported_value"] == "4800.0"


def test_main_attach_outcomes_runs_for_the_configured_companies(queue, parts, tmp_path, capsys) -> None:
    draft, outcome_row, _ = parts
    put_rows(queue, review.build_row(draft, None, TODAY))
    put_outcomes(queue, outcome_row)
    cfg = common.load_config()
    cfg["companies"] = [{"name": COMPANY.name, "ticker": COMPANY.ticker, "cik": COMPANY.cik}]
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(cfg))
    assert review.main(["--config", str(config), "--attach-outcomes"]) == 0
    assert "outcome filled 1" in capsys.readouterr().out and read_rows(queue)[0]["outcome.reported_value"] == "4800.0"


# --- 06: pending rows and the status line --------------------------------------------------------------------------------


def test_a_pending_row_is_not_approved_has_no_note_and_is_not_a_flag(world, record_data) -> None:
    rows = [
        csv_row(record_data, approved="false"),                                            # pending
        csv_row(record_data, approved="false", empty_block="true"),                        # a flag: not a record
        {**csv_row(record_data, approved="false"), "reviewer_note": "rejected: wrong"},    # looked at, and said no
        {**csv_row(record_data, approved="false"), "reviewer_note": "   "},                # a blank note is no note
        csv_row(record_data, approved="true"),                                             # decided
    ]
    seed(world, PUBLISHED, rows)
    assert len(publish.pending_rows(PUBLISHED, world / "review")) == 2
    assert publish.pending_rows(OTHER, world / "review") == []  # no review file


@pytest.mark.parametrize("pending, text", [
    (0, "Filings processed through 2026-09-20. No rows pending review."),
    (1, "Filings processed through 2026-09-20. 1 row pending review."),
    (148, "Filings processed through 2026-09-20. 148 rows pending review."),
    (1234, "Filings processed through 2026-09-20. 1,234 rows pending review."),
])
def test_the_status_line_says_none_one_or_how_many(pending, text) -> None:
    assert publish.format_status_line(date(2026, 9, 20), pending) == text


def test_the_card_and_the_space_carry_the_manifest_date_and_the_count(world, record_data, monkeypatch) -> None:
    config = world / "config.yaml"
    cfg = common.load_config()
    cfg["companies"] = [{"name": PUBLISHED.name, "ticker": PUBLISHED.ticker, "cik": PUBLISHED.cik, "fiscal_year_end_month": 1}]
    config.write_text(yaml.safe_dump(cfg))
    mf.save_manifest(mf.Manifest(date(2026, 10, 4), {}), common.MANIFEST_PATH)
    seed(world, PUBLISHED, [csv_row(record_data), csv_row(record_data, approved="false"), csv_row(record_data, approved="false", empty_block="true")])
    assert publish.main(["--config", str(config)]) == 0
    line = "Filings processed through 2026-10-04. 1 row pending review."
    card = (world / "card" / "README.md").read_text()
    assert line in card and card.index("# assumption-drift") < card.index(line) < card.index("Write-up:")
    assert "filed between\nJanuary 2019 and October 2026" in card  # the range ends on the manifest date
    page = (world / "space" / "index.html").read_text()
    assert f'<p class="dateline">{line}</p>' in page
    assert page.index("generated ") < page.index(line) < page.index("Source: SEC EDGAR")  # under the dateline, above the links note
    assert "filings Jan 2019 to Oct 2026" in page  # and the Space range ends on the manifest date too


def test_no_pending_rows_reads_as_such(world, record_data) -> None:
    config = world / "config.yaml"
    cfg = common.load_config()
    cfg["companies"] = [{"name": PUBLISHED.name, "ticker": PUBLISHED.ticker, "cik": PUBLISHED.cik, "fiscal_year_end_month": 1}]
    config.write_text(yaml.safe_dump(cfg))
    seed(world, PUBLISHED, [csv_row(record_data)])
    assert publish.main(["--config", str(config)]) == 0
    assert "Filings processed through 2026-09-20. No rows pending review." in (world / "card" / "README.md").read_text()


def test_publish_stops_before_writing_anything_without_a_manifest(world, record_data, capsys) -> None:
    config = world / "config.yaml"
    cfg = common.load_config()
    cfg["companies"] = [{"name": PUBLISHED.name, "ticker": PUBLISHED.ticker, "cik": PUBLISHED.cik, "fiscal_year_end_month": 1}]
    config.write_text(yaml.safe_dump(cfg))
    seed(world, PUBLISHED, [csv_row(record_data)])
    common.MANIFEST_PATH.unlink()
    assert publish.main(["--config", str(config)]) == 2
    assert "bootstrap" in capsys.readouterr().err and not (world / "release").exists() and not (world / "card").exists()


def test_the_space_page_leaves_the_line_out_when_it_is_not_given(record_data) -> None:
    space = importlib.import_module("pipeline.space")
    from research_record import stats
    from pipeline import figures
    records = [ResearchRecordFor(record_data)]
    page = space.render_page(records, stats.compute(records), figures.acknowledgement_summary(records), limitations=["l"], hf_user="u", generated_at=date(2026, 9, 27))
    assert "Filings processed through" not in page


def ResearchRecordFor(record_data):
    from research_record.schema import ResearchRecord
    return ResearchRecord(**record_data())
