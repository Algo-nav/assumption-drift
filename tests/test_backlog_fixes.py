"""Backlog items 1 to 3 (item 4, the acknowledgement filter, is in test_outcomes.py).

The Salesforce rows are the three real "N/A | value" operating margin drafts in the pilot (Q1 FY2022, Q2 FY2022 and
Q1 FY2023, data/review/0001108524.csv), with the table header 02 stored for each. Nothing here calls a model or
touches data/.
"""

from __future__ import annotations

import importlib
import json
from datetime import date, datetime, timezone

import pytest

from pipeline import llm
from research_record.schema import Evidence

structure = importlib.import_module("pipeline.03_structure")
candidates = importlib.import_module("pipeline.02_candidates")
common = importlib.import_module("pipeline.common")

CONFIG = common.load_config()
METRICS, KINDS = CONFIG["metrics"], CONFIG["metric_kinds"]
CRM = common.Company("Salesforce, Inc.", "CRM", "0001108524", 1)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(structure, "evidence_for", lambda company, cand, excerpt: Evidence(
        source_url="https://www.sec.gov/Archives/edgar/data/1/x/x.htm", accession_number="0001108524-21-000007", filing_type="8-K",
        filed_at=date.fromisoformat(cand["filed_at"]), fetched_at=datetime(2026, 9, 20, tzinfo=timezone.utc),
        content_sha256="a" * 64, excerpt=excerpt))


def block(text, header, filed="2021-02-25", heading="Full Year FY22 Guidance"):
    return {"cik": CRM.cik, "accession": "0001108524-21-000007", "filing_type": "8-K", "filed_at": filed, "char_start": 100,
            "char_end": 100 + len(text), "sentence": text, "capture_method": "section", "heading": heading, "lead_in": None,
            "table_header": header, "block_lines": len(text.split("\n"))}


def item(metric, unit, period, low, high, lines=(1, 1)):
    return {"metric": metric, "unit": unit, "target_period": period, "value_low": low, "value_high": high,
            "plus_minus": None, "plus_minus_kind": "none", "line_first": lines[0], "line_last": lines[1]}


def derive(cand, *items, company=CRM):
    cid = "b-test"
    res = {cid: llm.Result(cid, "succeeded", json.dumps({"items": list(items)}), 1, 1, None, "b")}
    drafts, rejects, _ = structure.derive_drafts({cid: (company, cand)}, res, METRICS, KINDS)
    return drafts.get(company.cik, []), rejects.get(company.cik, [])


# --- 1. "N/A | value": the value belongs to the column it sits under ------------------------------------------

@pytest.mark.parametrize("text, header, filed, model_period, fiscal_year", [
    ("Non-GAAP operating margin N/A ~17.7%", "Q1 FY22 | Guidance Full Year FY22", "2021-02-25", "Q1 FY2022", "FY2022"),
    ("Non-GAAP operating margin N/A ~18.0%", "Q2 FY22 | Guidance Full Year FY22", "2021-05-27", "Q2 FY2022", "FY2022"),
    ("Non-GAAP operating margin N/A ~20%", "Q1 FY23 | Guidance Full Year FY23", "2022-03-01", "Q1 FY2023", "FY2023"),
])
def test_a_value_after_an_na_cell_is_the_full_year_column_not_the_quarter(text, header, filed, model_period, fiscal_year) -> None:
    value = float(text.split("~")[1].rstrip("%"))
    drafts, rejects = derive(block(text, header, filed), item("operating margin non-GAAP", "percent", model_period, value, value))
    (draft,) = drafts
    assert rejects == [] and draft["assumption"]["target_period"] == fiscal_year
    assert model_period in draft["parens_note"] and "N/A" in draft["parens_note"]  # shown to the reviewer as the flag note


def test_the_na_column_is_found_when_the_row_is_broken_onto_separate_lines() -> None:
    text = "Non-GAAP operating margin\nN/A\n~17.7%"
    drafts, _ = derive(block(text, "Q1 FY22 | Guidance Full Year FY22"), item("operating margin non-GAAP", "percent", "Q1 FY2022", 17.7, 17.7, (1, 3)))
    assert drafts[0]["assumption"]["target_period"] == "FY2022"


def test_a_period_the_model_already_bound_to_the_value_column_is_left_alone() -> None:
    drafts, _ = derive(block("Non-GAAP operating margin N/A ~17.7%", "Q1 FY22 | Guidance Full Year FY22"),
                       item("operating margin non-GAAP", "percent", "FY2022", 17.7, 17.7))
    assert drafts[0]["assumption"]["target_period"] == "FY2022" and drafts[0]["parens_note"] is None


@pytest.mark.parametrize("text, period", [
    ("Non-GAAP earnings per share $0.54 - $0.55 $2.89 - $2.90 N/A N/A", "Q4 FY2020"),  # the N/A cells are after the figures
    ("Operating margin 18.0% 30.5%", "Q1 FY2022"),                                      # no N/A cell
])
def test_only_an_na_cell_before_the_figure_moves_the_period(text, period) -> None:
    cand = block(text, "Q4 FY20 | Guidance Full Year FY20 | Guidance Q1 FY21 | Guidance Full Year FY21")
    assert structure.fix_na_column_period(period, cand, text) == (period, None)


