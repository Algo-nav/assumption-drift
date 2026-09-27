"""03c_suggest.py: a suggested reviewer note for rows 03b_verify flagged "no"."""

from __future__ import annotations

import importlib

import pytest
import yaml

from pipeline import common

suggest = importlib.import_module("pipeline.03c_suggest")
review = importlib.import_module("pipeline.05_review")
candidates = importlib.import_module("pipeline.02_candidates")

COMPANY = common.Company("Example Corp", "EXMP", "0000000123", fiscal_year_end_month=1)

CFG = {
    "forward_terms": ["expect(?:s|ed|ing)?", "guidance", "outlook"],
    "number_units": ["percent", "million", "billion"],
}
FORWARD, NUMBER = candidates.compile_matchers(CFG)


def row(record_id="R1", metric="revenue", low="5.0", high="6.0", unit="USD billions", period="Q4 FY2026",
        excerpt="Revenue is expected to be $5.0 billion to $6.0 billion.", aid_verify="no",
        aid_verify_class="other", reviewer_note="", **overrides) -> dict[str, str]:
    base = {c: "" for c in review.COLUMNS}
    base.update({
        "record_id": record_id, "company": COMPANY.name, "ticker": COMPANY.ticker, "cik": COMPANY.cik,
        "assumption.metric": metric, "assumption.target_low": low, "assumption.target_high": high,
        "assumption.unit": unit, "assumption.target_period": period, "assumption.evidence.excerpt": excerpt,
        "approved": "false", "hand_verified": "false", "reviewer_note": reviewer_note, "conflict": "false",
        "empty_block": "false", "aid_verify": aid_verify, "aid_verify_class": aid_verify_class,
    })
    base.update(overrides)
    return base


# --- which rows qualify ---------------------------------------------------------


@pytest.mark.parametrize(
    "changes, expected",
    [
        ({}, True),
        ({"aid_verify": "yes"}, False),
        ({"aid_verify": ""}, False),
        ({"reviewer_note": "already checked"}, False),
        ({"reviewer_note": "   "}, True),  # whitespace-only is still "empty"
    ],
)
def test_qualifies(changes, expected) -> None:
    assert suggest.qualifies(row(**changes)) is expected


# --- wrong_period ------------------------------------------------------------


def test_wrong_period_false_alarm_from_a_pipe_joined_header() -> None:
    r = row(period="Q1 FY2022", aid_table_header="Q1 FY22 | Guidance Full Year FY22")
    assert suggest.suggest_wrong_period(r) == "false alarm: period from header column 1"


def test_wrong_period_false_alarm_from_the_second_pipe_joined_column() -> None:
    r = row(period="FY2022", aid_table_header="Q1 FY22 | Guidance Full Year FY22")
    assert suggest.suggest_wrong_period(r) == "false alarm: period from header column 2"


def test_wrong_period_false_alarm_from_a_single_line_header() -> None:
    r = row(period="Q2 2023", aid_table_header="Q2 2023 Full Year 2023")
    assert suggest.suggest_wrong_period(r) == "false alarm: period from header column 1"
    r2 = row(period="FY2023", aid_table_header="Q2 2023 Full Year 2023")
    assert suggest.suggest_wrong_period(r2) == "false alarm: period from header column 2"


def test_wrong_period_check_when_there_is_no_header() -> None:
    assert suggest.suggest_wrong_period(row(period="Q1 FY2022", aid_table_header="")) == "CHECK: period"


def test_wrong_period_check_when_the_period_is_not_among_the_headers_labels() -> None:
    r = row(period="Q3 FY2022", aid_table_header="Q1 FY22 | Guidance Full Year FY22")
    assert suggest.suggest_wrong_period(r) == "CHECK: period"


# --- wrong_value ---------------------------------------------------------------


def test_wrong_value_false_alarm_when_plus_minus_expands_to_the_stored_range() -> None:
    r = row(low="20.5", high="21.5", excerpt="Revenue of $21.00, plus or minus $0.50.")
    assert suggest.suggest_wrong_value(r) == "false alarm: ± expands to 20.5-21.5"


