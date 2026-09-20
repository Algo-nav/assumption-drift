"""The five fixes from the third pilot review, each tested against the pilot rows that called for it.

The fixture, fixtures/pilot_run3.json, is the 30-request pilot as it ran after the seven fixes of the second review:
the candidates that were sent (with their `table_header` as it was then), the model's raw answer to each, the drafts and
rejects 03 wrote, the 14 lines above every section block in the cached filing, and `wrong`, an audit of the drafts
against the filings: eight Salesforce rows from one table whose header the round-2 rule could not find, and Target's
"$1.30+" read as a point. `caveats` are correct rows with a known limitation.

The `main()`-level tests (dry runs write nothing, empty_blocks.jsonl, temperature) are in test_structure.py, the CSV
tests in test_review.py and the temperature tests on the client in test_llm.py, where their fixtures are.
Nothing here calls a model or touches data/.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from pipeline import llm
from research_record.schema import Evidence

structure = importlib.import_module("pipeline.03_structure")
candidates = importlib.import_module("pipeline.02_candidates")
review = importlib.import_module("pipeline.05_review")
common = importlib.import_module("pipeline.common")

PILOT = json.loads((Path(__file__).parent / "fixtures" / "pilot_run3.json").read_text(encoding="utf-8"))
CONFIG = common.load_config()
METRICS, KINDS = CONFIG["metrics"], CONFIG["metric_kinds"]
COMPANIES = {c.ticker: c for c in common.companies(CONFIG)}
NVDA, TGT, CRM = COMPANIES["NVDA"], COMPANIES["TGT"], COMPANIES["CRM"]
CANDS = {c["custom_id"]: c for c in PILOT["candidates"]}
WRONG = PILOT["wrong"]
SETTINGS = candidates.Settings.from_config(CONFIG["candidates"])


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Evidence normally comes from the cached filing's sidecar. Here it is made up, and valid."""
    monkeypatch.setattr(structure, "evidence_for", lambda company, cand, excerpt: Evidence(
        source_url="https://www.sec.gov/Archives/edgar/data/1/x/x.htm", accession_number=cand["accession"], filing_type="8-K",
        filed_at=date.fromisoformat(cand["filed_at"]), fetched_at=datetime(2026, 9, 20, tzinfo=timezone.utc),
        content_sha256="a" * 64, excerpt=excerpt))


def strip(c: dict) -> dict:
    return {k: v for k, v in c.items() if k not in ("ticker", "custom_id")}


def replay(cid: str, answer: dict | None = None):
    """One pilot request pushed back through derive_drafts. `answer` replaces the model's raw answer."""
    company = COMPANIES[CANDS[cid]["ticker"]]
    text = PILOT["raw"][cid] if answer is None else json.dumps(answer)
    drafts, rejects, _ = structure.derive_drafts({cid: (company, strip(CANDS[cid]))}, {cid: llm.Result(cid, "succeeded", text, 1, 1, None, "b")}, METRICS, KINDS)
    return drafts.get(company.cik, []), rejects.get(company.cik, [])


def replay_all():
    index = {c["custom_id"]: (COMPANIES[c["ticker"]], strip(c)) for c in PILOT["candidates"]}
    results = {cid: llm.Result(cid, "succeeded", text, 1, 1, None, "b") for cid, text in PILOT["raw"].items()}
    return index, results, *structure.derive_drafts(index, results, METRICS, KINDS)


def section_cid(ticker: str, filed_at: str) -> str:
    return next(c["custom_id"] for c in PILOT["candidates"] if c["ticker"] == ticker and c["filed_at"] == filed_at and c["capture_method"] == "section")


def header(lines: list[str], filed: str | None = None, company=None, block_start: int | None = None) -> str | None:
    """find_table_header on `lines`, the block starting right after them (or at `block_start`)."""
    return candidates.find_table_header(lines, len(lines) if block_start is None else block_start, SETTINGS,
                                        date.fromisoformat(filed) if filed else None, company)


# --- the fixture -------------------------------------------------------------


def test_the_fixture_is_the_thirty_request_pilot_after_the_second_review() -> None:
    assert len(PILOT["candidates"]) == 30 and set(PILOT["raw"]) == set(CANDS) and len(PILOT["drafts"]) == 75
    assert len(WRONG) == 9 and sorted({w["fixes"][0] for w in WRONG}) == [1, 2]
    assert sum(w["fixes"] == [1] for w in WRONG) == 8 and sum(w["fixes"] == [2] for w in WRONG) == 1
    assert len(PILOT["caveats"]) == 6
    frozen = {(d["custom_id"], d["assumption"]["metric"], d["assumption"]["target_period"]) for d in PILOT["drafts"]}
    assert all((w["custom_id"], w["metric"], w["target_period"]) in frozen for w in WRONG)


# --- fix 1: header gathering, and the guards that keep out a table of results ----------------------