def test_without_a_table_header_or_with_too_few_columns_nothing_is_guessed() -> None:
    text = "Non-GAAP operating margin N/A ~17.7%"
    assert structure.fix_na_column_period("Q1 FY2022", block(text, None), text) == ("Q1 FY2022", None)
    assert structure.fix_na_column_period("Q1 FY2022", block(text, "Q1 FY22"), text) == ("Q1 FY2022", None)


def test_a_period_the_header_does_not_name_as_an_empty_column_is_not_rewritten() -> None:
    text = "Non-GAAP operating margin N/A ~17.7%"
    assert structure.fix_na_column_period("Q3 FY2022", block(text, "Q1 FY22 | Guidance Full Year FY22"), text) == ("Q3 FY2022", None)


def test_two_na_cells_bind_the_value_to_the_third_column() -> None:
    text = "Non-GAAP operating margin N/A N/A ~17.7%"
    header = "Q1 FY22 | Q2 FY22 | Full Year FY22"
    assert structure.fix_na_column_period("Q1 FY2022", block(text, header), text)[0] == "FY2022"
    assert structure.fix_na_column_period("Q2 FY2022", block(text, header), text)[0] == "FY2022"


@pytest.mark.parametrize("text, count", [
    ("Non-GAAP operating margin N/A ~17.7%", 1), ("Non-GAAP operating margin n/a Approximately 18%", 1),
    ("Revenue N/A — $5.0 billion", 2), ("Revenue $1.47 - $1.49 N/A", 0), ("Revenue $1.47 - $1.49", 0), ("no figures here", 0),
])
def test_leading_empty_cells(text, count) -> None:
    assert structure.leading_empty_cells(text) == count


# --- 2. tax rate parentheses: the filer's own sign rule --------------------------------------------------------

TAX = "(1) The company's GAAP tax provision is expected to be approximately (20%) for the three months ended October 31, 2020."


def test_the_default_for_a_tax_rate_is_the_old_global_behaviour() -> None:
    assert structure.fix_parens_sign("tax rate", -20.0, -20.0, TAX, KINDS)[:2] == (20.0, 20.0)
    assert structure.fix_parens_sign("tax rate", -20.0, -20.0, TAX, KINDS, tax_rate_parens_negative=False)[:2] == (20.0, 20.0)


def test_a_filer_flagged_for_it_keeps_the_sign_on_a_tax_rate() -> None:
    assert structure.fix_parens_sign("tax rate", -20.0, -20.0, TAX, KINDS, tax_rate_parens_negative=True) == (-20.0, -20.0, None)


def test_the_flag_touches_the_tax_rate_and_nothing_else() -> None:
    margin = "Operating margin is expected to be approximately (2%) for the quarter."
    assert structure.fix_parens_sign("operating margin GAAP", -2.0, -2.0, margin, KINDS, tax_rate_parens_negative=True)[:2] == (2.0, 2.0)
    eps = "GAAP earnings (loss) per share ($0.44) - ($0.42)"
    assert structure.fix_parens_sign("EPS GAAP", -0.44, -0.42, eps, KINDS, tax_rate_parens_negative=True) == (-0.44, -0.42, None)


def test_the_company_flag_reaches_the_draft() -> None:
    cand = {**block(TAX, None), "capture_method": "sentence", "sentence": TAX, "heading": None}
    it = {"metric": "tax rate", "unit": "percent", "target_period": "Q3 FY2021", "value_low": -20.0, "value_high": -20.0,
          "plus_minus": None, "plus_minus_kind": "none"}
    (default,), _ = derive(cand, it)
    flagged = common.Company("Salesforce, Inc.", "CRM", "0001108524", 1, tax_rate_parens_negative=True)
    (kept,), _ = derive(cand, it, company=flagged)
    assert (default["assumption"]["target_low"], kept["assumption"]["target_low"]) == (20.0, -20.0)
    assert "positive" in default["parens_note"] and "positive" not in (kept["parens_note"] or "")


def test_config_reads_the_flag_per_company_and_defaults_it_off() -> None:
    base = {"name": "A", "ticker": "a", "cik": "1"}
    assert common._company(base).tax_rate_parens_negative is False
    assert common._company({**base, "tax_rate_parens_negative": True}).tax_rate_parens_negative is True
    with pytest.raises(ValueError, match="tax_rate_parens_negative"):
        common._company({**base, "tax_rate_parens_negative": "yes"})
    assert {c.ticker for c in common.companies(CONFIG) if c.tax_rate_parens_negative} == {"CRM"}  # only Salesforce, set by hand


# --- 3. the tax footnote sentence never produces a third period ------------------------------------------------

FOOTNOTE = "Our effective tax rate for the third quarter of fiscal 2024 and fiscal year 2024 was 21% and 19%, respectively."