def test_wrong_value_recognises_the_slash_and_symbol_spellings() -> None:
    assert suggest.suggest_wrong_value(row(low="18", high="22", excerpt="Growth of 20%, +/- 2%.")) == "false alarm: ± expands to 18-22"
    assert suggest.suggest_wrong_value(row(low="18", high="22", excerpt="Growth of 20%, ± 2%.")) == "false alarm: ± expands to 18-22"


def test_wrong_value_check_when_the_expansion_does_not_match_the_stored_range() -> None:
    r = row(low="20.5", high="21.5", excerpt="Revenue of $21.00, plus or minus $2.00.")
    assert suggest.suggest_wrong_value(r) == "CHECK: value"


def test_wrong_value_check_when_the_excerpt_has_no_plus_minus_phrasing() -> None:
    r = row(low="34.0", high="35.0", excerpt="Long range target for FY24 high end of range $34B to $35B.")
    assert suggest.suggest_wrong_value(r) == "CHECK: value"


# --- wrong_metric --------------------------------------------------------------


def test_wrong_metric_false_alarm_when_the_heading_names_it() -> None:
    assert suggest.suggest_wrong_metric(row(metric="gross margin", aid_heading="Gross Margin Guidance")) == "false alarm: metric in heading"


def test_wrong_metric_false_alarm_when_the_lead_in_names_it() -> None:
    assert suggest.suggest_wrong_metric(row(metric="operating margin", aid_lead_in="Operating margin outlook:")) == "false alarm: metric in heading"


def test_wrong_metric_check_when_neither_names_it() -> None:
    r = row(metric="gross margin", aid_heading="Q4 FY26 Guidance", aid_lead_in="Our outlook is as follows:")
    assert suggest.suggest_wrong_metric(r) == "CHECK: metric"


def test_wrong_metric_needs_every_word_of_the_metric() -> None:
    r = row(metric="gross margin", aid_heading="Gross Guidance")  # "margin" is missing
    assert suggest.suggest_wrong_metric(r) == "CHECK: metric"


# --- not_guidance ----------------------------------------------------------------


def test_not_guidance_false_alarm_when_verb_period_and_number_are_all_present() -> None:
    r = row(excerpt="We expect Q1 FY2027 revenue of approximately $2 billion.")
    assert suggest.suggest_not_guidance(r, FORWARD, NUMBER) == "false alarm: forward verb, period, number present"


def test_not_guidance_check_when_it_is_a_past_result_with_no_forward_verb() -> None:
    r = row(excerpt="Q1 FY2027 revenue was $2 billion.")
    assert suggest.suggest_not_guidance(r, FORWARD, NUMBER) == "CHECK: guidance"


def test_not_guidance_check_when_there_is_no_period() -> None:
    r = row(excerpt="We expect revenue of approximately $2 billion.")
    assert suggest.suggest_not_guidance(r, FORWARD, NUMBER) == "CHECK: guidance"


def test_not_guidance_check_when_there_is_no_number() -> None:
    r = row(excerpt="We expect strong growth in Q1 FY2027.")
    assert suggest.suggest_not_guidance(r, FORWARD, NUMBER) == "CHECK: guidance"


# --- wrong_sign, other, and a blank class ---------------------------------------


@pytest.mark.parametrize("klass, expected", [("wrong_sign", "CHECK: wrong_sign"), ("other", "CHECK: other"), ("", "CHECK: other")])
def test_classes_with_nothing_to_recompute_are_always_check(klass, expected) -> None:
    assert suggest.suggest_note(row(aid_verify_class=klass), FORWARD, NUMBER) == expected


# --- dispatch ----------------------------------------------------------------------


def test_suggest_note_dispatches_on_the_verify_class() -> None:
    r = row(aid_verify_class="wrong_metric", metric="revenue", aid_heading="Revenue Guidance")
    assert suggest.suggest_note(r, FORWARD, NUMBER) == "false alarm: metric in heading"


# --- a company's rows: fills qualifying rows, clears the rest -------------------