def test_salesforces_split_header_is_gathered_in_order_from_the_real_lines() -> None:
    cid = section_cid("CRM", "2020-12-01")
    above = PILOT["lines_above"][cid]
    assert above[-7:] == ["Q4 FY21", "Guidance", "Full Year FY21", "Guidance", "Q1 FY22 Guidance", "Full Year FY22", "Guidance"]
    assert header(above, "2020-12-01", CRM) == "Q4 FY21 | Full Year FY21 | Q1 FY22 Guidance | Full Year FY22"
    assert header(above) == header(above, "2020-12-01", CRM)  # with no calendar the date check is skipped; this one passes it anyway


def test_targets_split_header_is_gathered_too() -> None:
    above = PILOT["lines_above"][section_cid("TGT", "2026-03-03")]
    assert header(above, "2026-03-03", TGT) == "Q1 2026 | Full Year 2026"
    assert CANDS[section_cid("TGT", "2026-03-03")]["table_header"] is None  # the pilot as it ran had none: the one-line rule could not find it


def test_the_pilots_salesforce_rows_all_come_from_the_block_that_now_has_a_header() -> None:
    cid = section_cid("CRM", "2020-12-01")
    assert {w["custom_id"] for w in WRONG if w["fixes"] == [1]} == {cid}
    assert CANDS[cid]["table_header"] is None  # what was sent in the pilot: no header, and the model took columns 1 and 2 for Q1 FY22 and FY22
    found = header(PILOT["lines_above"][cid], "2020-12-01", CRM)
    system, user = structure.system_prompt(METRICS, block=True), structure.build_block_prompt({**strip(CANDS[cid]), "table_header": found}, CRM, None)
    assert "  [header] Q4 FY21 | Full Year FY21 | Q1 FY22 Guidance | Full Year FY22\n  [1] Revenue\n" in user and "[header]" not in structure.build_block_prompt(strip(CANDS[cid]), CRM, None)
    assert 'joined with " | "' in system


def test_a_line_naming_two_periods_still_wins_over_gathering_the_cells() -> None:
    assert header(["Q1 FY27", "Full Year FY27", "Q2 2023 Full Year 2023", "Guidance"]) == "Q2 2023 Full Year 2023"


def test_cells_are_gathered_only_from_the_ten_lines_above_the_block() -> None:
    assert header(["Q1 FY27", "Full Year FY27"] + ["filler"] * 8) == "Q1 FY27 | Full Year FY27"  # ten and nine lines up
    assert header(["Q1 FY27", "Full Year FY27"] + ["filler"] * 9) is None  # eleven and ten: one cell is left, and one is not a header


def test_one_period_is_not_a_header_and_neither_is_a_prose_line() -> None:
    assert header(["Full Year FY23", "Guidance"]) is None
    assert header(["Q4 FY21", "For the fourth quarter of fiscal 2021 the company expects revenue of $5.7 billion, and for the year FY21 $21.1 billion."]) is None


def test_the_cells_are_kept_in_document_order_and_fillers_are_left_out() -> None:
    lines = ["Q4 FY20", "Guidance Full Year FY20", "Guidance Q1 FY21", "Guidance Full Year FY21", "Guidance", "(unaudited)"]
    assert header(lines) == "Q4 FY20 | Guidance Full Year FY20 | Guidance Q1 FY21 | Guidance Full Year FY21"


# the two guards asked for ---------------------------------------------------------------------------


@pytest.mark.parametrize("line", [
    "Q4 FY23 Q4 FY22 Y/Y", "Q4 FY23 Q4 FY22 y/y", "Q4 FY23 vs Q4 FY22", "Q4 FY23 VS. Q4 FY22", "Q4 FY23 Q4 FY22 Change", "Q4 FY23 Q4 FY22 % Change",
    "Q4 FY23 Q4 FY22 (5%)", "($ in millions, except earnings per share) FY23 FY22 Y/Y",
])
def test_a_line_with_yy_vs_change_or_a_percent_is_never_a_header_line(line) -> None:
    assert header([line]) is None


@pytest.mark.parametrize("text, rejected", [
    ("Y/Y", True), ("y/y", True), ("Y/Y Growth", True), ("vs", True), ("VS.", True), ("Q4 FY23 vs Q4 FY22", True), ("Change", True), ("% Change", True),
    ("(5%)", True), ("74.9 %", True), ("%", True),
    ("exchange", False), ("Q1 2026", False), ("Full Year 2026", False), ("Guidance", False), ("Previous Q4 Fiscal 2019 Guidance", False), ("versus", False),
])
def test_the_line_guard_itself_matches_yy_vs_change_and_percent_and_nothing_else(text, rejected) -> None:
    """The guard asked for. It is tested on its own because the bare-label rule below also turns away most such lines
    (a "Y/Y" or "change" is not a word that may sit beside a period label), leaving a bare "%" as the case only this one catches."""
    assert bool(candidates._NOT_A_HEADER.search(text)) is rejected


def test_a_bare_percent_sign_is_turned_away_by_the_line_guard_alone() -> None:
    assert candidates._header_cell("Q4 FY23 Q4 FY22 Guidance", SETTINGS) and not candidates._header_cell("Q4 FY23 Q4 FY22 %", SETTINGS)
    assert header(["Q4 FY23 Q4 FY22 %"]) is None and header(["Q4 FY25", "Q4 FY24 %", "Q1 FY26"]) == "Q4 FY25 | Q1 FY26"