def test_a_footnote_with_a_quarter_and_its_year_has_exactly_two_periods() -> None:
    assert candidates.parse_periods(FOOTNOTE) == [(3, 2024), (None, 2024)]
    candidates.check_tax_footnote_periods(FOOTNOTE)  # does not raise


@pytest.mark.parametrize("sentence", [
    FOOTNOTE.replace("was 21%", "was 21% (fiscal 2023: 20%)"),                       # a stray year
    FOOTNOTE.replace("respectively.", "respectively, against Q4 FY23."),              # a label-shaped reference
    FOOTNOTE.replace("fiscal year 2024 was", "fiscal year 2024 and second quarter of fiscal 2024 was"),  # a second quarter
])
def test_a_third_period_in_a_tax_footnote_is_refused_not_kept(sentence) -> None:
    assert len(set(candidates.parse_periods(sentence))) == 3
    with pytest.raises(ValueError, match="exactly two periods"):
        candidates.check_tax_footnote_periods(sentence)


def test_the_check_is_only_for_the_tax_footnote_shape() -> None:
    candidates.check_tax_footnote_periods("Revenue for the third quarter of fiscal 2024, fiscal year 2024 and fiscal 2023 was $5 billion.")  # not tax
    candidates.check_tax_footnote_periods("The tax rate for fiscal 2024, fiscal 2023 and fiscal 2022 was 21%, 20% and 19%.")  # no quarter in it
    candidates.check_tax_footnote_periods("The tax rate for the third quarter of fiscal 2024 and fiscal 2023 was 21% and 19%.")  # not its year
    assert len(candidates.parse_periods("Q3 FY24 and fiscal 2023 and FY2022")) == 3  # parse_periods itself is unchanged


def test_derive_drafts_rejects_a_tax_rate_item_whose_sentence_has_a_third_period() -> None:
    bad = FOOTNOTE.replace("was 21%", "was 21% (fiscal 2023: 20%)") + " We expect a rate near 19%."
    cand = {**block(bad, None), "capture_method": "sentence", "heading": None}
    drafts, rejects = derive(cand, {"metric": "tax rate", "unit": "percent", "target_period": "FY2024", "value_low": 19.0, "value_high": 19.0,
                                    "plus_minus": None, "plus_minus_kind": "none"})
    assert drafts == [] and "exactly two periods" in rejects[0]["reason"]
    ok = FOOTNOTE + " We expect a rate near 19%."
    drafts, rejects = derive({**cand, "sentence": ok}, {"metric": "tax rate", "unit": "percent", "target_period": "FY2024", "value_low": 19.0,
                                                          "value_high": 19.0, "plus_minus": None, "plus_minus_kind": "none"})
    assert len(drafts) == 1 and rejects == []


# --- a plus or minus followed by a currency amount is absolute, in the metric's own unit -------------------------
#
# The excerpts are real Micron rows (data/review/0000723125.csv): the opex and EPS tables where the figure is a
# dollar amount with a dollar spread. The model gets these right on the pilot filings; the guard settles the range
# from the printed text, so a spread read as a percent, or left unscaled, cannot become a wrong range.

MICRON = [  # (record, metric, unit, excerpt, model's centre, expected low, expected high)
    ("01DNNDW300RD9SM5TFC1M35RS1", "EPS non-GAAP", "USD per share", "Diluted earnings per share\n$0.42 ± $0.07", 0.42, 0.35, 0.49),
    ("01HSCJGP00HP5SDBZPT8467V08", "EPS non-GAAP", "USD per share", "Diluted earnings per share\n$0.17 ± $0.07 $0.45 ± $0.07", 0.45, 0.38, 0.52),
    ("01FZ9FHR00FPS3HE0E46J0C6PN", "operating expenses non-GAAP", "USD billions", "Operating expenses\n$1.13 billion ± $25 million $1.05 billion ± $25 million", 1.05, 1.025, 1.075),
    ("01J8K7MH006E270D63BDN0PWSD", "operating expenses non-GAAP", "USD billions", "Operating expenses\n$1.211 billion ± $15 million $1.085 billion ± $15 million", 1.085, 1.07, 1.1),
    ("01DNNDW3001EK5QK020CFRSRA0", "operating expenses non-GAAP", "USD millions", "Operating expenses\n$818 million ± $25 million\n$780 million ± $25 million", 780.0, 755.0, 805.0),
    ("01DNNDW300GD47X18Q861TKK4N", "revenue", "USD billions", "Revenue\n$5.0 billion ± $200 million", 5.0, 4.8, 5.2),
]


def pm_item(metric, unit, centre, spread, kind):
    return {"metric": metric, "unit": unit, "target_period": "Q1 FY2020", "value_low": centre, "value_high": centre,
            "plus_minus": spread, "plus_minus_kind": kind}