def test_suggest_company_fills_qualifying_rows_and_clears_the_rest() -> None:
    rows = [
        row("R1", aid_verify="no", aid_verify_class="wrong_metric", metric="revenue", aid_heading="Revenue Guidance"),
        row("R2", aid_verify="yes", aid_verify_class="other"),
        row("R3", aid_verify="no", aid_verify_class="wrong_sign", reviewer_note="already looked"),
        row("R4", aid_verify="no", aid_verify_class="other"),
    ]
    updated, false_alarms, checks = suggest.suggest_company(rows, FORWARD, NUMBER)
    by_id = {r["record_id"]: r for r in updated}
    assert by_id["R1"]["aid_suggested_note"] == "false alarm: metric in heading"
    assert by_id["R2"]["aid_suggested_note"] == ""  # aid_verify is "yes": not touched
    assert by_id["R3"]["aid_suggested_note"] == ""  # already has a reviewer_note
    assert by_id["R4"]["aid_suggested_note"] == "CHECK: other"
    assert (false_alarms, checks) == (1, 1)


def test_suggest_company_clears_a_stale_suggestion_once_a_note_is_added() -> None:
    stale = row("R1", aid_verify="no", aid_verify_class="wrong_sign", reviewer_note="checked, it's fine",
                aid_suggested_note="CHECK: wrong_sign")
    updated, false_alarms, checks = suggest.suggest_company([stale], FORWARD, NUMBER)
    assert updated[0]["aid_suggested_note"] == ""
    assert (false_alarms, checks) == (0, 0)


def test_suggest_company_never_touches_verify_or_reviewer_columns() -> None:
    r = row("R1", approved="true", reviewer_note="", hand_verified="true")
    updated, _, _ = suggest.suggest_company([r], FORWARD, NUMBER)
    for key in ("aid_verify", "aid_verify_reason", "aid_verify_class", "approved", "hand_verified", "reviewer_note"):
        assert updated[0][key] == r[key]


# --- main, end to end --------------------------------------------------------


@pytest.fixture
def world(tmp_path, monkeypatch):
    (tmp_path / "review").mkdir()
    monkeypatch.setattr(suggest, "REVIEW_DIR", tmp_path / "review")
    return tmp_path


@pytest.fixture
def config_path(world):
    cfg = common.load_config()
    cfg["companies"] = [{"name": COMPANY.name, "ticker": COMPANY.ticker, "cik": COMPANY.cik, "fiscal_year_end_month": 1}]
    path = world / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


def seed(world, rows):
    review.write_csv(world / "review" / f"{COMPANY.cik}.csv", rows)


def read_csv(world):
    return review.read_csv(world / "review" / f"{COMPANY.cik}.csv")


def test_main_writes_suggestions_and_prints_the_counts(world, config_path, capsys) -> None:
    seed(world, [
        row("R1", aid_verify="no", aid_verify_class="wrong_metric", metric="revenue", aid_heading="Revenue Guidance"),
        row("R2", aid_verify="no", aid_verify_class="wrong_sign"),
        row("R3", aid_verify="yes", aid_verify_class="other"),
    ])
    assert suggest.main(["--config", str(config_path)]) == 0
    on_disk = {r["record_id"]: r for r in read_csv(world)}
    assert on_disk["R1"]["aid_suggested_note"] == "false alarm: metric in heading"
    assert on_disk["R2"]["aid_suggested_note"] == "CHECK: wrong_sign"
    assert on_disk["R3"]["aid_suggested_note"] == ""
    out = capsys.readouterr().out
    assert "EXMP: 1 false alarm, 1 CHECK" in out


def test_main_skips_a_company_with_no_review_file(world, config_path) -> None:
    assert suggest.main(["--config", str(config_path)]) == 0  # no file seeded: nothing to do, no crash


def test_main_with_a_company_filter_only_touches_that_company(world, config_path) -> None:
    other = world / "review" / "0000000456.csv"
    review.write_csv(other, [row("R1", aid_verify="no", aid_verify_class="other")])
    seed(world, [row("R1", aid_verify="no", aid_verify_class="other")])
    cfg = yaml.safe_load(config_path.read_text())
    cfg["companies"].append({"name": "Other Corp", "ticker": "OTHR", "cik": "0000000456"})
    config_path.write_text(yaml.safe_dump(cfg))
    assert suggest.main(["--config", str(config_path), "--company", "EXMP"]) == 0
    assert review.read_csv(other)[0]["aid_suggested_note"] == ""  # OTHR was not targeted
    assert read_csv(world)[0]["aid_suggested_note"] == "CHECK: other"
