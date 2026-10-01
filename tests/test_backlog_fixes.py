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
    assert default["parens_note"] and kept["parens_note"] is None


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