@pytest.mark.parametrize("record, metric, unit, excerpt, centre, low, high", MICRON, ids=[m[0][-6:] for m in MICRON])
@pytest.mark.parametrize("model_says", [("percent", "spread"), ("absolute", "unscaled"), ("absolute", "right")])
def test_a_currency_spread_becomes_the_right_absolute_range_whatever_the_model_said(record, metric, unit, excerpt, centre, low, high, model_says) -> None:
    kind, how = model_says
    right = (high - low) / 2
    spread = {"spread": right * 1000 if "million" in excerpt and unit == "USD billions" else right, "unscaled": right * 1000 if unit == "USD billions" else right, "right": right}[how]
    item = pm_item(metric, unit, centre, spread, kind)
    fixed, note = structure.with_currency_spread_absolute(metric, item, excerpt, KINDS)
    assert fixed["plus_minus_kind"] == "absolute" and fixed["plus_minus"] == pytest.approx(right)
    assert structure.resolve_range(fixed) == pytest.approx((low, high))
    assert (note is None) == (kind == "absolute" and fixed["plus_minus"] == pytest.approx(spread))  # a note only when something changed


def test_a_spread_the_model_already_has_right_is_left_alone_with_no_note() -> None:
    item = pm_item("EPS non-GAAP", "USD per share", 0.42, 0.07, "absolute")
    assert structure.with_currency_spread_absolute("EPS non-GAAP", item, "$0.42 ± $0.07", KINDS) == (item, None)


def test_a_percent_spread_is_not_a_currency_spread() -> None:
    gm = "Gross margin\n30% ± 1.5% 31% ± 1.5%"
    item = pm_item("gross margin non-GAAP", "percent", 31.0, 1.5, "absolute")
    assert structure.with_currency_spread_absolute("gross margin non-GAAP", item, gm, KINDS) == (item, None)
    rev = pm_item("revenue", "USD billions", 108.0, 2.0, "percent")  # "$108.0 billion, plus or minus 2%": relative, no currency spread
    assert structure.with_currency_spread_absolute("revenue", rev, "Revenue is expected to be $108.0 billion, plus or minus 2%.", KINDS) == (rev, None)


def test_the_centre_has_to_be_the_items_own_value() -> None:
    item = pm_item("EPS non-GAAP", "USD per share", 0.99, 0.5, "percent")
    assert structure.with_currency_spread_absolute("EPS non-GAAP", item, "$0.42 ± $0.07", KINDS) == (item, None)


def test_a_micron_eps_row_through_derive_drafts_gets_the_absolute_range_and_a_note() -> None:
    text = "Diluted earnings per share\n$0.42 ± $0.07"
    cand = {**block(text, None), "heading": "Guidance"}
    it = {**pm_item("EPS non-GAAP", "USD per share", 0.42, 0.07, "percent"), "line_first": 1, "line_last": 2}
    (draft,), rejects = derive(cand, it)
    a = draft["assumption"]
    assert rejects == [] and (a["target_low"], a["target_high"]) == (pytest.approx(0.35), pytest.approx(0.49))
    assert "absolute" in draft["parens_note"]


# --- total expenses and capital expenditures are metrics in their own right --------------------------------------
#
# Meta guides both in the sentence shape below (data/review/0001326801.csv). Before they were on the list the model
# put total expenses under "operating expenses GAAP" and capital expenditures under "other income and expense", and
# the second opinion called every one of those rows wrong_metric.

META_TOTAL = "We expect full year 2026 total expenses to be in the range of $162-169 billion."
META_CAPEX = "We expect capital expenditures to be in the range of $30-33 billion, lowered from our prior estimate of $34-37 billion."
META_CRM = common.Company("Meta Platforms, Inc.", "META", "0001326801", 12)


def sentence_candidate(text):
    return {**block(text, None), "capture_method": "sentence", "heading": None, "cik": META_CRM.cik, "filed_at": "2026-01-28"}


@pytest.mark.parametrize("metric", ["total expenses", "capital expenditures"])
def test_the_two_metrics_are_configured_as_dollar_amounts_where_lower_is_better(metric) -> None:
    from research_record import polarity
    assert metric in METRICS and KINDS[metric] == "dollars" and polarity.load()[metric] is False
    for block_mode in (False, True):
        enum = structure.item_schema(METRICS, block=block_mode)["properties"]["items"]["items"]["properties"]["metric"]["enum"]
        assert metric in enum and f'"{metric}"' in structure.system_prompt(METRICS, block=block_mode)


@pytest.mark.parametrize("text, metric, period, low, high", [
    (META_TOTAL, "total expenses", "FY2026", 162.0, 169.0),
    (META_CAPEX, "capital expenditures", "FY2026", 30.0, 33.0),
])
def test_a_meta_sentence_now_makes_a_draft_under_its_own_metric(text, metric, period, low, high) -> None:
    it = {"metric": metric, "unit": "USD billions", "target_period": period, "value_low": low, "value_high": high,
          "plus_minus": None, "plus_minus_kind": "none"}
    (draft,), rejects = derive(sentence_candidate(text), it, company=META_CRM)
    a = draft["assumption"]
    assert rejects == [] and (a["metric"], a["target_low"], a["target_high"], a["unit"]) == (metric, low, high, "USD billions")