def test_the_same_labels_without_those_words_are_a_header() -> None:
    assert header(["Q4 FY23 Q4 FY22"]) == "Q4 FY23 Q4 FY22" and header(["Q4 FY23 Q4 FY22 Guidance"]) == "Q4 FY23 Q4 FY22 Guidance"


def test_a_cell_that_says_yy_or_change_is_left_out_of_a_gathered_header() -> None:
    assert header(["Q4 FY25", "Q4 FY24 Y/Y", "Q1 FY26"]) == "Q4 FY25 | Q1 FY26"
    assert header(["Q4 FY25", "Change", "Q4 FY24 % change"]) is None


def test_the_nvidia_results_headers_that_the_round_2_rule_let_through_are_rejected() -> None:
    assert header(["($ in millions, except earnings per share) FY23 FY22 Y/Y"], "2023-02-22", NVDA) is None
    assert header(["($ in millions, except earnings per share) FY26 FY25 Y/Y"], "2026-02-25", NVDA) is None


def test_a_header_naming_only_finished_periods_is_a_table_of_results() -> None:
    """NVIDIA fiscal 2023 ended in January 2023, so on 2023-02-22 FY23 and FY22 are both over."""
    assert header(["FY23", "FY22"]) == "FY23 | FY22"  # without a calendar there is nothing to date them by
    assert header(["FY23", "FY22"], "2023-02-22", NVDA) is None
    assert header(["Q4 FY23", "Q3 FY23", "Q4 FY22"], "2023-02-22", NVDA) is None  # one label per line: the same, gathered
    assert header(["Q4 FY23 Q3 FY23"], "2023-02-22", NVDA) is None  # and on one line, with no Y/Y to give it away: only the date does
    assert header(["Q4 FY23 Q3 FY23"]) == "Q4 FY23 Q3 FY23"


def test_the_date_guard_rejects_only_when_every_period_is_over() -> None:
    assert header(["Q3 FY21", "Q4 FY21"], "2020-12-01", CRM) == "Q3 FY21 | Q4 FY21"  # Q3 ended in October, Q4 ends in January
    assert header(["FY23", "FY24"], "2023-02-22", NVDA) == "FY23 | FY24"
    assert header(["FY23", "FY22"], "2022-02-22", NVDA) == "FY23 | FY22"  # a year earlier, FY23 had not ended


def test_the_date_guard_needs_the_naming_as_well_as_the_month() -> None:
    """Target's fiscal 2023 STARTS in 2023. Read as ending in it, the header of the 2023-05-17 guidance table names only finished periods."""
    assert header(["Q2 2023 Full Year 2023"], "2023-05-17", TGT) == "Q2 2023 Full Year 2023"
    assert header(["Q2 2023 Full Year 2023"], "2023-05-17", dataclasses.replace(TGT, fiscal_year_named_for="end")) is None
    assert header(["Q2 2023 Full Year 2023"], "2023-05-17", NVDA) is None  # NVIDIA names its years the other way round


def test_a_company_with_no_fiscal_calendar_skips_the_date_guard() -> None:
    assert header(["FY23", "FY22"], "2023-02-22", common.Company("Example Corp", "EXMP", "0000000123")) == "FY23 | FY22"


def test_the_pilots_real_headers_pass_both_guards() -> None:
    for ticker, filed in (("TGT", "2023-05-17"), ("TGT", "2024-05-22")):
        cid = section_cid(ticker, filed)
        above = PILOT["lines_above"][cid]
        assert header(above, filed, COMPANIES[ticker]) == CANDS[cid]["table_header"]
    assert CANDS[section_cid("TGT", "2023-05-17")]["table_header"] == "Q2 2023 Full Year 2023"


# the one more that the corpus asked for ---------------------------------------------------------------


@pytest.mark.parametrize("stray", [
    "https://corporate.target.com/article/2020/03/q4-fy2019-earnings",
    "Salesforce Announces First Quarter Fiscal 2025 Results",
    "FY25 Results",
    "•Expects organic revenue re-acceleration in the second half of FY27",
    "Fiscal 2022 Guidance and Quarterly Commentary",
])
def test_a_title_a_bullet_or_a_url_beside_the_heading_is_not_a_header(stray) -> None:
    """Each of these was found across the corpus, gathered with the block's own heading, before cells had to be bare period labels."""
    assert candidates.parse_periods(stray) and header([stray, "Q1 FY27 Guidance"]) is None


def test_a_cell_is_a_period_label_and_words_like_guidance() -> None:
    for cell in ("Q4 FY20", "Guidance Full Year FY20", "Previous Q4 Fiscal 2019 Guidance", "Updated Q4 Fiscal 2019 Guidance", "Q1 2026", "Full Year 2026"):
        assert header([cell, "Q1 FY27"]) == f"{cell} | Q1 FY27", cell
    assert header(["Previous Q4 Fiscal 2019 Guidance", "Updated Q4 Fiscal 2019 Guidance"]) == "Previous Q4 Fiscal 2019 Guidance | Updated Q4 Fiscal 2019 Guidance"


