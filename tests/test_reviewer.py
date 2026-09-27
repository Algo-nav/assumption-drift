"""research_record/reviewer.py: the terminal, one-row-at-a-time review tool `rr review` drives."""

from __future__ import annotations

import csv

import pytest

from research_record import reviewer as rv

COLUMNS = [
    "record_id", "ticker", "assumption.metric", "assumption.target_period", "assumption.stated_at",
    "assumption.target_low", "assumption.target_high", "assumption.unit",
    "assumption.evidence.source_url", "assumption.evidence.excerpt",
    "outcome.reported_value", "outcome.reported_at", "outcome.evidence.excerpt",
    "approved", "hand_verified", "reviewer_note", "conflict", "empty_block",
    "aid_proposed_status", "aid_outcome_note", "aid_flag_note", "aid_verify", "aid_verify_reason",
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