@pytest.mark.parametrize("metric", ["total expenses", "capital expenditures"])
def test_the_unit_has_to_be_dollars_for_either_metric(metric) -> None:
    structure.check_unit_kind(metric, "USD billions", KINDS)
    with pytest.raises(ValueError):
        structure.check_unit_kind(metric, "percent", KINDS)


def test_over_the_range_is_worse_for_both_and_under_it_is_better() -> None:
    from research_record import polarity, rubric
    for metric in ("total expenses", "capital expenditures"):
        up = polarity.higher_is_better(metric)
        assert rubric.direction(162.0, 169.0, 175.0, up) == "worse" and rubric.direction(162.0, 169.0, 150.0, up) == "better"


def test_the_outcome_search_reads_total_expenses_lines_and_not_operating_expenses_ones() -> None:
    outcomes = importlib.import_module("pipeline.04_outcomes")
    terms = CONFIG["outcomes"]["metric_terms"]
    total = outcomes.metric_pattern("total expenses", terms)
    assert total.search("Total expenses were $97.7 billion, up 17% year-over-year.") and not total.search("Operating expenses were $12.0 billion.")
    assert outcomes.metric_pattern("operating expenses GAAP", terms).search("Operating expenses were $12.0 billion.")
    capex = outcomes.metric_pattern("capital expenditures", terms)
    assert capex.search("Capital expenditures, including principal payments on finance leases, were $18.8 billion.")


# --- the table's own GAAP | non-GAAP columns set each figure's basis -----------------------------------------------
#
# Micron's guidance tables (data/candidates/0000723125.jsonl, 2020-03-25 and 2022-06-30 releases) print GAAP and
# non-GAAP side by side. The section heading names only the second column ("Non-GAAP (2) Outlook", with "GAAP (1) Outlook"
# in the line above), so the heading rule gave every figure in the table the non-GAAP basis: both columns became
# "operating expenses non-GAAP", disagreed with each other, and were marked as conflicts.

MU = common.Company("Micron Technology, Inc.", "MU", "0000723125", 8)
MU_TABLE = ("Revenue\n$4.6 billion - $5.2 billion $4.6 billion - $5.2 billion\nGross margin\n30% ± 1.5% 31% ±1.5%\n"
            "Operating expenses\n$891 million ± $25 million $825 million ± $25 million\n"
            "Interest (income) expense, net\n$38 million $35 million\nDiluted earnings per share\n$0.41 ± $0.15 $0.55 ± $0.15")
MU_RECON = ("Revenue\n$4.6 billion - $5.2 billion — $4.6 billion - $5.2 billion\nGross margin\n30% ± 1.5% 1% A 31% ±1.5%\n"
            "Operating expenses\n$891 million ± $25 million $66 million B $825 million ± $25 million\n"
            "Diluted earnings per share (1)\n$0.41 ± $0.15 $0.14 A, B, C, D $0.55 ± $0.15")


def mu_block(text, heading, before):
    return {**block(text, None), "cik": MU.cik, "heading": heading, "context_before": before, "lead_in": None}


def mu_item(metric, unit, centre, line, spread=None, kind="none"):
    return {"metric": metric, "unit": unit, "target_period": "Q3 FY2020", "value_low": centre, "value_high": centre,
            "plus_minus": spread, "plus_minus_kind": kind, "line_first": line, "line_last": line + 1}


SPLIT = mu_block(MU_TABLE, "Non-GAAP (2) Outlook", ["FQ3-20", "GAAP (1) Outlook"])  # the header cells on separate lines
RECON = mu_block(MU_RECON, "GAAP Outlook Adjustments Non-GAAP Outlook", ["RECONCILIATION OF GAAP TO NON-GAAP OUTLOOK"])  # all in the heading


def test_the_header_columns_are_read_from_the_heading_alone_or_from_the_lines_above_it() -> None:
    assert structure.basis_columns(SPLIT) == ["GAAP", "non-GAAP"]
    assert structure.basis_columns(RECON) == ["GAAP", "Adjustments", "non-GAAP"]  # its own heading wins over the title above it
    assert structure.basis_columns(mu_block("x", "Adjusted diluted earnings per share guidance", [])) is None  # one basis only: not a column header
    assert structure.basis_columns({**SPLIT, "capture_method": "sentence"}) is None


@pytest.mark.parametrize("row, expected", [
    ("$891 million ± $25 million $825 million ± $25 million", 2),
    ("$891 million ± $25 million $66 million B $825 million ± $25 million", 3),
    ("$4.6 billion - $5.2 billion — $4.6 billion - $5.2 billion", 3),
    ("$0.41 ± $0.15 $0.14 A, B, C, D $0.55 ± $0.15", 3),
    ("30% ± 1.5% 31% ±1.5%", 2), ("Interest (income) expense, net $38 million $3 million C $35 million", 3),
])
def test_value_cells_counts_the_columns_of_a_row(row, expected) -> None:
    assert len(structure.value_cells(row)) == expected


