"""research_record/reviewer.py: the terminal, one-row-at-a-time review tool `rr review` drives."""

from __future__ import annotations

import csv
import importlib
import json

import pytest

from research_record import reviewer as rv

COLUMNS = [
    "record_id", "ticker", "assumption.metric", "assumption.target_period", "assumption.stated_at",
    "assumption.target_low", "assumption.target_high", "assumption.unit",
    "assumption.evidence.source_url", "assumption.evidence.excerpt",
    "outcome.reported_value", "outcome.reported_at", "outcome.evidence.excerpt",
    "approved", "hand_verified", "reviewer_note", "conflict", "empty_block",
    "aid_proposed_status", "aid_outcome_note", "aid_flag_note", "aid_verify", "aid_verify_reason", "aid_suggested_note",
    "acknowledged_at", "days_to_acknowledged", "acknowledgement_evidence.source_url", "acknowledgement_evidence.accession_number",
    "acknowledgement_evidence.filed_at", "acknowledgement_evidence.excerpt", "acknowledgement_evidence.content_sha256",
    "ack_pending_review", "aid_ack_proposed_at", "aid_ack_proposed_excerpt", "aid_ack_proposed_url", "aid_ack_proposed_evidence",
    "change_pending_review", "aid_proposed_change", "claim",
]


def row(record_id, **overrides) -> dict[str, str]:
    base = {c: "" for c in COLUMNS}
    base.update(
        record_id=record_id, ticker="EXMP", **{
            "assumption.metric": "revenue", "assumption.target_period": "Q4 FY2026", "assumption.stated_at": "2026-01-01",
            "assumption.target_low": "5.0", "assumption.target_high": "6.0", "assumption.unit": "USD billions",
            "assumption.evidence.source_url": "https://www.sec.gov/Archives/edgar/data/123/x/release.htm",
            "assumption.evidence.excerpt": "Revenue is expected to be $5.0 billion to $6.0 billion.",
        },
        approved="false", hand_verified="false", reviewer_note="", conflict="false", empty_block="false",
        aid_proposed_status="unresolved",
    )
    base.update(overrides)
    return base


def write_csv(path, rows, fieldnames=COLUMNS):
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path):
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# --- highlighting ------------------------------------------------------------


def test_highlight_numbers_wraps_every_numeric_token() -> None:
    text = "$5.0 billion to $6.0 billion, plus or minus 2%"
    out = rv.highlight_numbers(text, wrap=lambda s: f"[{s}]")
    assert out == "$[5.0] billion to $[6.0] billion, plus or minus [2%]"


def test_highlight_numbers_default_wrap_uses_ansi() -> None:
    assert "\x1b[1;33m5\x1b[0m" in rv.highlight_numbers("5 units")


def test_highlight_numbers_on_empty_text_is_a_no_op() -> None:
    assert rv.highlight_numbers("") == ""


# --- the file: load and save --------------------------------------------------


def test_load_round_trips_columns_in_their_own_order_including_ones_reviewer_does_not_know(tmp_path) -> None:
    path = tmp_path / "R.csv"
    write_csv(path, [row("R1")], fieldnames=COLUMNS + ["some_future_column"])
    rf = rv.ReviewFile.load(path)
    assert rf.fieldnames == COLUMNS + ["some_future_column"]
    rf.rows[0]["some_future_column"] = "untouched"
    rf.save()
    assert read_csv(path)[0]["some_future_column"] == "untouched"