# through the candidate pass ---------------------------------------------------------------------------

META = {"cik": "0001108524", "accession": "0001193125-20-307200", "filing_type": "8-K", "filed_at": "2020-12-01"}
SALESFORCE_LIKE = (b"<html><body><p>Management will provide further commentary on these assumptions on its earnings call.</p>"
                   b"<p>Q4 FY21</p><p>Guidance</p><p>Full Year FY21</p><p>Guidance</p><p>Q1 FY22 Guidance</p><p>Full Year FY22</p><p>Guidance</p>"
                   b"<p>Revenue</p><p>$5.665 - $5.675</p><p>Billion</p><p>$21.10 - $21.11</p><p>Billion</p></body></html>")


def test_a_section_row_carries_the_gathered_header_and_the_block_itself_is_unchanged() -> None:
    rows, _ = candidates.candidates_for_document(META, SALESFORCE_LIKE, SETTINGS, CRM)
    (section,) = [r for r in rows if r["capture_method"] == "section"]
    assert section["table_header"] == "Q4 FY21 | Full Year FY21 | Q1 FY22 Guidance | Full Year FY22"
    assert section["sentence"].split("\n")[0] == "Revenue" and section["heading"] == "Guidance"


def test_the_same_table_gets_no_header_once_every_period_in_it_is_over() -> None:
    later = {**META, "filed_at": "2021-03-05"}  # Q4 FY21 ended January 31, 2021; Q1 FY22 ends April 30
    (section,) = [r for r in candidates.candidates_for_document(later, SALESFORCE_LIKE, SETTINGS, CRM)[0] if r["capture_method"] == "section"]
    assert section["table_header"] == "Q4 FY21 | Full Year FY21 | Q1 FY22 Guidance | Full Year FY22"  # Q1 FY22 and FY22 are still to come
    much_later = {**META, "filed_at": "2022-06-01"}
    (section,) = [r for r in candidates.candidates_for_document(much_later, SALESFORCE_LIKE, SETTINGS, CRM)[0] if r["capture_method"] == "section"]
    assert section["table_header"] is None  # every one of those periods was over


def test_without_a_company_the_candidate_pass_still_works_and_skips_the_date_check() -> None:
    much_later = {**META, "filed_at": "2022-06-01"}
    (section,) = [r for r in candidates.candidates_for_document(much_later, SALESFORCE_LIKE, SETTINGS)[0] if r["capture_method"] == "section"]
    assert section["table_header"] == "Q4 FY21 | Full Year FY21 | Q1 FY22 Guidance | Full Year FY22"


# the fiscal calendar itself ---------------------------------------------------------------------------


@pytest.mark.parametrize("company, year, quarter, expected", [
    (CRM, 2021, 4, date(2021, 1, 31)), (CRM, 2022, 1, date(2021, 4, 30)), (CRM, 2022, None, date(2022, 1, 31)),
    (NVDA, 2023, None, date(2023, 1, 31)), (NVDA, 2024, 3, date(2023, 10, 31)),
    (TGT, 2023, 2, date(2023, 7, 31)), (TGT, 2023, None, date(2024, 1, 31)), (TGT, 2026, 1, date(2026, 4, 30)),
    (common.Company("Dec", "DEC", "1", 12), 2024, 1, date(2024, 3, 31)), (common.Company("Dec", "DEC", "1", 12), 2024, None, date(2024, 12, 31)),
    (common.Company("Jun", "JUN", "1", 6), 2025, 4, date(2025, 6, 30)), (common.Company("Jun", "JUN", "1", 6), 2025, 1, date(2024, 9, 30)),
])
def test_a_period_ends_in_the_last_month_of_its_quarter(company, year, quarter, expected) -> None:
    assert company.period_end(year, quarter) == expected


def test_a_company_with_no_fiscal_year_end_month_has_no_period_dates() -> None:
    assert common.Company("Example Corp", "EXMP", "0000000123").period_end(2024, 1) is None


@pytest.mark.parametrize("entry", [{"fiscal_year_end_month": 13}, {"fiscal_year_end_month": 0}, {"fiscal_year_end_month": "January"},
                                   {"fiscal_year_end_month": True}, {"fiscal_year_end_month": 1, "fiscal_year_named_for": "middle"}])
def test_a_bad_fiscal_calendar_in_the_config_is_refused(entry) -> None:
    with pytest.raises(ValueError, match="fiscal_year"):
        common.companies({"companies": [{"name": "X Corp", "ticker": "X", "cik": "1", **entry}]})


# --- fix 2: a floor is a low-only target ---------------------------------------------------------------


@pytest.mark.parametrize("evidence, value", [
    ("$1.30+(a)", 1.3), ("$1.30+", 1.3), ("$1.30 +", 1.3), ("at least $1.30", 1.3), ("At least 1.30 per share", 1.3),
    ("at least approximately $5.0 billion", 5.0), ("$1.30 or more", 1.3), ("$1.30 or better", 1.3), ("$5.0 billion or more", 5.0),
    ("30%+", 30.0), ("growth of 12 percent or better", 12.0), ("EPS of $1.30+, flat to up slightly on last year", 1.3),
    ("Revenue\n$41.0 billion+", 41.0),
])
def test_a_plus_at_least_or_more_or_better_makes_a_figure_a_floor(evidence, value) -> None:
    assert structure.has_low_marker(value, evidence)