@pytest.mark.parametrize("metric, unit, centre, line, said, becomes", [
    ("operating expenses non-GAAP", "USD millions", 891.0, 5, "operating expenses non-GAAP", "operating expenses GAAP"),   # the first column
    ("operating expenses non-GAAP", "USD millions", 825.0, 5, "operating expenses GAAP", "operating expenses non-GAAP"),  # the second
    ("EPS non-GAAP", "USD per share", 0.41, 9, "EPS non-GAAP", "EPS GAAP"),
    ("EPS non-GAAP", "USD per share", 0.55, 9, "EPS GAAP", "EPS non-GAAP"),
    ("gross margin non-GAAP", "percent", 30.0, 3, "gross margin non-GAAP", "gross margin GAAP"),
    ("gross margin non-GAAP", "percent", 31.0, 3, "gross margin GAAP", "gross margin non-GAAP"),
])
def test_a_figure_takes_the_basis_of_its_column_whatever_the_heading_and_the_model_said(metric, unit, centre, line, said, becomes) -> None:
    it = mu_item(said, unit, centre, line, spread=25.0 if "expenses" in metric else None, kind="absolute" if "expenses" in metric else "none")
    drafts, rejects = derive(SPLIT, it, company=MU)
    assert rejects == [] and [d["assumption"]["metric"] for d in drafts] == [becomes]


def test_both_columns_of_a_row_are_kept_as_two_separate_rows_not_a_conflict() -> None:
    items = [mu_item("operating expenses non-GAAP", "USD millions", 891.0, 5, 25.0, "absolute"), mu_item("operating expenses non-GAAP", "USD millions", 825.0, 5, 25.0, "absolute")]
    drafts, _ = derive(SPLIT, *items, company=MU)
    by = {d["assumption"]["metric"]: (d["assumption"]["target_low"], d["assumption"]["target_high"]) for d in drafts}
    assert by == {"operating expenses GAAP": (866.0, 916.0), "operating expenses non-GAAP": (800.0, 850.0)}
    assert not any(d["conflict"] for d in drafts)


def test_a_three_column_reconciliation_row_assigns_gaap_and_non_gaap_and_refuses_the_adjustment() -> None:
    gaap, adj, non = (mu_item("operating expenses non-GAAP", "USD millions", v, 5) for v in (891.0, 66.0, 825.0))
    gaap = {**gaap, "plus_minus": 25.0, "plus_minus_kind": "absolute"}
    non = {**non, "plus_minus": 25.0, "plus_minus_kind": "absolute"}
    drafts, rejects = derive(RECON, gaap, adj, non, company=MU)
    assert sorted(d["assumption"]["metric"] for d in drafts) == ["operating expenses GAAP", "operating expenses non-GAAP"]
    assert len(rejects) == 1 and "Adjustments column" in rejects[0]["reason"]


def test_a_metric_without_a_gaap_pair_and_a_figure_in_two_columns_are_left_as_they_were() -> None:
    rev = mu_item("revenue", "USD billions", 4.6, 1)
    assert structure.with_column_basis("revenue", SPLIT, rev, "Revenue\n$4.6 billion - $5.2 billion $4.6 billion - $5.2 billion", METRICS) == ("revenue", None, False)
    same = mu_item("operating expenses GAAP", "USD millions", 825.0, 5)
    row = "Operating expenses\n$825 million ± $25 million $825 million ± $25 million"
    assert structure.with_column_basis("operating expenses GAAP", SPLIT, same, row, METRICS) == ("operating expenses GAAP", None, False)


def test_a_row_whose_cells_do_not_match_the_header_is_not_overridden() -> None:
    it = mu_item("operating expenses non-GAAP", "USD millions", 891.0, 5)
    assert structure.with_column_basis("operating expenses non-GAAP", SPLIT, it, "Operating expenses\n$891 million", METRICS)[2] is False


def test_without_a_column_header_the_heading_rule_still_decides() -> None:
    cand = mu_block("GAAP\nNon-GAAP\nDiluted net income per share\n$1.10 - $1.20", "Adjusted diluted earnings per share guidance", [])
    it = {"metric": "EPS GAAP", "unit": "USD per share", "target_period": "Q3 FY2020", "value_low": 1.1, "value_high": 1.2,
          "plus_minus": None, "plus_minus_kind": "none", "line_first": 3, "line_last": 4}
    drafts, _ = derive(cand, it, company=MU)
    assert drafts[0]["assumption"]["metric"] == "EPS non-GAAP"  # the heading says Adjusted, as before


# --- the headline: wrapped over lines, and a fiscal year derived from the calendar --------------------------------
#
# Micron wraps its headline ("MICRON TECHNOLOGY, INC. REPORTS RESULTS FOR THE" / "FIRST QUARTER OF FISCAL 2025") and the
# matcher saw only the first line; Lowe's headline names the quarter and no year ("LOWE'S REPORTS FOURTH QUARTER SALES AND
# EARNINGS RESULTS", filed 2020-02-26) and the year was looked for in the first 2,500 characters, where it is not.