def test_a_missing_file_raises(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        rv.ReviewFile.load(tmp_path / "no-such-file.csv")


def test_a_csv_without_a_record_id_column_is_rejected(tmp_path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("a,b\n1,2\n")
    with pytest.raises(ValueError, match="record_id"):
        rv.ReviewFile.load(path)


# --- what each key does, pure --------------------------------------------------


def test_approve_and_reject() -> None:
    r = row("R1")
    assert rv.approve(r)["approved"] == "true"
    rejected = rv.reject(r, "wrong metric")
    assert (rejected["approved"], rejected["reviewer_note"]) == ("false", "wrong metric")


def test_reject_after_approve_flips_it_back() -> None:
    r = rv.approve(row("R1"))
    assert rv.reject(r, "actually no")["approved"] == "false"


def test_hand_verify() -> None:
    assert rv.hand_verify(row("R1"))["hand_verified"] == "true"


def test_edit_field_changes_only_that_column() -> None:
    r = row("R1")
    edited = rv.edit_field(r, "assumption.target_high", "6100.0")
    assert edited["assumption.target_high"] == "6100.0"
    assert {k: v for k, v in edited.items() if k != "assumption.target_high"} == {k: v for k, v in r.items() if k != "assumption.target_high"}


def test_edit_field_rejects_an_unknown_column() -> None:
    with pytest.raises(KeyError):
        rv.edit_field(row("R1"), "not_a_column", "x")


def test_edit_field_rejects_record_id() -> None:
    with pytest.raises(KeyError):
        rv.edit_field(row("R1"), "record_id", "R2")


def test_approved_count_and_due_for_hand_verify() -> None:
    rows = [rv.approve(row(f"R{i}")) for i in range(9)]
    assert rv.approved_count(rows) == 9 and not rv.due_for_hand_verify(rows)
    rows.append(rv.approve(row("R9")))
    assert rv.approved_count(rows) == 10 and rv.due_for_hand_verify(rows)
    rows.append(rv.approve(row("R10")))
    assert not rv.due_for_hand_verify(rows)  # 11 is not a multiple of ten
    rows.append(row("R11"))  # not approved
    assert not rv.due_for_hand_verify(rows)
    assert not rv.due_for_hand_verify([])  # zero approvals never fires


# --- rendering a row -----------------------------------------------------------


def test_render_row_highlights_the_target_numbers_and_shows_the_edgar_url() -> None:
    out = rv.render_row(row("R1"), 1, 3, wrap=lambda s: f"[{s}]")
    assert "target: 5.0 to 6.0 USD billions" in out
    assert "[5.0]" in out and "[6.0]" in out  # the excerpt's own numbers, highlighted
    assert "EDGAR: https://www.sec.gov/Archives/edgar/data/123/x/release.htm" in out
    assert "1/3" in out and "EXMP" in out and "revenue" in out and "Q4 FY2026" in out


def test_render_row_shows_the_outcome_excerpt_when_there_is_one() -> None:
    r = row("R1", **{"outcome.reported_value": "4.8", "outcome.reported_at": "2026-02-14", "outcome.evidence.excerpt": "Revenue was $4.8 billion."})
    out = rv.render_row(r, 1, 1, wrap=lambda s: f"[{s}]")
    assert "outcome: reported 4.8 on 2026-02-14" in out and "[4.8]" in out


def test_render_row_says_why_there_is_no_outcome() -> None:
    out = rv.render_row(row("R1", aid_outcome_note="no later 8-K release for that period found"), 1, 1)
    assert "outcome: none (no later 8-K release for that period found)" in out


ACK = {"acknowledged_at": "2026-05-20", "acknowledgement_evidence.excerpt": "Revenue of $4.8 billion was below our guidance.",
       "acknowledgement_evidence.source_url": "https://www.sec.gov/Archives/edgar/data/123/y/ack.htm"}


def test_render_row_shows_the_acknowledgement_date_excerpt_and_edgar_link() -> None:
    r = row("R1", **{"outcome.reported_value": "4.8", "outcome.reported_at": "2026-02-14", "outcome.evidence.excerpt": "x"}, **ACK)
    out = rv.render_row(r, 1, 1, wrap=lambda s: f"[{s}]")
    ack = out[out.index("acknowledgement:") :]
    assert out.index("outcome:") < out.index("acknowledgement:")
    assert "acknowledgement: 2026-05-20" in ack and "[4.8] billion was below our guidance." in ack
    assert "EDGAR: https://www.sec.gov/Archives/edgar/data/123/y/ack.htm" in ack


def test_render_row_says_none_when_there_is_no_acknowledgement() -> None:
    out = rv.render_row(row("R1", **{"outcome.reported_value": "4.8", "outcome.reported_at": "2026-02-14"}), 1, 1)
    assert "acknowledgement: none" in out and out.index("outcome:") < out.index("acknowledgement:")


def test_render_row_with_no_outcome_still_says_acknowledgement_none() -> None:
    out = rv.render_row(row("R1", aid_outcome_note="none found"), 1, 1)
    assert "acknowledgement: none" in out


def test_render_row_flags_an_empty_block_row_instead_of_a_target() -> None:
    r = row("R1", empty_block="true", aid_flag_note="the model returned no items", **{"assumption.metric": ""})
    out = rv.render_row(r, 1, 1)
    assert "OUTLOOK BLOCK WITH NO DRAFT" in out and "the model returned no items" in out
    assert "target:" not in out


def test_render_row_shows_the_verify_reason_only_when_it_says_no() -> None:
    yes = rv.render_row(row("R1", aid_verify="yes", aid_verify_reason="matches"), 1, 1)
    assert "matches" not in yes
    no = rv.render_row(row("R1", aid_verify="no", aid_verify_reason="wrong period"), 1, 1)
    assert "verify: no (wrong period)" in no


def test_render_row_shows_the_suggested_note_when_there_is_one() -> None:
    out = rv.render_row(row("R1", aid_suggested_note="false alarm: metric in heading"), 1, 1)
    assert "suggested note: false alarm: metric in heading" in out


def test_render_row_has_no_suggested_note_line_when_there_is_none() -> None:
    assert "suggested note:" not in rv.render_row(row("R1"), 1, 1)


# --- the interactive loop -------------------------------------------------------


class Script:
    """A canned read_key/read_line pair, and everything written, for driving `run()` without a terminal."""

    def __init__(self, keys=(), lines=()):
        self.keys = list(keys)
        self.lines = list(lines)
        self.out: list[str] = []

    def key(self) -> str:
        return self.keys.pop(0)

    def line(self, prompt: str = "") -> str:
        return self.lines.pop(0)

    def write(self, text: str) -> None:
        self.out.append(text)


def seed(tmp_path, *rows):
    path = tmp_path / "R.csv"
    write_csv(path, list(rows))
    return path


def test_approving_a_row_writes_true_and_moves_to_the_next(tmp_path) -> None:
    path = seed(tmp_path, row("R1"), row("R2"))
    s = Script(keys=["y", "y"])
    rv.run(path, read_key=s.key, read_line=s.line, write=s.write)
    on_disk = {r["record_id"]: r for r in read_csv(path)}
    assert on_disk["R1"]["approved"] == "true" and on_disk["R2"]["approved"] == "true"
    assert "done: 2 rows reviewed." in s.out


def test_rejecting_asks_for_a_note_and_writes_it(tmp_path) -> None:
    path = seed(tmp_path, row("R1"))
    s = Script(keys=["n"], lines=["the number is for the wrong quarter"])
    rv.run(path, read_key=s.key, read_line=s.line, write=s.write)
    on_disk = read_csv(path)[0]
    assert (on_disk["approved"], on_disk["reviewer_note"]) == ("false", "the number is for the wrong quarter")


def test_editing_a_field_stays_on_the_row_until_approved(tmp_path) -> None:
    path = seed(tmp_path, row("R1"))
    s = Script(keys=["e", "y"], lines=["assumption.target_high", "6100.0"])
    rv.run(path, read_key=s.key, read_line=s.line, write=s.write)
    on_disk = read_csv(path)[0]
    assert (on_disk["assumption.target_high"], on_disk["approved"]) == ("6100.0", "true")


def test_editing_an_unknown_field_reports_it_and_stays_on_the_row(tmp_path) -> None:
    path = seed(tmp_path, row("R1"))
    s = Script(keys=["e", "s"], lines=["not_a_column", "x"])
    rv.run(path, read_key=s.key, read_line=s.line, write=s.write)
    assert any("not a column" in m for m in s.out)
    assert read_csv(path)[0]["approved"] == "false"  # nothing changed, and it moved on with 's'


def test_skip_changes_nothing_and_moves_on(tmp_path) -> None:
    path = seed(tmp_path, row("R1"), row("R2"))
    s = Script(keys=["s", "y"])
    rv.run(path, read_key=s.key, read_line=s.line, write=s.write)
    on_disk = {r["record_id"]: r for r in read_csv(path)}
    assert on_disk["R1"]["approved"] == "false" and on_disk["R2"]["approved"] == "true"


def test_hand_verify_stays_on_the_row_so_it_can_still_be_approved_or_rejected(tmp_path) -> None:
    path = seed(tmp_path, row("R1"))
    s = Script(keys=["v", "y"])
    rv.run(path, read_key=s.key, read_line=s.line, write=s.write)
    on_disk = read_csv(path)[0]
    assert (on_disk["hand_verified"], on_disk["approved"]) == ("true", "true")


def test_quit_stops_early_and_leaves_the_rest_of_the_file_untouched(tmp_path) -> None:
    path = seed(tmp_path, row("R1"), row("R2"))
    before = path.read_bytes()
    s = Script(keys=["y", "q"])
    rv.run(path, read_key=s.key, read_line=s.line, write=s.write)
    on_disk = {r["record_id"]: r for r in read_csv(path)}
    assert on_disk["R1"]["approved"] == "true" and on_disk["R2"] == {k: v for k, v in row("R2").items()}
    assert any("stopped at row 2/2" in m for m in s.out)
    assert before != path.read_bytes()  # R1's approval was written


def test_every_keypress_is_written_before_the_next_row_is_shown(tmp_path) -> None:
    """A session cut off after the first keypress must not lose it: `run` writes to disk on every key,
    not only when it finishes or is asked to quit."""
    path = seed(tmp_path, row("R1"), row("R2"))
    s = Script(keys=["y"])  # only one key: the "session" is cut off right after
    with pytest.raises(IndexError):  # run() tries to read a second key and the script has none left
        rv.run(path, read_key=s.key, read_line=s.line, write=s.write)
    assert read_csv(path)[0]["approved"] == "true"  # already on disk


def test_every_tenth_approval_prompts_for_hand_verification_and_prints_the_edgar_url(tmp_path) -> None:
    rows = [row(f"R{i}", approved="true" if i < 9 else "false", **{"assumption.evidence.source_url": f"https://www.sec.gov/x{i}.htm"})
            for i in range(10)]
    path = seed(tmp_path, *rows)
    s = Script(keys=["s"] * 9 + ["y"], lines=["y"])  # skip the first 9 (already approved), approve the tenth
    rv.run(path, read_key=s.key, read_line=s.line, write=s.write)
    assert any("10 approvals" in m and "SCOPE 4.3" in m for m in s.out)
    assert any("EDGAR: https://www.sec.gov/x9.htm" in m for m in s.out)
    on_disk = {r["record_id"]: r for r in read_csv(path)}
    assert on_disk["R9"]["hand_verified"] == "true"


def test_declining_the_tenth_approval_prompt_leaves_hand_verified_false(tmp_path) -> None:
    rows = [row(f"R{i}", approved="true") for i in range(9)] + [row("R9")]
    path = seed(tmp_path, *rows)
    s = Script(keys=["s"] * 9 + ["y"], lines=["n"])
    rv.run(path, read_key=s.key, read_line=s.line, write=s.write)
    assert read_csv(path)[-1]["hand_verified"] == "false"


def test_approving_an_empty_block_row_is_refused(tmp_path) -> None:
    path = seed(tmp_path, row("R1", empty_block="true"))
    s = Script(keys=["y", "s"])
    rv.run(path, read_key=s.key, read_line=s.line, write=s.write)
    assert any("empty block, use s" in m for m in s.out)
    assert read_csv(path)[0]["approved"] == "false"


def test_approving_a_row_with_a_suggested_note_and_pressing_enter_accepts_it(tmp_path) -> None:
    path = seed(tmp_path, row("R1", aid_suggested_note="false alarm: metric in heading"))
    s = Script(keys=["y"], lines=[""])  # blank line: Enter
    rv.run(path, read_key=s.key, read_line=s.line, write=s.write)
    on_disk = read_csv(path)[0]
    assert (on_disk["approved"], on_disk["reviewer_note"]) == ("true", "false alarm: metric in heading")


def test_approving_a_row_with_a_suggested_note_and_typing_something_else_uses_that(tmp_path) -> None:
    path = seed(tmp_path, row("R1", aid_suggested_note="false alarm: metric in heading"))
    s = Script(keys=["y"], lines=["actually the metric is wrong"])
    rv.run(path, read_key=s.key, read_line=s.line, write=s.write)
    on_disk = read_csv(path)[0]
    assert (on_disk["approved"], on_disk["reviewer_note"]) == ("true", "actually the metric is wrong")


def test_a_suggested_note_is_not_offered_once_the_row_already_has_a_note(tmp_path) -> None:
    path = seed(tmp_path, row("R1", aid_suggested_note="false alarm: metric in heading", reviewer_note="already checked"))
    s = Script(keys=["y"])  # no lines queued: a prompt here would raise IndexError
    rv.run(path, read_key=s.key, read_line=s.line, write=s.write)
    on_disk = read_csv(path)[0]
    assert (on_disk["approved"], on_disk["reviewer_note"]) == ("true", "already checked")


def test_a_suggested_note_satisfies_a_filtered_sessions_note_requirement_without_a_second_prompt(tmp_path) -> None:
    path = seed(tmp_path, row("R1", aid_verify="no", aid_suggested_note="false alarm: metric in heading"))
    s = Script(keys=["y"], lines=[""])  # exactly one line consumed: no second prompt
    rv.run(path, filters=["verify-no"], read_key=s.key, read_line=s.line, write=s.write)
    on_disk = read_csv(path)[0]
    assert (on_disk["approved"], on_disk["reviewer_note"]) == ("true", "false alarm: metric in heading")


def test_an_unrecognised_key_is_reported_and_the_row_stays(tmp_path) -> None:
    path = seed(tmp_path, row("R1"))
    s = Script(keys=["z", "s"])
    rv.run(path, read_key=s.key, read_line=s.line, write=s.write)
    assert any("unrecognised key" in m for m in s.out)


def test_default_read_key_falls_back_to_a_line_when_stdin_is_not_a_tty(tmp_path) -> None:
    import io

    stream = io.StringIO("y\n")
    assert rv.default_read_key(stream) == "y"


def test_default_read_key_returns_q_on_end_of_input(tmp_path) -> None:
    import io

    assert rv.default_read_key(io.StringIO("")) == "q"


# --- filters -------------------------------------------------------------------


def test_filter_verify_no_matches_only_aid_verify_no() -> None:
    pred = rv.parse_filter("verify-no")
    assert pred(row("R1", aid_verify="no"))
    assert not pred(row("R2", aid_verify="yes"))
    assert not pred(row("R3", aid_verify=""))


def test_filter_no_note_matches_only_an_empty_reviewer_note() -> None:
    pred = rv.parse_filter("no-note")
    assert pred(row("R1", reviewer_note=""))
    assert not pred(row("R2", reviewer_note="already reviewed"))


def test_filter_ids_matches_only_the_listed_record_ids() -> None:
    pred = rv.parse_filter("ids=R1,R3")
    assert pred(row("R1")) and pred(row("R3"))
    assert not pred(row("R2"))


def test_filter_unrecognised_spec_raises() -> None:
    with pytest.raises(ValueError, match="bogus"):
        rv.parse_filter("bogus")


def test_combine_filters_ands_them_together() -> None:
    pred = rv.combine_filters(["verify-no", "no-note"])
    assert pred(row("R1", aid_verify="no", reviewer_note=""))
    assert not pred(row("R2", aid_verify="no", reviewer_note="already reviewed"))
    assert not pred(row("R3", aid_verify="yes", reviewer_note=""))


def test_combine_filters_with_no_specs_matches_every_row() -> None:
    assert rv.combine_filters([])(row("R1"))


# --- running with --filter ------------------------------------------------------


def test_running_with_verify_no_only_visits_matching_rows(tmp_path) -> None:
    path = seed(
        tmp_path,
        row("R1", aid_verify="no", reviewer_note="n"),
        row("R2", aid_verify="yes", reviewer_note="n"),
        row("R3", aid_verify="no", reviewer_note="n"),
    )
    s = Script(keys=["s", "s"])
    rv.run(path, filters=["verify-no"], read_key=s.key, read_line=s.line, write=s.write)
    assert "done: 2 rows reviewed." in s.out
    on_disk = {r["record_id"]: r for r in read_csv(path)}
    assert on_disk["R2"]["approved"] == "false"  # never visited


def test_running_with_no_note_only_visits_unnoted_rows(tmp_path) -> None:
    path = seed(tmp_path, row("R1", reviewer_note="already reviewed"), row("R2", reviewer_note=""))
    s = Script(keys=["s"])
    rv.run(path, filters=["no-note"], read_key=s.key, read_line=s.line, write=s.write)
    assert "done: 1 rows reviewed." in s.out


def test_running_with_ids_only_visits_the_named_records(tmp_path) -> None:
    path = seed(tmp_path, row("R1"), row("R2"), row("R3"))
    s = Script(keys=["s"])
    rv.run(path, filters=["ids=R2"], read_key=s.key, read_line=s.line, write=s.write)
    assert "done: 1 rows reviewed." in s.out


def test_running_with_multiple_filters_ands_them(tmp_path) -> None:
    path = seed(
        tmp_path,
        row("R1", aid_verify="no", reviewer_note=""),
        row("R2", aid_verify="no", reviewer_note="already reviewed"),
        row("R3", aid_verify="yes", reviewer_note=""),
    )
    s = Script(keys=["s"])
    rv.run(path, filters=["verify-no", "no-note"], read_key=s.key, read_line=s.line, write=s.write)
    assert "done: 1 rows reviewed." in s.out


def test_rows_outside_the_filter_are_left_untouched(tmp_path) -> None:
    path = seed(tmp_path, row("R1", aid_verify="no", reviewer_note="n"), row("R2", aid_verify="yes", reviewer_note="n"))
    before = {r["record_id"]: r for r in read_csv(path)}
    s = Script(keys=["y"])
    rv.run(path, filters=["verify-no"], read_key=s.key, read_line=s.line, write=s.write)
    after = {r["record_id"]: r for r in read_csv(path)}
    assert after["R2"] == before["R2"]


def test_approving_a_filtered_row_with_an_empty_note_prompts_for_one(tmp_path) -> None:
    path = seed(tmp_path, row("R1", aid_verify="no", reviewer_note=""))
    s = Script(keys=["y"], lines=["verified against the 10-K"])
    rv.run(path, filters=["verify-no"], read_key=s.key, read_line=s.line, write=s.write)
    on_disk = read_csv(path)[0]
    assert (on_disk["approved"], on_disk["reviewer_note"]) == ("true", "verified against the 10-K")


def test_approving_a_filtered_row_with_an_existing_note_does_not_prompt(tmp_path) -> None:
    path = seed(tmp_path, row("R1", aid_verify="no", reviewer_note="already checked"))
    s = Script(keys=["y"])  # no lines queued: a prompt here would raise IndexError
    rv.run(path, filters=["verify-no"], read_key=s.key, read_line=s.line, write=s.write)
    on_disk = read_csv(path)[0]
    assert (on_disk["approved"], on_disk["reviewer_note"]) == ("true", "already checked")


def test_approving_without_any_filter_does_not_require_a_note(tmp_path) -> None:
    path = seed(tmp_path, row("R1", reviewer_note=""))
    s = Script(keys=["y"])  # no lines queued: a prompt here would raise IndexError
    rv.run(path, read_key=s.key, read_line=s.line, write=s.write)
    on_disk = read_csv(path)[0]
    assert (on_disk["approved"], on_disk["reviewer_note"]) == ("true", "")


# --- a proposed acknowledgement on an already-reviewed row ---------------------------------------

PROPOSED_URL = "https://www.sec.gov/Archives/edgar/data/123/y/ack.htm"
PROPOSED = {
    "ack_pending_review": "true", "aid_ack_proposed_at": "2026-05-20", "aid_ack_proposed_url": PROPOSED_URL,
    "aid_ack_proposed_excerpt": "Revenue of $4.8 billion was below our guidance.",
    "aid_ack_proposed_evidence": ('{"accession_number": "0000000123-26-000009", "filed_at": "2026-05-20", "content_sha256": "%s",'
                                  ' "source_url": "%s", "excerpt": "Revenue of $4.8 billion was below our guidance."}' % ("a" * 64, PROPOSED_URL)),
}


def pending(record_id="R1", **overrides):
    fields = {"approved": "true", "outcome.reported_value": "4.8", "outcome.reported_at": "2026-02-14", **PROPOSED, **overrides}
    return row(record_id, **fields)


def test_render_row_shows_a_proposed_acknowledgement_apart_from_the_real_one() -> None:
    out = rv.render_row(pending(), 1, 1, wrap=lambda s: f"[{s}]")
    block = out[out.index("ACKNOWLEDGEMENT (proposed, not yet reviewed)") :]
    assert "2026-05-20" in block and "[4.8] billion was below our guidance." in block and f"EDGAR: {PROPOSED_URL}" in block
    assert "acknowledgement: none" in out  # the real acknowledgement is still empty, and says so
    assert out.index("acknowledgement: none") < out.index("ACKNOWLEDGEMENT (proposed")


def test_render_row_has_no_proposed_block_when_nothing_is_pending() -> None:
    assert "proposed, not yet reviewed" not in rv.render_row(row("R1"), 1, 1)


def test_y_on_a_pending_row_copies_the_proposal_into_the_real_columns_and_clears_the_flag(tmp_path) -> None:
    path = seed(tmp_path, pending())
    s = Script(keys=["y"])
    rv.run(path, read_key=s.key, read_line=s.line, write=s.write)
    r = read_csv(path)[0]
    assert r["acknowledged_at"] == "2026-05-20" and r["acknowledgement_evidence.source_url"] == PROPOSED_URL
    assert r["acknowledgement_evidence.excerpt"] == "Revenue of $4.8 billion was below our guidance."
    assert r["acknowledgement_evidence.accession_number"] == "0000000123-26-000009"
    assert r["acknowledgement_evidence.content_sha256"] == "a" * 64
    assert r["days_to_acknowledged"] == "95"  # 2026-02-14 to 2026-05-20
    assert r["ack_pending_review"] == "false"
    assert all(r[c] == "" for c in ("aid_ack_proposed_at", "aid_ack_proposed_excerpt", "aid_ack_proposed_url", "aid_ack_proposed_evidence"))
    assert r["approved"] == "true"


def test_n_on_a_pending_row_clears_the_proposal_and_records_the_note_leaving_the_real_columns(tmp_path) -> None:
    path = seed(tmp_path, pending(reviewer_note="checked"))
    s = Script(keys=["n"], lines=["about a different quarter"])
    rv.run(path, read_key=s.key, read_line=s.line, write=s.write)
    r = read_csv(path)[0]
    assert r["ack_pending_review"] == "false" and r["aid_ack_proposed_at"] == "" and r["aid_ack_proposed_evidence"] == ""
    assert r["acknowledged_at"] == "" and r["acknowledgement_evidence.excerpt"] == ""
    assert r["approved"] == "true"
    assert r["reviewer_note"] == f"checked; acknowledgement proposal rejected (2026-05-20, {PROPOSED_URL}): about a different quarter"


def test_y_on_a_row_that_is_not_pending_still_just_approves(tmp_path) -> None:
    path = seed(tmp_path, row("R1"))
    rv.run(path, read_key=Script(keys=["y"]).key, read_line=Script().line, write=Script().write)
    r = read_csv(path)[0]
    assert r["approved"] == "true" and r["acknowledged_at"] == ""


def test_filter_ack_pending_shows_only_pending_rows(tmp_path) -> None:
    path = seed(tmp_path, row("R1"), pending("R2"), row("R3"))
    s = Script(keys=["s"])
    rv.run(path, filters=["ack-pending"], read_key=s.key, read_line=s.line, write=s.write)
    assert "done: 1 rows reviewed." in s.out
    assert not rv.parse_filter("ack-pending")(row("R1")) and rv.parse_filter("ack-pending")(pending())


def test_accept_without_the_evidence_json_still_fills_date_excerpt_and_url() -> None:
    r = rv.accept_acknowledgement(pending(aid_ack_proposed_evidence=""))
    assert (r["acknowledged_at"], r["acknowledgement_evidence.source_url"]) == ("2026-05-20", PROPOSED_URL)
    assert r["acknowledgement_evidence.excerpt"].startswith("Revenue of $4.8 billion")


# --- a proposed change on an already-touched row ---------------------------------------------------------------

review_step = importlib.import_module("pipeline.05_review")
CHANGE = {"assumption.target_low": "-146.0", "assumption.target_high": "-146.0", "aid_flag_note": "", "claim": "guides -146"}


def changed(record_id="R1", **overrides):
    fields = {"approved": "true", "assumption.target_low": "146.0", "assumption.target_high": "146.0", "claim": "guides 146",
              "aid_flag_note": "'tax rate' is a rate: read as positive", "change_pending_review": "true",
              "aid_proposed_change": json.dumps(CHANGE, sort_keys=True), **overrides}
    return row(record_id, **fields)


def test_render_row_shows_the_old_and_new_values_side_by_side_in_a_proposed_change_block() -> None:
    out = rv.render_row(changed(), 1, 1)
    block = out[out.index("PROPOSED CHANGE (not yet reviewed)") :]
    lines = block.splitlines()
    assert lines[1].split() == ["field", "now", "proposed"]
    target = next(l for l in lines if l.lstrip().startswith("assumption.target_low"))
    assert target.split() == ["assumption.target_low", "146.0", "-146.0"]
    flag = next(l for l in lines if l.lstrip().startswith("aid_flag_note"))
    assert "(blank)" in flag and "read as positive" in flag
    assert "target: 146.0 USD billions" in out  # the real columns still show the old values
    assert "PROPOSED CHANGE" not in rv.render_row(row("R1"), 1, 1)


def test_y_applies_the_proposal_to_the_real_columns_clears_it_and_notes_it(tmp_path) -> None:
    path = seed(tmp_path, changed(reviewer_note="checked"))
    s = Script(keys=["y"])
    rv.run(path, read_key=s.key, read_line=s.line, write=s.write)
    r = read_csv(path)[0]
    assert (r["assumption.target_low"], r["assumption.target_high"], r["aid_flag_note"], r["claim"]) == ("-146.0", "-146.0", "", "guides -146")
    assert r["change_pending_review"] == "false" and r["aid_proposed_change"] == "" and r["approved"] == "true"
    assert r["reviewer_note"].startswith("checked; proposed change applied [") and "assumption.target_low" in r["reviewer_note"]
    assert "[y] apply proposed change" in "\n".join(s.out)


def test_n_clears_the_proposal_records_the_reason_and_leaves_every_real_column(tmp_path) -> None:
    path = seed(tmp_path, changed())
    s = Script(keys=["n"], lines=["Salesforce prints a large provision in parentheses"])
    rv.run(path, read_key=s.key, read_line=s.line, write=s.write)
    r = read_csv(path)[0]
    assert r["change_pending_review"] == "false" and r["aid_proposed_change"] == ""
    assert (r["assumption.target_low"], r["claim"], r["approved"]) == ("146.0", "guides 146", "true")
    assert r["reviewer_note"] == f"proposed change rejected [{rv.change_hash(CHANGE)}]: Salesforce prints a large provision in parentheses"


def test_a_rejection_is_the_note_the_refresh_looks_for_so_the_same_change_is_not_proposed_again() -> None:
    assert rv.change_hash(CHANGE) == review_step.change_hash(CHANGE)  # the two modules name a change the same way
    rejected = rv.reject_change(changed(), "no")
    fresh = {c: "" for c in review_step.COLUMNS} | CHANGE | {"aid_proposed_status": "unresolved", "conflict": "false"}
    again = review_step.propose_change({c: rejected.get(c, "") for c in review_step.COLUMNS}, fresh)
    assert again["change_pending_review"] != "true"


def test_filter_change_pending_shows_only_those_rows(tmp_path) -> None:
    path = seed(tmp_path, row("R1"), changed("R2"), pending("R3"))
    s = Script(keys=["s"])
    rv.run(path, filters=["change-pending"], read_key=s.key, read_line=s.line, write=s.write)
    assert "done: 1 rows reviewed." in s.out
    assert rv.parse_filter("change-pending")(changed()) and not rv.parse_filter("change-pending")(pending())


def test_a_row_with_a_change_and_an_acknowledgement_pending_settles_the_change_then_stays_for_the_acknowledgement(tmp_path) -> None:
    both = changed(**{k: v for k, v in PROPOSED.items()}, **{"outcome.reported_value": "4.8", "outcome.reported_at": "2026-02-14"})
    path = seed(tmp_path, both)
    s = Script(keys=["y", "y"])
    rv.run(path, read_key=s.key, read_line=s.line, write=s.write)
    r = read_csv(path)[0]
    assert r["assumption.target_low"] == "-146.0" and r["acknowledged_at"] == "2026-05-20"
    assert r["change_pending_review"] == "false" and r["ack_pending_review"] == "false"
    shown = "\n".join(s.out)
    assert shown.count("PROPOSED CHANGE (not yet reviewed)") == 1 and shown.count("ACKNOWLEDGEMENT (proposed") == 2


def test_an_unreadable_proposal_applies_nothing_but_still_clears(tmp_path) -> None:
    path = seed(tmp_path, changed(aid_proposed_change="not json"))
    rv.run(path, read_key=Script(keys=["y"]).key, read_line=Script().line, write=Script().write)
    r = read_csv(path)[0]
    assert r["assumption.target_low"] == "146.0" and r["change_pending_review"] == "false"


# --- fast / slow ------------------------------------------------------------------

FAST_COLUMNS = COLUMNS + ["company", "aid_capture_method", "aid_lead_in"]


def frow(record_id, **overrides) -> dict[str, str]:
    base = {c: "" for c in FAST_COLUMNS}
    base.update(row(record_id), company="Example Corp", aid_capture_method="sentence", aid_verify="yes")
    base.update(overrides)
    return {c: base.get(c, "") for c in FAST_COLUMNS}


def fseed(tmp_path, *rows):
    path = tmp_path / "F.csv"
    write_csv(path, list(rows), fieldnames=FAST_COLUMNS)
    return path


def test_a_clean_sentence_row_is_fast_and_not_slow() -> None:
    r = frow("R1")
    assert rv.parse_filter("fast")(r) and not rv.parse_filter("slow")(r)


@pytest.mark.parametrize("overrides", [
    {"aid_capture_method": "section"},
    {"aid_verify": "no"},
    {"aid_verify": ""},
    {"aid_flag_note": "heading says gross margin"},
    {"conflict": "true"},
    {"empty_block": "true"},
    {"change_pending_review": "true", "aid_proposed_change": '{"status": "met"}'},
    {"ack_pending_review": "true"},
    {"outcome.reported_value": "4.0"},  # revenue below 5.0-6.0: a worse miss
])
def test_any_one_disqualifier_makes_a_row_slow_not_fast(overrides) -> None:
    r = frow("R1", **overrides)
    assert not rv.parse_filter("fast")(r) and rv.parse_filter("slow")(r)


def test_a_better_miss_or_a_hit_is_still_fast() -> None:
    assert rv.parse_filter("fast")(frow("R1", **{"outcome.reported_value": "7.0"}))  # above range, revenue: better
    assert rv.parse_filter("fast")(frow("R2", **{"outcome.reported_value": "5.5"}))  # inside range


def test_lower_is_better_metrics_flip_which_miss_is_worse() -> None:
    cost = {"assumption.metric": "operating expenses GAAP", "outcome.reported_value": "7.0"}  # above the range: worse
    assert rv.row_direction(frow("R1", **cost)) == "worse" and not rv.parse_filter("fast")(frow("R1", **cost))
    cheaper = {"assumption.metric": "operating expenses GAAP", "outcome.reported_value": "4.0"}
    assert rv.parse_filter("fast")(frow("R2", **cheaper))


def test_a_noted_row_is_neither_fast_nor_slow_unless_something_is_pending() -> None:
    r = frow("R1", reviewer_note="looked at it")
    assert not rv.parse_filter("fast")(r) and not rv.parse_filter("slow")(r)


def test_an_unknown_metric_with_an_outcome_is_not_fast() -> None:
    assert not rv.parse_filter("fast")(frow("R1", **{"assumption.metric": "no_such_metric", "outcome.reported_value": "7.0"}))


def test_approved_and_rejected_rows_are_neither_fast_nor_slow() -> None:
    for r in (frow("R1", approved="true"), frow("R2", approved="false", reviewer_note="wrong metric")):
        assert not rv.parse_filter("fast")(r) and not rv.parse_filter("slow")(r)


def test_a_rejected_row_with_a_pending_change_is_slow() -> None:
    r = frow("R1", reviewer_note="earlier note", change_pending_review="true", aid_proposed_change='{"status": "met"}')
    assert rv.parse_filter("slow")(r)


def test_fast_and_slow_partition_the_unreviewed_rows(tmp_path) -> None:
    rows = [frow("R1"), frow("R2", aid_verify="no"), frow("R3", approved="true"), frow("R4", conflict="true"), frow("R5")]
    fast = [r["record_id"] for r in rows if rv.parse_filter("fast")(r)]
    slow = [r["record_id"] for r in rows if rv.parse_filter("slow")(r)]
    assert (fast, slow) == (["R1", "R5"], ["R2", "R4"])


def test_bracket_targets_marks_only_the_target_numbers() -> None:
    text = "Revenue is expected to be $5.0 billion to $6.0 billion, plus or minus 2%, up from 4.2."
    assert rv.bracket_targets(text, "5.0", "6.0") == (
        "Revenue is expected to be $[5.0] billion to $[6.0] billion, plus or minus 2%, up from 4.2.")


def test_bracket_targets_matches_across_a_thousand_step_of_scale_and_commas() -> None:
    assert rv.bracket_targets("between $5,000 million and $6,000 million", "5.0", "6.0") == "between $[5,000] million and $[6,000] million"


def test_bracket_targets_with_no_target_leaves_the_text_alone() -> None:
    assert rv.bracket_targets("about 5 percent", "", "") == "about 5 percent"


def test_compact_render_is_header_lead_in_excerpt() -> None:
    r = frow("R1", aid_lead_in="For the fourth quarter of fiscal 2026, we expect:", aid_proposed_status="unresolved")
    out = rv.render_compact(r, 3, 9).split("\n")
    assert out == [
        "3/9  Example Corp | revenue | Q4 FY2026 | 5.0 to 6.0 USD billions | proposed: unresolved",
        "lead-in: For the fourth quarter of fiscal 2026, we expect:",
        "Revenue is expected to be $[5.0] billion to $[6.0] billion.",
    ]


def test_compact_render_omits_an_absent_lead_in_and_falls_back_to_ticker() -> None:
    out = rv.render_compact(frow("R1", company=""), 1, 1).split("\n")
    assert len(out) == 2 and out[0].startswith("1/1  EXMP | revenue")


def test_fast_mode_shows_the_compact_form_with_the_key_prompt(tmp_path) -> None:
    path = fseed(tmp_path, frow("R1"))
    s = Script(keys=["s"])
    rv.run(path, filters=["fast"], read_key=s.key, read_line=s.line, write=s.write)
    assert "$[5.0] billion" in s.out[0] and "EDGAR:" not in s.out[0] and s.out[1] == rv.PROMPT


def test_fast_mode_y_approves_without_asking_for_a_note(tmp_path) -> None:
    path = fseed(tmp_path, frow("R1"), frow("R2"))
    s = Script(keys=["y", "y"])  # no lines queued: a note prompt would raise IndexError
    rv.run(path, filters=["fast"], read_key=s.key, read_line=s.line, write=s.write)
    assert [(r["approved"], r["reviewer_note"]) for r in read_csv(path)] == [("true", ""), ("true", "")]


def test_fast_mode_n_still_asks_for_a_reason_and_e_still_edits(tmp_path) -> None:
    path = fseed(tmp_path, frow("R1"))
    s = Script(keys=["e", "n"], lines=["assumption.unit", "USD millions", "wrong unit"])
    rv.run(path, filters=["fast"], read_key=s.key, read_line=s.line, write=s.write)
    on_disk = read_csv(path)[0]
    assert (on_disk["approved"], on_disk["reviewer_note"], on_disk["assumption.unit"]) == ("false", "wrong unit", "USD millions")


def test_fast_mode_only_visits_fast_rows(tmp_path) -> None:
    path = fseed(tmp_path, frow("R1", aid_verify="no"), frow("R2"), frow("R3", approved="true"))
    s = Script(keys=["y"])
    rv.run(path, filters=["fast"], read_key=s.key, read_line=s.line, write=s.write)
    assert [r["approved"] for r in read_csv(path)] == ["false", "true", "true"]


def test_slow_mode_uses_the_full_view_and_still_requires_a_note(tmp_path) -> None:
    path = fseed(tmp_path, frow("R1"), frow("R2", aid_verify="no"))
    s = Script(keys=["y"], lines=["checked on EDGAR"])
    rv.run(path, filters=["slow"], read_key=s.key, read_line=s.line, write=s.write)
    assert "excerpt:" in s.out[0] and s.out[0].startswith("── 1/1 ──")
    assert [(r["approved"], r["reviewer_note"]) for r in read_csv(path)] == [("false", ""), ("true", "checked on EDGAR")]


def test_fast_mode_keeps_the_every_tenth_hand_verify_stop(tmp_path) -> None:
    path = fseed(tmp_path, *[frow(f"R{n}") for n in range(10)])
    s = Script(keys=["y"] * 10, lines=["y"])
    rv.run(path, filters=["fast"], read_key=s.key, read_line=s.line, write=s.write)
    rows = read_csv(path)
    assert [r["hand_verified"] for r in rows] == ["false"] * 9 + ["true"]
    assert any("SCOPE 4.3" in m for m in s.out)