@pytest.mark.parametrize("evidence, value", [
    ("$1.30", 1.3), ("$1.30 to $1.50", 1.3), ("$1.30 +/- 0.05", 1.3), ("$1.30 +2%", 1.3), ("$1.30 or less", 1.3),
    ("at least $2.00, against $1.30 last year", 1.3),  # the floor is the 2.00
    ("$1.30 up 4+ points", 1.3),  # the "+" belongs to the 4
    ("more than $1.30", 1.3), ("about $1.30", 1.3),  # the four phrases asked for, and no others
])
def test_anything_else_is_not_a_floor(evidence, value) -> None:
    assert not structure.has_low_marker(value, evidence)


def item(low=1.3, high=1.3, kind="none", pm=None, metric="EPS non-GAAP"):
    return {"metric": metric, "unit": "USD per share", "target_period": "Q1 FY2026", "value_low": low, "value_high": high,
            "plus_minus": pm, "plus_minus_kind": kind}


def test_a_point_marked_as_a_floor_loses_its_high_and_a_model_that_already_did_so_is_left_alone() -> None:
    original = item()
    assert structure.with_one_sided_low(original, "$1.30+(a)")["value_high"] is None and original["value_high"] == 1.3  # not changed in place
    assert structure.with_one_sided_low(item(high=None), "$1.30+(a)") == item(high=None)
    assert structure.with_one_sided_low(item(), "$1.30") == item()


def test_only_a_bare_point_is_changed() -> None:
    assert structure.with_one_sided_low(item(1.3, 1.5), "$1.30+ to $1.50") == item(1.3, 1.5)  # a range
    assert structure.with_one_sided_low(item(1.3, 1.3, "percent", 2.0), "$1.30+, plus or minus 2%") == item(1.3, 1.3, "percent", 2.0)
    assert structure.with_one_sided_low(item(None, 1.3), "at least $1.30") == item(None, 1.3)  # a ceiling stays a ceiling


def test_targets_q1_fy26_row_was_a_point_and_is_a_floor_now() -> None:
    cid = section_cid("TGT", "2026-03-03")
    w = next(w for w in WRONG if w["fixes"] == [2])
    assert (w["metric"], w["target_period"], w["custom_id"]) == ("EPS non-GAAP", "Q1 FY2026", cid)
    q1 = next(i for i in json.loads(PILOT["raw"][cid])["items"] if i["target_period"] == "Q1 FY2026")
    assert (q1["value_low"], q1["value_high"]) == (1.3, 1.3)  # what the model said, for "$1.30+(a)"
    assert next(d for d in PILOT["drafts"] if d["custom_id"] == cid and d["assumption"]["target_period"] == "Q1 FY2026")["assumption"]["target_high"] == 1.3

    drafts, rejects = replay(cid)
    by_period = {d["assumption"]["target_period"]: d["assumption"] for d in drafts}
    assert (by_period["Q1 FY2026"]["target_low"], by_period["Q1 FY2026"]["target_high"]) == (1.3, None)
    assert by_period["Q1 FY2026"]["evidence"]["excerpt"] == "$1.30+(a)"
    assert (by_period["FY2026"]["target_low"], by_period["FY2026"]["target_high"]) == (7.5, 8.5)  # the range beside it is untouched
    assert rejects == []


def test_the_floor_reaches_the_claim_and_the_invalidation_condition() -> None:
    cid = section_cid("TGT", "2026-03-03")
    drafts, _ = replay(cid)
    q1 = next(d for d in drafts if d["assumption"]["target_period"] == "Q1 FY2026")
    assert review.range_words(q1["assumption"]) == "at least $1.3 per share" and review.invalidation(q1["assumption"]) == "reported value is below 1.3"
    assert review.build_row(q1, None, date(2026, 9, 20))["assumption.target_high"] == ""


def test_a_sentence_that_says_at_least_gives_a_low_only_draft_even_when_the_model_returns_a_point() -> None:
    company = COMPANIES["CRM"]
    cand = {"cik": company.cik, "accession": "0001108524-24-000002", "filing_type": "8-K", "filed_at": "2024-02-28", "char_start": 10, "char_end": 60,
            "sentence": "Revenue is expected to be at least $5.0 billion.", "text_version": 1, "capture_method": "sentence", "heading": None,
            "lead_in": "Our outlook for fiscal 2026 is as follows:", "context_before": [], "context_after": [], "block_lines": None, "block_end": None,
            "table_header": None}
    answer = {"items": [{**item(5.0, 5.0, metric="revenue"), "unit": "USD billions", "target_period": "FY2026"}]}
    text = json.dumps(answer)
    drafts, rejects, _ = structure.derive_drafts({"c-1": (company, cand)}, {"c-1": llm.Result("c-1", "succeeded", text, 1, 1, None, "b")}, METRICS, KINDS)
    (d,) = drafts[company.cik]
    assert (d["assumption"]["target_low"], d["assumption"]["target_high"]) == (5.0, None) and rejects[company.cik] == []