from types import SimpleNamespace

outcomes_step = importlib.import_module("pipeline.04_outcomes")
MICRON_CO = common.Company("Micron Technology, Inc.", "MU", "0000723125", 8)
LOWES = common.Company("Lowe's Companies, Inc.", "LOW", "0000060667", 1, "start")
TARGET = common.Company("Target Corporation", "TGT", "0000027419", 1, "start")

MU_RELEASE = "EX-99.1\nFOR IMMEDIATE RELEASE\nContacts:\n(408) 203-2910\nMICRON TECHNOLOGY, INC. REPORTS RESULTS FOR THE\nFIRST QUARTER OF FISCAL 2025\nMicron delivers record fiscal Q1 revenue, driven by strong AI demand\n"
LOWES_RELEASE = "EX-99.1\nPRESS RELEASE\nFebruary 26, 2020\nLOWE’S REPORTS FOURTH QUARTER SALES AND EARNINGS RESULTS\n-- Diluted Earnings Per Share of $0.66 --\n" + "x " * 2000


def release(text, filed):
    return SimpleNamespace(text=text, meta={"filed_at": filed})


def test_a_wrapped_headline_is_read_over_up_to_three_lines_and_stops_once_it_names_a_quarter() -> None:
    assert structure.document_title(MU_RELEASE, wrap=True) == "MICRON TECHNOLOGY, INC. REPORTS RESULTS FOR THE FIRST QUARTER OF FISCAL 2025"
    assert structure.document_title(MU_RELEASE) == "MICRON TECHNOLOGY, INC. REPORTS RESULTS FOR THE"  # the prompt still shows the first line alone
    sub = "Acme Reports Third Quarter Results\nRaises fourth quarter outlook\n"
    assert structure.document_title(sub, wrap=True) == "Acme Reports Third Quarter Results"  # a subtitle is not part of it
    assert len(structure.document_title("ACME REPORTS RESULTS FOR THE\n" + "word " * 19 + "\n" + "word " * 19, wrap=True)) <= 300


def test_micron_wrapped_headline_now_names_its_period() -> None:
    doc = release(MU_RELEASE, "2024-12-18")
    assert outcomes_step.release_names_period(doc, (1, 2025), 2500, MICRON_CO)
    assert not outcomes_step.release_names_period(doc, (1, 2024), 2500, MICRON_CO) and not outcomes_step.release_names_period(doc, (2, 2025), 2500, MICRON_CO)


@pytest.mark.parametrize("period, expected", [((None, 2019), True), ((4, 2019), True), ((None, 2018), False), ((None, 2020), False), ((3, 2019), False)])
def test_a_quarter_headline_with_no_year_takes_its_fiscal_year_from_the_filing_date_and_the_calendar(period, expected) -> None:
    doc = release(LOWES_RELEASE, "2020-02-26")  # fiscal 2019 ended January 31, 2020
    assert outcomes_step.release_names_period(doc, period, 2500, LOWES) is expected


def test_without_a_calendar_the_year_is_still_looked_for_in_the_head() -> None:
    doc = release("LOWE’S REPORTS FOURTH QUARTER SALES AND EARNINGS RESULTS\nfiscal 2019 sales", "2020-02-26")
    assert outcomes_step.release_names_period(doc, (None, 2019), 2500, None) and not outcomes_step.release_names_period(doc, (None, 2020), 2500, None)


@pytest.mark.parametrize("company, quarter, filed, expected", [
    (LOWES, 4, "2020-02-26", 2019), (LOWES, None, "2020-02-26", 2019), (LOWES, 1, "2020-05-20", 2020),
    (TARGET, 2, "2023-08-16", 2023), (MICRON_CO, 1, "2024-12-18", 2025), (MICRON_CO, 4, "2023-09-27", 2023),
    (LOWES, 1, "2020-02-26", None),                                  # the first quarter ended most of a year before: not the one reported
    (common.Company("x", "x", "1"), 4, "2020-02-26", None),          # no fiscal calendar
])
def test_fiscal_year_reported(company, quarter, filed, expected) -> None:
    from datetime import date
    assert company.fiscal_year_reported(quarter, date.fromisoformat(filed)) == expected


# --- a period that closed before the guidance was stated -----------------------------------------------------------
#
# The 13 Micron drafts: six labelled Q2 FY2020 stated 2020-03-25, six labelled Q3 FY2020 stated 2020-06-29, and a
# non-GAAP gross margin labelled Q4 FY2023 stated 2023-11-28. Each is the quarter before the one the release guides.

MICRON_13 = ([("Q2 FY2020", "2020-03-25", "Q3 FY2020")] * 6 + [("Q3 FY2020", "2020-06-29", "Q4 FY2020")] * 6
             + [("Q4 FY2023", "2023-11-28", "Q1 FY2024")])


@pytest.mark.parametrize("period, stated, likely", MICRON_13)
def test_the_thirteen_micron_drafts_are_flagged_and_the_next_quarter_is_the_suggestion(period, stated, likely) -> None:
    from datetime import date
    assert structure.period_closes_before_stated(MICRON_CO, period, date.fromisoformat(stated)) == structure.PERIOD_BEFORE_STATED
    assert structure.next_fiscal_period(period) == likely
    assert structure.PERIOD_BEFORE_STATED == "period closes before stated; likely next quarter"


@pytest.mark.parametrize("period, stated", [("Q3 FY2020", "2020-03-25"), ("Q4 FY2020", "2020-06-29"), ("FY2020", "2020-03-25"), ("Q2 FY2020", "2020-02-29")])
def test_a_period_still_open_at_the_statement_is_not_flagged(period, stated) -> None:
    from datetime import date
    assert structure.period_closes_before_stated(MICRON_CO, period, date.fromisoformat(stated)) is None  # closing on the day itself is not before it


def test_nothing_is_flagged_without_a_calendar_or_with_a_period_it_cannot_read() -> None:
    from datetime import date
    assert structure.period_closes_before_stated(common.Company("x", "x", "1"), "Q2 FY2020", date(2020, 3, 25)) is None
    assert structure.period_closes_before_stated(MICRON_CO, "second half", date(2020, 3, 25)) is None
    assert [structure.next_fiscal_period(p) for p in ("FY2020", "Q4 FY2020", "Q1 FY2021", "later")] == ["FY2021", "Q1 FY2021", "Q2 FY2021", None]


def test_a_flagged_draft_is_kept_with_the_flag_as_its_note_and_never_corrected() -> None:
    cand = mu_block("Operating expenses\n$891 million ± $25 million", "Outlook", [])
    cand["filed_at"] = "2020-03-25"
    it = {**mu_item("operating expenses GAAP", "USD millions", 891.0, 1, 25.0, "absolute"), "target_period": "Q2 FY2020", "line_last": 2}
    (draft,), rejects = derive(cand, it, company=MICRON_CO)
    assert rejects == [] and draft["assumption"]["target_period"] == "Q2 FY2020"  # the period is not changed
    assert draft["parens_note"].startswith(structure.PERIOD_BEFORE_STATED)


def test_03c_suggests_the_next_quarter_for_a_flagged_row_and_only_while_nobody_has_reviewed_it() -> None:
    suggest = importlib.import_module("pipeline.03c_suggest")
    forward, number = importlib.import_module("pipeline.02_candidates").compile_matchers(CONFIG["candidates"])
    row = {"aid_flag_note": structure.PERIOD_BEFORE_STATED, "assumption.target_period": "Q2 FY2020", "aid_verify": "yes", "reviewer_note": "", "approved": "false"}
    assert suggest.qualifies(row)  # even though the verifier said yes
    note = suggest.suggest_note(row, forward, number)
    assert note.startswith("CHECK:") and "likely Q3 FY2020" in note and "false alarm" not in note
    assert not suggest.qualifies({**row, "reviewer_note": "checked"})
    updated, false_alarms, checks = suggest.suggest_company([row, {**row, "reviewer_note": "x"}], forward, number)
    assert updated[0]["aid_suggested_note"] == note and updated[1]["aid_suggested_note"] == "" and (false_alarms, checks) == (0, 1)
    assert all(r["approved"] == "false" for r in updated)  # never approves


# --- the column a figure came from, for the verifier -----------------------------------------------------------------


def test_the_column_a_figure_came_from_is_recorded_for_the_reviewer_and_the_verifier() -> None:
    assert structure.value_column_text(SPLIT, mu_item("operating expenses GAAP", "USD millions", 891.0, 5), MU_TABLE) == "GAAP | non-GAAP: the figure is in column 1, GAAP"
    assert structure.value_column_text(SPLIT, mu_item("operating expenses GAAP", "USD millions", 825.0, 5), MU_TABLE).endswith("column 2, non-GAAP")
    assert structure.value_column_text(RECON, mu_item("operating expenses GAAP", "USD millions", 825.0, 5), MU_RECON) == "GAAP | Adjustments | non-GAAP: the figure is in column 3, non-GAAP"
    assert structure.value_column_text(SPLIT, mu_item("revenue", "USD billions", 4.6, 1), MU_TABLE) is None  # printed in both columns: not told
    assert structure.value_column_text(block("x", None), mu_item("revenue", "USD billions", 4.6, 1), "x") is None  # no header


def test_the_draft_carries_it_and_the_review_row_shows_it() -> None:
    review = importlib.import_module("pipeline.05_review")
    it = {**mu_item("operating expenses non-GAAP", "USD millions", 891.0, 5, 25.0, "absolute")}
    (draft,), _ = derive({**SPLIT, "filed_at": "2020-03-25"}, it, company=MU)
    assert draft["value_column"] == "GAAP | non-GAAP: the figure is in column 1, GAAP"
    assert "aid_value_column" in review.COLUMNS and review.is_pipeline_owned("aid_value_column")