@pytest.mark.parametrize("block", [False, True])
def test_the_prompt_teaches_the_floor_forms(block) -> None:
    text = structure.system_prompt(METRICS, block=block)
    assert '"At least X", "X+", "X or more" and "X or better" give value_low only' in text and '"$1.30+" is value_low 1.30 and no value_high' in text


# --- fix 5: the range is kept over a point that lies inside it -------------------------------------------


def stub(draft_id, method, start, low, high, unit="percent", metric="gross margin GAAP", period="Q3 FY2023", item_index=0):
    return {"draft_id": draft_id, "custom_id": f"cid-{draft_id}", "cik": "0001045810", "capture_method": method, "char_start": start,
            "item_index": item_index, "conflict": False,
            "assumption": {"metric": metric, "target_period": period, "stated_at": "2022-08-24", "target_low": low, "target_high": high, "unit": unit,
                           "text": "t", "evidence": {"accession_number": "0001045810-22-000136"}}}


def ids(rows):
    return sorted(d["draft_id"] for d in rows)


@pytest.mark.parametrize("point_method, range_method", [("section", "section"), ("sentence", "section"), ("section", "sentence"), ("sentence", "sentence")])
def test_the_range_is_kept_over_the_point_however_each_was_captured(point_method, range_method) -> None:
    """NVIDIA: 'GAAP gross margin 62.4%' in the reconciliation table, and '62.4% ... plus or minus 50 basis points' in the outlook."""
    kept, dropped = structure.dedupe_drafts([stub("pt", point_method, 10, 62.4, 62.4), stub("rg", range_method, 90, 61.9, 62.9)])
    assert ids(kept) == ["rg"] and [e["dropped"]["draft_id"] for e in dropped] == ["pt"]
    assert not kept[0]["conflict"]  # a point and the range around it are not a conflict
    assert dropped[0]["reason"].startswith("range kept over point") and dropped[0]["kept"]["draft_id"] == "rg"
    assert "states 61.9 to 62.9" in dropped[0]["reason"] and "(62.4) lies inside it" in dropped[0]["reason"]


def test_two_section_drafts_a_point_and_a_range_around_it_are_not_flagged_as_a_conflict() -> None:
    """Before this rule they were: same key, different numbers, both sections."""
    kept, _ = structure.dedupe_drafts([stub("a", "section", 10, 62.4, 62.4), stub("b", "section", 90, 61.9, 62.9)])
    assert ids(kept) == ["b"] and kept[0]["conflict"] is False


@pytest.mark.parametrize("point", [61.9, 62.9, 62.4])  # the ends count as inside
def test_a_point_on_the_edge_of_the_range_is_inside_it(point) -> None:
    kept, _ = structure.dedupe_drafts([stub("pt", "section", 10, point, point), stub("rg", "section", 90, 61.9, 62.9)])
    assert ids(kept) == ["rg"]


def test_a_point_outside_the_range_is_a_disagreement_and_is_left_to_the_conflict_rule() -> None:
    kept, dropped = structure.dedupe_drafts([stub("pt", "section", 10, 65.0, 65.0), stub("rg", "section", 90, 61.9, 62.9)])
    assert ids(kept) == ["pt", "rg"] and all(d["conflict"] for d in kept) and dropped == []  # nothing chosen, both flagged


def test_a_point_outside_the_range_still_loses_to_a_sentence_as_it_always_did() -> None:
    kept, dropped = structure.dedupe_drafts([stub("pt", "sentence", 10, 65.0, 65.0), stub("rg", "section", 90, 61.9, 62.9)])
    assert ids(kept) == ["pt"] and dropped[0]["reason"].startswith("duplicate of draft pt") and "the numbers differ" in dropped[0]["reason"]


def test_points_and_ranges_in_different_units_are_not_compared() -> None:
    kept, dropped = structure.dedupe_drafts([stub("pt", "section", 10, 1535.0, 1535.0, unit="USD millions", metric="operating expenses GAAP"),
                                             stub("rg", "section", 90, 1.5, 1.6, unit="USD billions", metric="operating expenses GAAP")])
    assert ids(kept) == ["pt", "rg"] and all(d["conflict"] for d in kept)  # the unit differs, so this is left as it was: a flagged conflict


def test_a_floor_holds_a_point_at_or_above_it_and_a_ceiling_one_at_or_below_it() -> None:
    kept, _ = structure.dedupe_drafts([stub("pt", "section", 10, 1.3, 1.3), stub("fl", "section", 90, 1.3, None)])
    assert ids(kept) == ["fl"]  # "$1.30+" and a bare 1.30
    kept, _ = structure.dedupe_drafts([stub("pt", "section", 10, 1.4, 1.4), stub("fl", "section", 90, 1.3, None)])
    assert ids(kept) == ["fl"]
    kept, _ = structure.dedupe_drafts([stub("pt", "section", 10, 1.2, 1.2), stub("fl", "section", 90, 1.3, None)])
    assert ids(kept) == ["fl", "pt"] and all(d["conflict"] for d in kept)  # below the floor: a disagreement
    kept, _ = structure.dedupe_drafts([stub("pt", "section", 10, 6.0, 6.0), stub("ce", "section", 90, None, 6.5)])
    assert ids(kept) == ["ce"]


def test_a_point_is_held_by_the_first_range_that_holds_it_a_sentence_before_a_section() -> None:
    kept, dropped = structure.dedupe_drafts([stub("pt", "section", 5, 5.7, 5.7), stub("sec", "section", 20, 5.5, 6.5), stub("sen", "sentence", 90, 5.0, 6.0)])
    assert ids(kept) == ["sen"]  # the section range then loses to the sentence range, as sections do
    held = next(e for e in dropped if e["dropped"]["draft_id"] == "pt")
    assert held["kept"]["draft_id"] == "sen" and held["reason"].startswith("range kept over point")
    assert next(e for e in dropped if e["dropped"]["draft_id"] == "sec")["reason"].startswith("duplicate of draft sen")


def test_a_range_still_conflicts_with_a_different_range_and_a_dropped_point_does_not_hide_it() -> None:
    kept, dropped = structure.dedupe_drafts([stub("pt", "section", 5, 62.4, 62.4), stub("r1", "section", 20, 61.9, 62.9), stub("r2", "section", 90, 70.0, 71.0)])
    assert ids(kept) == ["r1", "r2"] and all(d["conflict"] for d in kept)
    assert [e["dropped"]["draft_id"] for e in dropped] == ["pt"]


def test_the_rule_only_looks_within_one_key() -> None:
    kept, dropped = structure.dedupe_drafts([stub("pt", "section", 10, 62.4, 62.4, period="Q4 FY2023"), stub("rg", "section", 90, 61.9, 62.9)])
    assert ids(kept) == ["pt", "rg"] and dropped == [] and not any(d["conflict"] for d in kept)


def test_dedupe_still_does_not_change_what_it_is_given_when_it_drops_a_point() -> None:
    pt, rg = stub("pt", "section", 10, 62.4, 62.4), stub("rg", "section", 90, 61.9, 62.9)
    structure.dedupe_drafts([pt, rg])
    assert pt["conflict"] is False and rg["conflict"] is False


def test_the_frozen_pilots_answers_still_dedupe_to_the_same_75_drafts() -> None:
    """No range and point share a key in these drafts, so the new rule changes nothing here. (And the one-sided low changes no count.)"""
    _, _, drafts, rejects, dupes = replay_all()
    assert sum(len(v) for v in drafts.values()) == 75 and sum(len(v) for v in dupes.values()) == 1
    assert not any(e["reason"].startswith("range kept over point") for v in dupes.values() for e in v)


# --- fix 4, second half: outlook blocks that produced no draft ---------------------------------------------


def block(start: int, text: str, heading="Q4 FY26 Guidance", filed="2024-02-01", accession="0000000123-24-000001", header=None):
    return {"cik": "0000000123", "accession": accession, "filing_type": "8-K", "filed_at": filed, "char_start": start, "char_end": start + len(text),
            "sentence": text, "text_version": 1, "capture_method": "section", "heading": heading, "lead_in": None, "context_before": [],
            "context_after": [], "block_lines": len(text.split("\n")), "block_end": "gap", "table_header": header}


def sentence(start: int, text: str):
    return {**block(start, text), "capture_method": "sentence", "block_lines": None, "block_end": None}


EXAMPLE = common.Company("Example Corp", "EXMP", "0000000123")


def rev(lines=(1, 2), low=5.0, high=6.0, metric="revenue", period="Q4 FY2026", unit="USD billions"):
    return {"metric": metric, "unit": unit, "target_period": period, "value_low": low, "value_high": high, "plus_minus": None,
            "plus_minus_kind": "none", "line_first": lines[0], "line_last": lines[1]}


def run(cands: dict[str, dict], answers: dict[str, list | str]):
    index = {cid: (EXAMPLE, c) for cid, c in cands.items()}
    results = {cid: llm.Result(cid, "succeeded", a if isinstance(a, str) else json.dumps({"items": a}), 1, 1, None, "b") for cid, a in answers.items()}
    drafts, rejects, dupes = structure.derive_drafts(index, results, METRICS, KINDS)
    return structure.find_empty_blocks(index, results, drafts, rejects, dupes).get(EXAMPLE.cik, []), drafts, rejects, dupes


def test_a_block_the_model_answered_with_nothing_is_empty() -> None:
    empties, drafts, *_ = run({"b-1": block(100, "Revenue\n$5.0 - $6.0 billion")}, {"b-1": []})
    assert drafts[EXAMPLE.cik] == [] and [e["custom_id"] for e in empties] == ["b-1"]
    assert empties[0]["reason"] == "the model returned no items"


def test_a_block_whose_every_item_was_rejected_is_empty_and_says_why() -> None:
    empties, drafts, rejects, _ = run({"b-1": block(100, "Revenue\n$5.0 - $6.0 billion")}, {"b-1": [rev(low=8.0, high=9.0), rev(unit="percent")]})
    assert drafts[EXAMPLE.cik] == [] and len(rejects[EXAMPLE.cik]) == 2
    (e,) = empties
    assert e["reason"].startswith("2 item(s) returned, all rejected: ") and "is not in the evidence text" in e["reason"] and "is measured in dollars" in e["reason"]


def test_output_that_does_not_parse_is_an_empty_block() -> None:
    empties, *_ = run({"b-1": block(100, "Revenue\n$5.0 - $6.0 billion")}, {"b-1": "not json at all"})
    assert [e["reason"] for e in empties] == ["the model's output did not parse as {items: [...]}"]


def test_a_block_with_a_draft_is_not_empty_even_if_some_of_its_items_were_rejected() -> None:
    empties, drafts, rejects, _ = run({"b-1": block(100, "Revenue\n$5.0 billion to $6.0 billion")}, {"b-1": [rev(), rev(low=8.0, high=9.0, period="Q1 FY2027")]})
    assert len(drafts[EXAMPLE.cik]) == 1 and len(rejects[EXAMPLE.cik]) == 1 and empties == []


def test_a_block_whose_drafts_were_all_dropped_as_duplicates_is_not_empty() -> None:
    """What it said is in the queue under the sentence's draft."""
    cands = {"c-1": sentence(10, "Revenue is expected to be between $5.0 billion and $6.0 billion."), "b-1": block(300, "Revenue\n$5.0 billion to $6.0 billion")}
    empties, drafts, _, dupes = run(cands, {"c-1": [{**rev(), "line_first": 1, "line_last": 1}], "b-1": [rev()]})
    assert [d["custom_id"] for d in drafts[EXAMPLE.cik]] == ["c-1"] and [e["custom_id"] for e in dupes[EXAMPLE.cik]] == ["b-1"] and empties == []


def test_a_sentence_that_produced_nothing_is_never_listed() -> None:
    empties, *_ = run({"c-1": sentence(10, "Revenue was $5.0 billion.")}, {"c-1": []})
    assert empties == []


def test_a_block_that_was_not_answered_is_not_listed() -> None:
    index = {"b-1": (EXAMPLE, block(100, "Revenue\n$5.0 billion to $6.0 billion")), "b-2": (EXAMPLE, block(300, "Gross margin\n70%")), "b-3": (EXAMPLE, block(500, "Tax rate\n17%"))}
    results = {"b-2": llm.Result("b-2", "errored", None, 0, 0, "overloaded", "b"), "b-3": llm.Result("b-3", "truncated", "{", 1, 1, None, "b")}
    assert structure.find_empty_blocks(index, results, {}, {}, {}) == {}


def test_an_empty_block_carries_its_source_and_the_start_of_the_block_verbatim_and_a_stable_id() -> None:
    text = "Revenue\n" + "$5.0 billion to $6.0 billion " * 30  # longer than an excerpt may be
    empties, *_ = run({"b-1": block(100, text, header="Q4 FY26 | Full Year FY26", filed="2024-02-01")}, {"b-1": []})
    (e,) = empties
    assert e["evidence"]["excerpt"] == text[:400] and e["evidence"]["accession_number"] == "0000000123-24-000001"
    assert e["evidence"]["source_url"].startswith("https://www.sec.gov/") and e["evidence"]["content_sha256"] == "a" * 64
    assert (e["heading"], e["table_header"], e["filed_at"], e["ticker"], e["cik"]) == ("Q4 FY26 Guidance", "Q4 FY26 | Full Year FY26", "2024-02-01", "EXMP", EXAMPLE.cik)
    assert e["block_id"] == run({"b-1": block(100, text)}, {"b-1": []})[0][0]["block_id"] and len(e["block_id"]) == 26
    assert e["block_id"] != run({"b-2": block(900, text)}, {"b-2": []})[0][0]["block_id"]


def test_empty_blocks_come_back_in_filing_order() -> None:
    cands = {"b-2": block(50, "x $1 million", filed="2024-05-01", accession="0000000123-24-000009"), "b-1": block(900, "y $2 million"), "b-0": block(10, "z $3 million")}
    empties, *_ = run(cands, {cid: [] for cid in cands})
    assert [e["custom_id"] for e in empties] == ["b-0", "b-1", "b-2"]


def test_on_the_pilot_exactly_one_outlook_block_produced_no_draft() -> None:
    """Target 2021-05-19: a paragraph about a mid-to-high single digit comparable sales and a margin 'well above 7.0 percent'. No figure to guide to."""
    index, results, drafts, rejects, dupes = replay_all()
    found = structure.find_empty_blocks(index, results, drafts, rejects, dupes)
    assert list(found) == [TGT.cik] and [e["custom_id"] for e in found[TGT.cik]] == [section_cid("TGT", "2021-05-19")]
    (e,) = found[TGT.cik]
    assert e["heading"] == "Fiscal 2021 Guidance" and e["reason"].startswith("2 item(s) returned, all rejected: ")
    assert "no number stated" in e["reason"]
    assert e["evidence"]["excerpt"].startswith("For the second quarter of 2021, the Company expects mid-to-high single digit growth in comparable sales.")
