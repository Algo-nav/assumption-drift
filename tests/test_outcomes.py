"""04_outcomes.py against synthetic filings and a fake client."""

from __future__ import annotations

import hashlib
import importlib
import json
import re
from datetime import date

import pytest
import yaml

import fakes
from pipeline import common, llm
from research_record import rubric

outcomes = importlib.import_module("pipeline.04_outcomes")
cand = importlib.import_module("pipeline.02_candidates")

COMPANY = common.Company("Example Corp", "EXMP", "0000000123")
CONFIG = common.load_config()
OCFG = CONFIG["outcomes"]
NUMBER = cand.Settings.from_config(CONFIG["candidates"]).number
TODAY = date(2026, 9, 20)

DOCS = [
    # accession suffix, filed, headline, lines
    ("0010", "2024-02-01", "Example Corp Announces Financial Results for Third Quarter Fiscal 2024",
     ["Outlook", "Revenue is expected to be $5.0 billion to $6.0 billion for the fourth quarter of fiscal 2024."]),
    ("0011", "2024-04-15", "Example Corp Third Quarter Fiscal 2024 Results Conference Call",
     ["Fourth quarter revenue of $9.9 billion is what we hope to discuss."]),
    ("0012", "2024-05-01", "Example Corp Announces Financial Results for Fourth Quarter and Fiscal 2024",
     ["Fourth quarter revenue of $4.8 billion, up 10% from a year ago.",
      "Fiscal 2024 revenue was $19.5 billion.",
      "Cost of revenues $ 300 $ 250",
      "Initiates first quarter fiscal 2025 revenue guidance of $5.2 billion to $5.4 billion."]),
    ("0013", "2024-08-01", "Example Corp Announces Financial Results for First Quarter Fiscal 2025",
     ["First quarter revenue of $5.3 billion.", "Last quarter revenue of $4.8 billion was below our guidance."]),
]


def draft(did="D1", metric="revenue", unit="USD billions", period="Q4 FY2024", stated="2024-02-01", low=5.0, high=6.0):
    return {"draft_id": did, "cik": COMPANY.cik, "ticker": "EXMP", "company": COMPANY.name,
            "assumption": {"metric": metric, "unit": unit, "target_period": period, "stated_at": stated,
                           "target_low": low, "target_high": high, "text": "Revenue is expected to be $5.0 billion to $6.0 billion."}}


@pytest.fixture
def world(tmp_path, monkeypatch):
    raw = tmp_path / "raw" / COMPANY.cik
    raw.mkdir(parents=True)
    for suffix, filed, title, lines in DOCS:
        acc = f"0000000123-24-00{suffix}"
        html = ("<html><body>" + f"<p>{title}</p>" + "".join(f"<p>{line}</p>" for line in lines) + "</body></html>").encode()
        (raw / f"{acc}.html").write_bytes(html)
        (raw / f"{acc}.meta.json").write_text(json.dumps({
            "cik": COMPANY.cik, "accession": acc, "filing_type": "8-K", "filed_at": filed, "http_status": 200,
            "final_url": f"https://www.sec.gov/Archives/edgar/data/123/{acc.replace('-', '')}/release.htm",
            "fetched_at": "2026-09-20T12:00:00+00:00", "content_sha256": hashlib.sha256(html).hexdigest()}))
    monkeypatch.setattr(common, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(outcomes, "DRAFTS_DIR", tmp_path / "drafts")
    monkeypatch.setattr(outcomes, "OUTCOMES_DIR", tmp_path / "outcomes")
    monkeypatch.setattr(llm, "BATCH_DIR", tmp_path / "batches")
    return tmp_path


def store():
    return outcomes.DocStore(COMPANY)


def test_only_8k_exhibits_are_ever_searched(world) -> None:
    raw = world / "raw" / COMPANY.cik
    meta = json.loads((raw / "0000000123-24-000012.meta.json").read_text())
    for form in ("10-K", "10-Q"):
        acc = f"0000000123-24-9{form[-1] == 'K' and '1' or '2'}0000"
        (raw / f"{acc}.html").write_bytes(b"<html><body><p>Fourth quarter revenue of $4.8 billion.</p></body></html>")
        (raw / f"{acc}.meta.json").write_text(json.dumps({**meta, "accession": acc, "filing_type": form}))
    assert {m["filing_type"] for m in store().metas} == {"8-K"}


def requests_for(*drafts):
    return outcomes.build_outcome_requests({COMPANY.cik: list(drafts)}, {COMPANY.cik: store()}, OCFG, NUMBER, today=TODAY)


# --- periods, headlines, metrics, units ------------------------------------


@pytest.mark.parametrize("text, expected", [("FY2027", (None, 2027)), ("Q3 FY2027", (3, 2027)), ("third quarter", None), ("FY27", None)])
def test_parse_period(text, expected) -> None:
    assert outcomes.parse_period(text) == expected


@pytest.mark.parametrize(
    "title, head, quarter, year, expected",
    [
        ("NVIDIA Announces Financial Results for Fourth Quarter and Fiscal 2026", "", 4, 2026, True),
        ("NVIDIA Announces Financial Results for Fourth Quarter and Fiscal 2026", "", None, 2026, True),  # fiscal-year results ride with Q4
        ("NVIDIA Announces Financial Results for Fourth Quarter and Fiscal 2026", "", 4, 2025, False),  # a headline that names a year must name this one
        ("Salesforce Reports Record First Quarter Fiscal 2026 Results", "", 2, 2026, False),  # wrong quarter
        ("Salesforce Delivers Record Fourth Quarter FY26 Results", "", 4, 2026, True),  # FY26 form
        ("Target Corporation Reports Second Quarter Earnings", "second quarter 2026 results", 2, 2026, True),  # no year in the headline: use the head
        ("Target Corporation Reports Second Quarter Earnings", "second quarter 2026 results", 2, 2025, False),
        ("Financial Implications and Third Quarter Fiscal 2026 Results Conference Call", "", 3, 2026, False),  # a preview notice
    ],
)
def test_a_headline_decides_which_period_a_release_reports(title, head, quarter, year, expected) -> None:
    assert outcomes.title_names_period(title, head, quarter, year) is expected


def test_guidance_bullets_at_the_top_of_a_release_do_not_make_it_a_release_for_that_period() -> None:
    # Salesforce opens with "Initiates fourth quarter FY26 revenue guidance..." in a Q3 release.
    assert not outcomes.title_names_period("Salesforce Delivers Record Third Quarter Fiscal 2026 Results", "fourth quarter FY26 guidance", 4, 2026)


@pytest.mark.parametrize(
    "metric, line, expected",
    [
        ("revenue", "Total net sales were $5 billion", True),
        ("non-gaap gross margin", "Gross margin 75.2 %", True),
        ("gaap eps", "Diluted earnings per share $2.30", True),
        ("operating cash flow growth", "Cash flow from operations rose", True),
        ("revenue", "Gross margin 75.2 %", False),
        ("headcount growth", "Headcount rose to 5,000", True),  # no group: falls back to its content words
        ("gross margin non-GAAP", "Gross margin 75.2 %", True),  # the canonical, mixed-case names
        ("gross margin GAAP", "GAAP gross margin was 75.0%", True),
        ("EPS GAAP", "Diluted earnings per share $2.30", True),
        ("EPS non-GAAP", "Non-GAAP EPS was $1.87", True),
        ("operating expenses GAAP", "GAAP operating expenses $4,250", True),
        ("operating income", "Income from operations was $5 billion", True),
        ("operating income", "Gross margin 75.2 %", False),
        ("tax rate", "The effective tax rate was 17%", True),
        ("other income and expense", "Other income, net $500", True),
        ("comparable sales", "Comparable sales rose 2.1 percent", True),
        ("free cash flow", "Free cash flow was $5 billion", True),
    ],
)
def test_metric_pattern_says_which_lines_are_about_the_metric(metric, line, expected) -> None:
    pattern = outcomes.metric_pattern(metric, OCFG["metric_terms"])
    assert bool(pattern.search(line)) is expected


def test_a_metric_with_nothing_to_match_on_is_not_recognised() -> None:
    assert outcomes.metric_pattern("gaap non", OCFG["metric_terms"]) is None


@pytest.mark.parametrize(
    "value, a, b, expected",
    [(5600, "USD millions", "USD billions", 5.6), (5.6, "USD billions", "USD millions", 5600), (75, "percent", "percent", 75),
     (50, "basis points", "percent", 0.5), (2.3, "USD per share", "USD per share", 2.3)],
)
def test_units_convert_within_a_family(value, a, b, expected) -> None:
    assert outcomes.convert(value, a, b) == pytest.approx(expected)


@pytest.mark.parametrize("a, b", [("USD millions", "percent"), ("USD per share", "USD millions"), ("none", "USD billions")])
def test_units_never_convert_across_families(a, b) -> None:
    with pytest.raises(ValueError):
        outcomes.convert(1.0, a, b)


def test_period_signal_ranks_lines_that_name_the_period() -> None:
    assert outcomes.period_signal("Fourth quarter revenue of $4.8 billion", 4, 2024) == 2
    assert outcomes.period_signal("Fourth quarter fiscal 2024 revenue of $4.8 billion", 4, 2024) == 3
    assert outcomes.period_signal("Third quarter revenue of $4.8 billion", 4, 2024) == 0
    assert outcomes.period_signal("Fiscal 2024 revenue was $19.5 billion", None, 2024) == 3


# --- stage 1: choosing what to ask -----------------------------------------


def test_the_results_release_is_read_and_the_notice_and_guidance_release_are_not(world) -> None:
    reqs, index, reasons = requests_for(draft())
    (cid,) = index
    _, lines = index[cid]
    assert reasons == {} and {l.accession for l in lines} == {"0000000123-24-000012"}  # not 0010 (guides it), not 0011 (notice)


def test_a_full_year_period_uses_the_fourth_quarter_release(world) -> None:
    _, index, _ = requests_for(draft(period="FY2024", metric="revenue"))
    (_, lines), = index.values()
    assert {l.accession for l in lines} == {"0000000123-24-000012"}
    assert any("Fiscal 2024 revenue was $19.5 billion" in l.sentence for l in lines)


def test_only_releases_filed_strictly_after_the_guidance_are_read(world) -> None:
    _, _, reasons = requests_for(draft(stated="2024-05-01"))  # the results release is filed that same day
    assert reasons == {"D1": "no later 8-K release for that period found"}


def test_lines_are_ranked_actuals_first_and_guidance_last(world) -> None:
    _, index, _ = requests_for(draft())
    (_, lines), = index.values()
    by_score = sorted(lines, key=lambda l: -l.score)
    assert by_score[0].sentence.startswith("Fourth quarter revenue of $4.8 billion")
    guidance = next(l for l in lines if l.sentence.startswith("Initiates first quarter"))
    assert all(guidance.score < l.score for l in lines if l is not guidance)  # strictly below every other line


def test_the_request_shows_the_guidance_and_numbered_lines(world) -> None:
    reqs, _, _ = requests_for(draft())
    user = reqs[0].user
    assert "guided revenue for Q4 FY2024 (stated 2024-02-01): 5 to 6 USD billions." in user
    assert "[1] filed 2024-05-01 | 0000000123-24-000012" in user and "LINE: Fourth quarter revenue of $4.8 billion" in user
    assert reqs[0].custom_id == "o-D1"


@pytest.mark.parametrize(
    "d, reason",
    [
        (draft(period="third quarter"), "not understood"),
        (draft(metric="gaap non"), "not recognised"),
        (draft(period="Q4 FY2030"), "no later 8-K release"),  # not reported (yet)
        (draft(metric="free cash flow"), "no line naming the metric"),
    ],
)
def test_a_draft_with_no_request_always_says_why(world, d, reason) -> None:
    reqs, _, reasons = requests_for(d)
    assert reqs == [] and reason in reasons["D1"]


# --- stage 1: checking what the model says ---------------------------------


def outcome_for(world, payload, d=None, ok=True):
    d = d or draft()
    reqs, index, _ = requests_for(d)
    res = {reqs[0].custom_id: llm.Result(reqs[0].custom_id, "succeeded" if ok else "errored", json.dumps(payload), 1, 1, None, "b")}
    return outcomes.derive_outcomes(index, res, {COMPANY.cik: store()}, OCFG), index[reqs[0].custom_id][1]


def line_no(lines, prefix):
    return next(i for i, l in enumerate(lines, 1) if l.sentence.startswith(prefix))


def test_a_good_answer_becomes_an_outcome_with_the_verbatim_line_as_evidence(world) -> None:
    reqs, index, _ = requests_for(draft())
    lines = index[reqs[0].custom_id][1]
    n = line_no(lines, "Fourth quarter revenue of $4.8 billion")
    (outs, rejects, reasons), _ = outcome_for(world, {"index": n, "value": 4.8, "unit": "USD billions"})
    o = outs["D1"]
    assert (o["reported_value"], o["reported_at"]) == (4.8, "2024-05-01")
    assert o["evidence"]["excerpt"].startswith("Fourth quarter revenue of $4.8 billion")
    assert o["evidence"]["accession_number"] == "0000000123-24-000012" and rejects == [] and reasons == {}


def test_a_value_in_another_unit_is_converted_by_code(world) -> None:
    reqs, index, _ = requests_for(draft())
    n = line_no(index[reqs[0].custom_id][1], "Fourth quarter revenue")
    (outs, _, _), _ = outcome_for(world, {"index": n, "value": 4800, "unit": "USD millions"})
    assert outs["D1"]["reported_value"] == pytest.approx(4.8)


@pytest.mark.parametrize(
    "payload, why",
    [
        ({"index": 1, "value": 4800, "unit": "USD billions"}, "mix-up"),  # 1000x the guidance
        ({"index": 1, "value": 4.8, "unit": "percent"}, "cannot convert"),
        ({"index": 99, "value": 4.8, "unit": "USD billions"}, "not usable"),
        ({"index": 1, "value": None, "unit": "USD billions"}, "not usable"),
        ({"nonsense": True}, "did not parse"),
    ],
)
def test_answers_that_fail_a_check_are_rejected_with_a_reason(world, payload, why) -> None:
    (outs, rejects, reasons), _ = outcome_for(world, payload)
    assert outs == {} and why in rejects[0]["reason"] and reasons["D1"] == "model output rejected"


def test_no_line_found_and_no_answer_are_different_reasons(world) -> None:
    (_, _, reasons), _ = outcome_for(world, {"index": None, "value": None, "unit": "none"})
    assert "found no line" in reasons["D1"]
    (_, _, reasons), _ = outcome_for(world, {}, ok=False)
    assert reasons["D1"] == "outcome search not completed"


# --- an outcome excerpt must report a result, not still be guidance --------


def test_excerpt_reports_a_result_is_false_for_still_forward_looking_guidance() -> None:
    """From data/outcomes/0001108524.jsonl, draft 01D5300100XWHQJGPYDMZ2QG94 (CRM, tax rate, Q4 FY2020): the
    outcome search had picked "(1) The Company's GAAP tax rate is expected to be approximately 72% for the
    three months ended April 30, 2020..." -- guidance for a LATER quarter, still in the future tense -- as if
    it reported the FY2020 actual."""
    assert not outcomes.excerpt_reports_a_result(
        "(1) The Company's GAAP tax rate is expected to be approximately 72% for the three months ended "
        "April 30, 2020, and for the year ended January 31, 2021."
    )


def test_excerpt_reports_a_result_is_true_with_a_past_tense_verb_even_when_it_also_says_guidance() -> None:
    """From data/outcomes/0000027419.jsonl, draft 01H0KG3A0009YTW3NZ8Q4YZRWP (TGT, EPS GAAP, Q2 FY2023): an
    outcome excerpt is allowed to say "guidance" as long as it also reports what happened."""
    assert outcomes.excerpt_reports_a_result(
        "Second quarter GAAP and Adjusted EPS1 of $1.80 was more than 4 times higher than a year ago and "
        "above the high end of the Company's guidance range, reflecting a meaningful profit recovery."
    )


def test_excerpt_reports_a_result_is_true_without_any_forward_looking_word() -> None:
    assert outcomes.excerpt_reports_a_result("Fourth quarter revenue of $4.8 billion, up 10% from a year ago.")


def test_an_outcome_line_that_is_still_forward_looking_is_rejected(world) -> None:
    """The line the model points at can itself be guidance for a later period: it names the metric, the
    period the model was asked about happens to be named too, and it has a number. Code must not trust it as
    an outcome just because the model pointed at it."""
    add_filing(world, "0021", "2024-05-02", "8-K",
               ["Fourth quarter revenue is expected to be $4.8 billion."],
               title="Example Corp Announces Financial Results for Fourth Quarter and Fiscal 2024")
    reqs, index, _ = requests_for(draft())
    n = line_no(index[reqs[0].custom_id][1], "Fourth quarter revenue is expected")
    (outs, rejects, reasons), _ = outcome_for(world, {"index": n, "value": 4.8, "unit": "USD billions"})
    assert outs == {} and "forward-looking" in rejects[0]["reason"] and reasons["D1"] == "model output rejected"


# --- a multi-column table line needs its column confirmed by a header ------


def test_is_ambiguous_multi_column_true_for_a_bare_dollar_table_row() -> None:
    """From data/drafts/0001045810.jsonl (NVDA): a results table with the current, prior-quarter and
    year-ago figures squashed onto one line, no label to say which is which."""
    assert outcomes.is_ambiguous_multi_column("USD billions", "Revenue $4,726 $3,866 $3,014 Up 22% Up 57%")


def test_is_ambiguous_multi_column_false_for_a_growth_rate_annotation() -> None:
    """From data/drafts/0001108524.jsonl (CRM): "Revenue of $31.4 Billion, up 18% Y/Y, up 22% CC" has three
    numbers, but only one is a dollar figure; the other two are percent growth rates, a different unit, and
    are not alternate readings of the metric's own value."""
    assert not outcomes.is_ambiguous_multi_column("USD billions", "FY23 Revenue of $31.4 Billion, up 18% Y/Y, up 22% CC")


def test_is_ambiguous_multi_column_false_with_comparison_language() -> None:
    """From data/drafts/0001045810.jsonl (NVDA): the opening sentence of a release ("...of $2.22 billion
    compared with $3.21 billion a year earlier and $2.21 billion...") has three dollar figures, but says in
    words which is which, unlike a bare table row."""
    assert not outcomes.is_ambiguous_multi_column(
        "USD billions",
        "NVIDIA today reported revenue for the first quarter of $2.22 billion compared with $3.21 billion "
        "a year earlier and $2.21 billion sequentially.",
    )


def test_is_ambiguous_multi_column_false_under_three_numbers() -> None:
    assert not outcomes.is_ambiguous_multi_column("USD billions", "Fourth quarter revenue of $4.8 billion, up 10% from a year ago.")


def test_find_result_header_reads_the_q_q_and_y_y_columns_a_guidance_header_would_reject() -> None:
    """From data/raw/0001045810 (NVDA): a results-table header ("($ in millions...) Q3 FY21 Q2 FY21 Q3 FY20
    Q/Q Y/Y") carries change columns that 02_candidates.find_table_header rejects a header for having, since
    there that means a results table rather than a guidance one. Confirming an outcome's column needs the
    opposite: a results header is exactly what is wanted, so those columns are not excluded here."""
    lines = ["($ in millions, except earnings per share) Q3 FY21 Q2 FY21 Q3 FY20 Q/Q Y/Y",
             "Revenue $4,726 $3,866 $3,014 Up 22% Up 57%"]
    header = outcomes.find_result_header(lines, 1, 10)
    assert header == lines[0] and (3, 2021) in cand.parse_periods(header)


def test_find_result_header_returns_none_past_the_lookback_or_without_a_period() -> None:
    assert outcomes.find_result_header(["Q3 FY21 Q2 FY21", "x", "x", "Revenue $1 $2"], 3, 1) is None
    assert outcomes.find_result_header(["Some heading", "Revenue $1 $2 $3"], 1, 10) is None


def test_column_confirmed_true_when_a_header_above_names_the_period(world) -> None:
    add_filing(world, "0021", "2024-05-02", "8-K", [
        "($ in millions, except earnings per share) Q4 FY24 Q3 FY24 Q4 FY23 Q/Q Y/Y",
        "Revenue $4,900 $4,800 $3,000 Up 2% Up 63%",
    ], title="Example Corp Announces Financial Results for Fourth Quarter and Fiscal 2024")
    st = store()
    meta = next(m for m in st.metas if m["accession"] == "0000000123-24-000021")
    doc = st.get(meta)
    line = outcomes.Line("0000000123-24-000021", "2024-05-02", doc.lines[2], doc.line_starts[2], [], None, None, 0)
    assert outcomes.column_confirmed(st, meta, line, (4, 2024), "USD millions", 10)
    assert not outcomes.column_confirmed(st, meta, line, (1, 2025), "USD millions", 10)  # a period the header does not name


def test_column_confirmed_false_without_a_header_above_the_row(world) -> None:
    add_filing(world, "0021", "2024-05-02", "8-K", ["Revenue $4,900 $4,800 $3,000 Up 2% Up 63%"],
               title="Example Corp Announces Financial Results for Fourth Quarter and Fiscal 2024")
    st = store()
    meta = next(m for m in st.metas if m["accession"] == "0000000123-24-000021")
    doc = st.get(meta)
    line = outcomes.Line("0000000123-24-000021", "2024-05-02", doc.lines[1], doc.line_starts[1], [], None, None, 0)
    assert not outcomes.column_confirmed(st, meta, line, (4, 2024), "USD millions", 10)


def test_column_confirmed_true_when_the_line_is_not_ambiguous() -> None:
    line = outcomes.Line("acc", "2024-05-02", "Fourth quarter revenue of $4.8 billion.", 0, [], None, None, 0)
    assert outcomes.column_confirmed(store(), {"accession": "acc"}, line, (4, 2024), "USD billions", 10)


def test_a_picked_multi_column_line_becomes_multi_column_unconfirmed(world) -> None:
    add_filing(world, "0021", "2024-05-02", "8-K", ["Revenue $4,900 $4,800 $3,000 Up 2% Up 63%"],
               title="Example Corp Announces Financial Results for Fourth Quarter and Fiscal 2024")
    reqs, index, _ = requests_for(draft())
    n = line_no(index[reqs[0].custom_id][1], "Revenue $4,900")
    (outs, rejects, reasons), _ = outcome_for(world, {"index": n, "value": 4900, "unit": "USD millions"})
    assert outs == {} and rejects == [] and reasons["D1"] == "multi-column, unconfirmed"


# --- stage 2: acknowledgements ---------------------------------------------


def test_only_a_missed_row_is_searched_for_an_acknowledgement(world) -> None:
    out_miss = {"reported_value": 4.8, "reported_at": "2024-05-01"}
    out_met = {"reported_value": 5.5, "reported_at": "2024-05-01"}
    assert outcomes.missed(draft(), out_miss) and not outcomes.missed(draft(), out_met)
    reqs, index = outcomes.build_ack_requests({COMPANY.cik: [draft()]}, {"D1": out_met}, {COMPANY.cik: store()}, OCFG, NUMBER, today=TODAY)
    assert reqs == []
    reqs, index = outcomes.build_ack_requests({COMPANY.cik: [draft()]}, {"D1": out_miss}, {COMPANY.cik: store()}, OCFG, NUMBER, today=TODAY)
    (cid,) = index
    assert cid == "k-D1" and [l.sentence for l in index[cid][1]] == ["Last quarter revenue of $4.8 billion was below our guidance."]
    assert "Actually reported: 4.8 USD billions, on 2024-05-01" in reqs[0].user


def test_an_acknowledgement_needs_a_line_with_the_metric_and_an_acknowledging_word(world) -> None:
    out = {"reported_value": 4.8, "reported_at": "2024-05-01"}
    reqs, index = outcomes.build_ack_requests({COMPANY.cik: [draft()]}, {"D1": out}, {COMPANY.cik: store()}, OCFG, NUMBER, today=TODAY)
    assert all(re.search("below|short of|missed|revised", l.sentence, re.I) for l in index["k-D1"][1])
    none = outcomes.build_ack_requests({COMPANY.cik: [draft(metric="free cash flow")]}, {"D1": out}, {COMPANY.cik: store()}, OCFG, NUMBER, today=TODAY)
    assert none[0] == []


def test_the_earliest_confirmed_line_is_the_acknowledgement(world) -> None:
    out = {"reported_value": 4.8, "reported_at": "2024-05-01"}
    _, index = outcomes.build_ack_requests({COMPANY.cik: [draft()]}, {"D1": out}, {COMPANY.cik: store()}, OCFG, NUMBER, today=TODAY)
    res = {"k-D1": llm.Result("k-D1", "succeeded", json.dumps({"indices": [1]}), 1, 1, None, "b")}
    found, rejects = outcomes.derive_acknowledgements(index, res, {COMPANY.cik: store()})
    assert found["D1"]["acknowledged_at"] == "2024-08-01" and rejects == []
    assert found["D1"]["evidence"]["excerpt"] == "Last quarter revenue of $4.8 billion was below our guidance."


def test_an_empty_or_out_of_range_answer_is_no_acknowledgement(world) -> None:
    out = {"reported_value": 4.8, "reported_at": "2024-05-01"}
    _, index = outcomes.build_ack_requests({COMPANY.cik: [draft()]}, {"D1": out}, {COMPANY.cik: store()}, OCFG, NUMBER, today=TODAY)
    for payload in ({"indices": []}, {"indices": [7]}):
        res = {"k-D1": llm.Result("k-D1", "succeeded", json.dumps(payload), 1, 1, None, "b")}
        assert outcomes.derive_acknowledgements(index, res, {COMPANY.cik: store()})[0] == {}
    bad = {"k-D1": llm.Result("k-D1", "succeeded", "yes", 1, 1, None, "b")}
    assert "did not parse" in outcomes.derive_acknowledgements(index, bad, {COMPANY.cik: store()})[1][0]["reason"]


# --- acknowledgement vocabulary differs by direction ------------------------
#
# 318 missed rows and zero acknowledgements: the acknowledgement phrase list had only shortfall words
# ("below", "short of", ...), but 243 of those 320 missed rows were beats (the reported value above the
# high end, not below the low end) -- a beat could never be found however plainly the company said it.


def test_a_beat_is_acknowledged_with_beat_vocabulary(world) -> None:
    """From data/review/0000027419.csv, draft 01H0KG3A0009YTW3NZ8Q4YZRWP (TGT, EPS GAAP, Q2 FY2023): guided
    1.3 to 1.7, reported 1.8, acknowledged in the very same 8-K with "above the high end of the Company's
    guidance range" -- vocabulary the shortfall-only phrase list could never match."""
    add_filing(world, "0021", "2024-05-01", "8-K", [
        "Second quarter GAAP and Adjusted EPS1 of $1.80 was more than 4 times higher than a year ago and "
        "above the high end of the Company's guidance range, reflecting a meaningful profit recovery from last year's inventory actions."
    ])
    d = draft(metric="EPS GAAP", unit="USD", low=1.3, high=1.7)
    out = {"reported_value": 1.8, "reported_at": "2024-05-01"}
    assert outcomes.missed(d, out) and rubric.direction(1.3, 1.7, 1.8) == "beat"
    reqs, index = outcomes.build_ack_requests({COMPANY.cik: [d]}, {"D1": out}, {COMPANY.cik: store()}, OCFG, NUMBER, today=TODAY)
    assert len(reqs) == 1 and "above the high end of the guided range (a beat)" in reqs[0].user
    (cid,) = index
    assert any("above the high end of the Company's guidance range" in l.sentence for l in index[cid][1])
    res = {cid: llm.Result(cid, "succeeded", json.dumps({"indices": [1]}), 1, 1, None, "b")}
    found, rejects = outcomes.derive_acknowledgements(index, res, {COMPANY.cik: store()})
    assert found["D1"]["acknowledged_at"] == "2024-05-01" and rejects == []


def test_a_shortfall_is_still_acknowledged_with_shortfall_vocabulary(world) -> None:
    """The direction split must not cost the shortfall side anything it already had. (A real Target FY2020 or
    Salesforce shortfall was checked directly against the cached filings for this: Target's FY2020 Q1 miss
    was guidance withdrawn before the quarter closed, not a plain miss, and Salesforce's FY2020 EPS collapse
    is reported later without referring back to the original guidance in these words. This fixture is
    synthetic for that reason, and exercises the same code path as the beat test above.)"""
    add_filing(world, "0021", "2024-05-01", "8-K", ["Fourth quarter revenue of $4.8 billion did not meet the low end of our guidance range."])
    d = draft()  # default range 5.0-6.0, a shortfall against 4.8
    out = {"reported_value": 4.8, "reported_at": "2024-05-01"}
    assert rubric.direction(5.0, 6.0, 4.8) == "shortfall"
    reqs, index = outcomes.build_ack_requests({COMPANY.cik: [d]}, {"D1": out}, {COMPANY.cik: store()}, OCFG, NUMBER, today=TODAY)
    assert len(reqs) == 1 and "below the low end of the guided range (a shortfall)" in reqs[0].user
    assert any("did not meet the low end of our guidance range" in l.sentence for l in index["k-D1"][1])


def test_beat_vocabulary_does_not_leak_into_a_shortfall_search_or_the_reverse(world) -> None:
    add_filing(world, "0021", "2024-05-01", "8-K", ["Fourth quarter revenue of $4.8 billion exceeded expectations by a wide margin."])
    d = draft()  # a shortfall: 4.8 is below the 5.0-6.0 range
    out = {"reported_value": 4.8, "reported_at": "2024-05-01"}
    _, index = outcomes.build_ack_requests({COMPANY.cik: [d]}, {"D1": out}, {COMPANY.cik: store()}, OCFG, NUMBER, today=TODAY)
    # the fixture's own "Last quarter revenue ... was below our guidance." is still a candidate; the new,
    # beat-worded sentence about the same period and metric is not, because this is a shortfall search
    sentences = [l.sentence for l in index["k-D1"][1]]
    assert "Last quarter revenue of $4.8 billion was below our guidance." in sentences
    assert not any("exceeded expectations" in s for s in sentences)


def test_an_acknowledgement_line_no_longer_needs_a_number_of_its_own(world) -> None:
    """The number requirement (needed for an outcome line, which must state a value) cut real acknowledgement
    candidates from 101 to 13 across the pilot data: most acknowledgement sentences narrate the gap without
    repeating the figure, which is already given in the request's own guidance line."""
    add_filing(world, "0021", "2024-05-01", "8-K", ["Fourth quarter revenue was well below the low end of our guidance range for the period."])
    d = draft()
    out = {"reported_value": 4.8, "reported_at": "2024-05-01"}
    _, index = outcomes.build_ack_requests({COMPANY.cik: [d]}, {"D1": out}, {COMPANY.cik: store()}, OCFG, NUMBER, today=TODAY)
    assert any("well below the low end" in l.sentence for l in index["k-D1"][1])


# --- main, end to end ------------------------------------------------------


@pytest.fixture
def config_path(world):
    cfg = common.load_config()
    cfg["companies"] = [{"name": COMPANY.name, "ticker": COMPANY.ticker, "cik": COMPANY.cik, "fiscal_year_end_month": 6}]
    path = world / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


def respond(params: dict) -> str:
    user = params["messages"][0]["content"]
    if params["system"] == outcomes.WITHDRAWAL_SYSTEM:
        hit = re.search(r"\[(\d+)\][^\[]*?LINE: [^\n]*withdrawing its fiscal 2024 guidance", user)
        return json.dumps({"indices": [int(hit.group(1))] if hit else []})
    if params["system"] == outcomes.ACK_SYSTEM:
        hit = re.search(r"\[(\d+)\][^\[]*?LINE: [^\n]*below our guidance", user)
        return json.dumps({"indices": [int(hit.group(1))] if hit else []})
    hit = re.search(r"\[(\d+)\][^\[]*?LINE: Fourth quarter revenue of \$4\.8 billion", user)
    return json.dumps({"index": int(hit.group(1)), "value": 4.8, "unit": "USD billions"} if hit else {"index": None, "value": None, "unit": "none"})


def seed_drafts(world, *drafts):
    (world / "drafts").mkdir()
    (world / "drafts" / f"{COMPANY.cik}.jsonl").write_text("".join(json.dumps(d) + "\n" for d in drafts))


def read_outcomes(world):
    return [json.loads(l) for l in (world / "outcomes" / f"{COMPANY.cik}.jsonl").read_text().splitlines()]


def test_a_dry_run_only_counts_tokens_and_writes_nothing(world, config_path, monkeypatch, capsys) -> None:
    """It used to write a row per draft with the reason there was no outcome. With stale or missing answers that overwrote real outcomes."""
    seed_drafts(world, draft(), draft("D2", period="Q4 FY2030"))
    client = fakes.FakeAnthropic(respond=respond)
    monkeypatch.setattr(llm, "make_client", lambda: client)
    assert outcomes.main(["--config", str(config_path)]) == 0
    assert client.messages.create_calls == [] and client.messages.batches.created == []
    assert not (world / "outcomes").exists()
    out = capsys.readouterr().out
    assert "nothing was written to data/outcomes/" in out and "EXMP: 2 drafts, 0 with an outcome, 0 acknowledged, 0 withdrawn" in out


def test_submit_runs_both_stages_and_only_a_miss_is_searched_for_an_acknowledgement(world, config_path, monkeypatch) -> None:
    seed_drafts(world, draft(), draft("D3", low=4.0, high=5.0))  # D3's range contains 4.8: a hit, so no ack stage for it
    client = fakes.FakeAnthropic(respond=respond)
    monkeypatch.setattr(llm, "make_client", lambda: client)
    assert outcomes.main(["--config", str(config_path), "--submit"]) == 0
    rows = {r["draft_id"]: r for r in read_outcomes(world)}
    assert rows["D1"]["outcome"]["reported_value"] == 4.8 and rows["D3"]["outcome"]["reported_value"] == 4.8
    assert rows["D1"]["acknowledgement"]["acknowledged_at"] == "2024-08-01"
    assert rows["D3"]["acknowledgement"] is None
    steps = {e["step"] for e in llm.Ledger().entries()}
    assert steps == {"04_outcomes", "04_acknowledgements"}
    archived_ack = llm.RawArchive("04_acknowledgements").load()
    assert set(archived_ack) == {"k-D1"}  # D3 was never sent


def test_a_rerun_sends_nothing_new(world, config_path, monkeypatch) -> None:
    seed_drafts(world, draft())
    monkeypatch.setattr(llm, "make_client", lambda: fakes.FakeAnthropic(respond=respond))
    outcomes.main(["--config", str(config_path), "--submit"])
    again = fakes.FakeAnthropic(respond=respond)
    monkeypatch.setattr(llm, "make_client", lambda: again)
    assert outcomes.main(["--config", str(config_path), "--submit"]) == 0
    assert again.messages.create_calls == [] and again.messages.batches.created == []
    assert read_outcomes(world)[0]["acknowledgement"] is not None


def test_submit_without_credentials_stops(world, config_path, monkeypatch, capsys) -> None:
    seed_drafts(world, draft())
    client = fakes.FakeAnthropic(authenticated=False)
    monkeypatch.setattr(llm, "make_client", lambda: client)
    assert outcomes.main(["--config", str(config_path), "--submit"]) == 3
    assert "no API credentials" in capsys.readouterr().err
    assert client.messages.create_calls == [] and client.messages.batches.created == []


def test_a_dry_run_leaves_no_empty_output_files(world, config_path, monkeypatch) -> None:
    seed_drafts(world)  # no drafts at all
    monkeypatch.setattr(llm, "make_client", lambda: fakes.FakeAnthropic(respond=respond))
    assert outcomes.main(["--config", str(config_path)]) == 0
    out = world / "outcomes"
    assert not out.exists() or all(p.stat().st_size > 0 for p in out.iterdir())


def test_every_configured_metric_has_a_term_group_so_none_falls_back_to_guessing() -> None:
    groups = OCFG["metric_terms"]
    for metric in CONFIG["metrics"]:
        assert any(re.search(g["when"], metric, re.IGNORECASE) for g in groups), f"{metric!r} has no metric_terms group"


def test_a_non_gaap_metric_ranks_the_non_gaap_row_above_the_gaap_one(world) -> None:
    raw = world / "raw" / COMPANY.cik
    body = ("<p>Example Corp Announces Financial Results for Fourth Quarter and Fiscal 2024</p><p>GAAP</p>"
            "<p>Gross margin 75.0 % 73.4 %</p><p>Non-GAAP</p><p>Gross margin 75.2 % 73.6 %</p>")
    html = f"<html><body>{body}</body></html>".encode()
    acc = "0000000123-24-000099"
    (raw / f"{acc}.html").write_bytes(html)
    (raw / f"{acc}.meta.json").write_text(json.dumps({
        "cik": COMPANY.cik, "accession": acc, "filing_type": "8-K", "filed_at": "2024-05-02", "http_status": 200,
        "final_url": "https://www.sec.gov/Archives/edgar/data/123/x/release.htm", "fetched_at": "2026-09-20T12:00:00+00:00",
        "content_sha256": hashlib.sha256(html).hexdigest()}))
    d = draft(metric="gross margin non-GAAP", unit="percent", low=74.0, high=76.0)
    _, index, _ = requests_for(d)
    lines = {l.sentence: l.score for l in index["o-D1"][1] if l.accession == acc}
    assert lines["Gross margin 75.2 % 73.6 %"] > lines["Gross margin 75.0 % 73.4 %"]


# --- helpers for the tests below ------------------------------------------------------------------------


def add_filing(world, suffix, filed, form, lines, title="Example Corp Announces Financial Results for Fourth Quarter and Fiscal 2024", cached=True):
    """One more cached filing of any form, in the same shape the fixture writes."""
    raw = world / "raw" / COMPANY.cik
    acc = f"0000000123-24-00{suffix}"
    html = ("<html><body>" + f"<p>{title}</p>" + "".join(f"<p>{line}</p>" for line in lines) + "</body></html>").encode()
    if cached:
        (raw / f"{acc}.html").write_bytes(html)
    (raw / f"{acc}.meta.json").write_text(json.dumps({
        "cik": COMPANY.cik, "accession": acc, "filing_type": form, "filed_at": filed, "http_status": 200,
        "final_url": f"https://www.sec.gov/Archives/edgar/data/123/{acc.replace('-', '')}/{form.lower()}.htm",
        "fetched_at": "2026-09-20T12:00:00+00:00", "content_sha256": hashlib.sha256(html).hexdigest()}))
    return acc


MISS = {"reported_value": 4.8, "reported_at": "2024-05-01"}
DATED = COMPANY.__class__(COMPANY.name, COMPANY.ticker, COMPANY.cik, 6)  # the fiscal year ends in June: Q4 FY2024 closes 2024-06-30


def ack_requests(d=None, out=MISS):
    return outcomes.build_ack_requests({COMPANY.cik: [d or draft()]}, {(d or draft())["draft_id"]: out}, {COMPANY.cik: store()}, OCFG, NUMBER, today=TODAY)


# --- acknowledgements are looked for in every cached filing --------------------------------------------------


def test_the_store_keeps_every_cached_filing_and_the_8k_exhibits_apart(world) -> None:
    q, k = add_filing(world, "0021", "2024-06-10", "10-Q", ["x"]), add_filing(world, "0022", "2024-06-20", "10-K", ["y"])
    st = store()
    assert {m["filing_type"] for m in st.metas} == {"8-K"}  # what the outcome and withdrawal searches read
    assert {m["filing_type"] for m in st.all_metas} == {"8-K", "10-Q", "10-K"} and {q, k} <= {m["accession"] for m in st.all_metas}
    assert [m["filed_at"] for m in st.all_metas] == sorted(m["filed_at"] for m in st.all_metas)  # oldest first


def test_a_filing_whose_html_is_not_in_the_cache_is_left_out_of_both_lists(world) -> None:
    gone = add_filing(world, "0023", "2024-06-11", "10-Q", ["x"], cached=False)
    assert gone not in {m["accession"] for m in store().all_metas} | {m["accession"] for m in store().metas}


def test_the_outcome_search_reads_8k_exhibits_only_even_when_a_10q_reports_the_period(world) -> None:
    """The 10-Q has a headline that names the period, so only the restriction to 8-K keeps it out."""
    q = add_filing(world, "0021", "2024-05-15", "10-Q", ["Fourth quarter revenue of $4.9 billion."])
    _, index, _ = requests_for(draft())
    assert {line.accession for _, lines in index.values() for line in lines}.isdisjoint({q})
    assert {line.accession for _, lines in index.values() for line in lines} == {"0000000123-24-000012"}


def test_the_acknowledgement_search_reads_the_10q_and_the_10k_as_well_as_the_8k_exhibits(world) -> None:
    q = add_filing(world, "0021", "2024-06-10", "10-Q", ["Revenue for the fourth quarter of fiscal 2024 was $4.8 billion, below our guidance."])
    k = add_filing(world, "0022", "2024-06-20", "10-K", ["Fourth quarter revenue of $4.8 billion was below the low end of our guidance range."])
    reqs, index = ack_requests()
    lines = index["k-D1"][1]
    assert [l.accession for l in lines] == [q, k, "0000000123-24-000013"]  # oldest first, across the three forms
    assert [l.filed_at for l in lines] == ["2024-06-10", "2024-06-20", "2024-08-01"]
    assert all(f"[{n}] filed" in reqs[0].user for n in (1, 2, 3))


def test_a_confirmed_10q_line_is_the_acknowledgement_and_the_evidence_is_that_10q(world) -> None:
    q = add_filing(world, "0021", "2024-06-10", "10-Q", ["Revenue for the fourth quarter of fiscal 2024 was $4.8 billion, below our guidance."])
    _, index = ack_requests()
    res = {"k-D1": llm.Result("k-D1", "succeeded", json.dumps({"indices": [1, 2]}), 1, 1, None, "b")}
    found, rejects = outcomes.derive_acknowledgements(index, res, {COMPANY.cik: store()})
    ev = found["D1"]["evidence"]
    assert found["D1"]["acknowledged_at"] == "2024-06-10" and rejects == []
    assert (ev["filing_type"], ev["accession_number"]) == ("10-Q", q) and ev["source_url"].endswith("/10-q.htm")
    assert ev["excerpt"] == "Revenue for the fourth quarter of fiscal 2024 was $4.8 billion, below our guidance."


def test_a_10k_line_is_the_acknowledgement_when_it_is_the_one_confirmed(world) -> None:
    k = add_filing(world, "0022", "2024-06-20", "10-K", ["Fourth quarter revenue of $4.8 billion was below the low end of our guidance range."])
    _, index = ack_requests()
    (n,) = [i for i, l in enumerate(index["k-D1"][1], 1) if l.accession == k]
    res = {"k-D1": llm.Result("k-D1", "succeeded", json.dumps({"indices": [n]}), 1, 1, None, "b")}
    found, _ = outcomes.derive_acknowledgements(index, res, {COMPANY.cik: store()})
    assert (found["D1"]["acknowledged_at"], found["D1"]["evidence"]["filing_type"]) == ("2024-06-20", "10-K")


def test_only_filings_after_the_outcome_and_within_the_window_are_read_for_an_acknowledgement(world) -> None:
    before = add_filing(world, "0021", "2024-04-20", "10-Q", ["Revenue of $4.8 billion for the quarter was below our guidance."])
    inside = add_filing(world, "0022", "2025-06-01", "10-K", ["Revenue of $4.8 billion for the quarter was below our guidance."])  # 396 days after
    beyond = add_filing(world, "0023", "2025-06-20", "10-K", ["Revenue of $4.8 billion for the quarter was below our guidance."])  # 415 days after
    accessions = {l.accession for l in ack_requests()[1]["k-D1"][1]}
    assert inside in accessions and before not in accessions and beyond not in accessions


def test_the_ack_step_reads_a_10q_end_to_end_and_the_outcome_step_still_does_not(world, config_path, monkeypatch) -> None:
    """The only line that admits the miss is in a 10-Q. Without the 10-Q there is no acknowledgement to find."""
    # The fixture's own 8-K that admits the miss (2024-08-01) is rewritten so that it no longer does.
    (world / "raw" / COMPANY.cik / "0000000123-24-000013.html").write_bytes(b"<html><body><p>Example Corp Announces Financial Results for First Quarter Fiscal 2025</p><p>First quarter revenue of $5.3 billion.</p></body></html>")
    add_filing(world, "0021", "2024-06-10", "10-Q", ["Revenue for the fourth quarter of fiscal 2024 was $4.8 billion, below our guidance."])
    seed_drafts(world, draft())
    monkeypatch.setattr(llm, "make_client", lambda: fakes.FakeAnthropic(respond=respond))
    assert outcomes.main(["--config", str(config_path), "--submit"]) == 0
    (row,) = read_outcomes(world)
    assert row["outcome"]["evidence"]["filing_type"] == "8-K"  # the outcome came from an 8-K exhibit
    assert row["acknowledgement"]["evidence"]["filing_type"] == "10-Q" and row["acknowledgement"]["acknowledged_at"] == "2024-06-10"


# --- withdrawals ---------------------------------------------------------------------------------------------

PHRASES = re.compile("|".join(f"(?:{p})" for p in OCFG["withdrawal_phrases"]), re.IGNORECASE)
SUBJECTS = re.compile(OCFG["withdrawal_subjects"], re.IGNORECASE)


@pytest.mark.parametrize("sentence", [
    "The Company is withdrawing its fiscal 2024 guidance.", "Target has withdrawn its outlook for the year.", "NVIDIA withdrew its forecast.",
    "We are suspending our full-year outlook.", "The Company is suspending guidance until further notice.", "Suspension of guidance reflects the uncertainty.",
    "The company is no longer providing financial guidance.", "We will no longer provide an outlook for the second quarter.",
])
def test_a_sentence_is_read_when_it_says_withdraw_suspend_or_no_longer_providing_about_guidance(sentence) -> None:
    assert PHRASES.search(sentence) and SUBJECTS.search(sentence)


@pytest.mark.parametrize("sentence", [
    "The Company is suspending its quarterly dividend.", "It suspended operations at two plants.", "The Company reaffirmed its guidance.",
    "We are raising our outlook.", "Withdrawal of the offer is expected.", "The Board authorized a share repurchase program.",
])
def test_a_sentence_about_something_else_or_about_guidance_without_a_withdrawal_is_not_read(sentence) -> None:
    assert not (PHRASES.search(sentence) and SUBJECTS.search(sentence))


def withdrawal_world(world):
    """Guidance stated 2024-02-01 for Q4 FY2024, which closes 2024-06-30 for a June year end."""
    inside = add_filing(world, "0014", "2024-04-20", "8-K", ["The Company is withdrawing its fiscal 2024 guidance.", "The Company is suspending its quarterly dividend.",
                                                            "We are no longer providing an outlook for the fourth quarter."], title="Example Corp Update")
    return inside


def withdrawal_requests(d=None, company=DATED):
    d = d or draft()
    return outcomes.build_withdrawal_requests({COMPANY.cik: [d]}, {COMPANY.cik: company}, {COMPANY.cik: store()}, OCFG, today=TODAY)


def test_only_8k_exhibits_filed_after_the_guidance_and_by_the_close_of_the_period_are_searched(world) -> None:
    inside = withdrawal_world(world)
    add_filing(world, "0015", "2024-07-10", "8-K", ["The Company is withdrawing its guidance."], title="Example Corp Update")  # after the close
    add_filing(world, "0016", "2024-04-25", "10-Q", ["The Company is withdrawing its guidance."])  # not an 8-K exhibit
    add_filing(world, "0017", "2024-02-01", "8-K", ["The Company is withdrawing its guidance."], title="Example Corp Update")  # the day it was stated
    add_filing(world, "0018", "2024-06-30", "8-K", ["The Company is suspending its guidance."], title="Example Corp Update")  # the last day of the period
    reqs, index = withdrawal_requests()
    (cid,) = index
    lines = index[cid][1]
    assert cid == "w-D1" and [(l.accession, l.sentence) for l in lines] == [
        (inside, "The Company is withdrawing its fiscal 2024 guidance."), (inside, "We are no longer providing an outlook for the fourth quarter."),
        ("0000000123-24-000018", "The Company is suspending its guidance.")]
    assert index[cid][2] == date(2024, 6, 30)


def test_the_window_closes_with_the_period_and_not_with_today(world) -> None:
    add_filing(world, "0015", "2024-07-10", "8-K", ["The Company is withdrawing its guidance."], title="Example Corp Update")
    assert withdrawal_requests(draft())[1] == {}  # Q4 FY2024 closed on 2024-06-30
    _, index = withdrawal_requests(draft("D5", period="Q1 FY2025"))  # closes 2024-09-30
    assert [l.filed_at for l in index["w-D5"][1]] == ["2024-07-10"]


def test_a_draft_that_cannot_be_dated_is_not_searched(world) -> None:
    withdrawal_world(world)
    assert withdrawal_requests(company=COMPANY)[0] == []  # no fiscal calendar
    assert withdrawal_requests(draft("D6", period="Q4 FY2030"), company=DATED)[1] != {}  # (dated, so searched)
    d = draft("D7")
    d["assumption"] = {**d["assumption"], "target_period": "second half"}
    assert withdrawal_requests(d)[0] == []  # a period that is not understood


def test_a_draft_with_no_candidate_sentence_gets_no_request(world) -> None:
    assert withdrawal_requests()[0] == []  # the fixture's filings say nothing of the kind


def test_the_request_shows_the_guidance_the_close_and_the_numbered_sentences(world) -> None:
    withdrawal_world(world)
    (req,), _ = withdrawal_requests()
    assert req.system == outcomes.WITHDRAWAL_SYSTEM and req.schema == outcomes.ACK_SCHEMA and req.max_tokens == outcomes.MAX_TOKENS
    user = req.user
    assert "guided revenue for Q4 FY2024 to 5 to 6 USD billions (stated 2024-02-01). The period ends 2024-06-30." in user
    assert "[1] filed 2024-04-20" in user and "LINE: The Company is withdrawing its fiscal 2024 guidance." in user
    assert "LINE: The Company is suspending its quarterly dividend." not in user  # it is only the context before the next line


def test_the_prompt_says_what_a_withdrawal_is_and_is_not() -> None:
    text = outcomes.WITHDRAWAL_SYSTEM
    for rule in ["Suspending a dividend, a share repurchase program, an operation or a service does not count",
                 "Withdrawing guidance for another metric, or only for another period, does not count",
                 "Raising, lowering, updating or reaffirming guidance is not withdrawing it",
                 "may withdraw or update guidance in future"]:
        assert rule in text


def test_the_earliest_confirmed_sentence_is_the_withdrawal_and_the_evidence_is_verbatim(world) -> None:
    inside = withdrawal_world(world)
    _, index = withdrawal_requests()
    res = {"w-D1": llm.Result("w-D1", "succeeded", json.dumps({"indices": [2, 1]}), 1, 1, None, "b")}
    found, rejects = outcomes.derive_withdrawals(index, res, {COMPANY.cik: store()})
    w = found["D1"]
    assert (w["withdrawn_at"], w["period_close"]) == ("2024-04-20", "2024-06-30") and rejects == []
    assert w["evidence"]["excerpt"] == "The Company is withdrawing its fiscal 2024 guidance."
    assert (w["evidence"]["filing_type"], w["evidence"]["accession_number"]) == ("8-K", inside)


@pytest.mark.parametrize("payload", [{"indices": []}, {"indices": [9]}, {"indices": [0]}, {"indices": ["1"]}])
def test_an_empty_or_out_of_range_answer_is_no_withdrawal(world, payload) -> None:
    withdrawal_world(world)
    _, index = withdrawal_requests()
    res = {"w-D1": llm.Result("w-D1", "succeeded", json.dumps(payload), 1, 1, None, "b")}
    assert outcomes.derive_withdrawals(index, res, {COMPANY.cik: store()})[0] == {}


def test_an_answer_that_does_not_parse_is_rejected_and_an_unanswered_request_is_no_withdrawal(world) -> None:
    withdrawal_world(world)
    _, index = withdrawal_requests()
    bad = {"w-D1": llm.Result("w-D1", "succeeded", "yes", 1, 1, None, "b")}
    found, rejects = outcomes.derive_withdrawals(index, bad, {COMPANY.cik: store()})
    assert found == {} and "did not parse" in rejects[0]["reason"]
    failed = {"w-D1": llm.Result("w-D1", "errored", None, 0, 0, "overloaded", "b")}
    assert outcomes.derive_withdrawals(index, failed, {COMPANY.cik: store()}) == ({}, []) == outcomes.derive_withdrawals(index, {}, {COMPANY.cik: store()})


def test_a_sentence_dated_after_the_close_is_never_a_withdrawal_whatever_the_model_says(world) -> None:
    """The window already keeps these out. This is the rubric's own test, applied to what comes back."""
    withdrawal_world(world)
    _, index = withdrawal_requests()
    d, lines, close = index["w-D1"]
    late = outcomes.Line(lines[0].accession, "2024-07-01", lines[0].sentence, lines[0].char_start, [], None, None, 0)
    res = {"w-D1": llm.Result("w-D1", "succeeded", json.dumps({"indices": [1]}), 1, 1, None, "b")}
    assert outcomes.derive_withdrawals({"w-D1": (d, [late], close)}, res, {COMPANY.cik: store()})[0] == {}


# --- the withdrawal stage through main -----------------------------------------------------------------------


def test_submit_runs_the_withdrawal_stage_and_puts_the_withdrawal_on_the_row(world, config_path, monkeypatch, capsys) -> None:
    withdrawal_world(world)
    d4 = draft("D4", period="Q1 FY2024")  # closed before the guidance was stated: never searched
    seed_drafts(world, draft(), draft("D3", low=4.0, high=5.0), d4)
    client = fakes.FakeAnthropic(respond=respond)
    monkeypatch.setattr(llm, "make_client", lambda: client)
    assert outcomes.main(["--config", str(config_path), "--submit"]) == 0
    rows = {r["draft_id"]: r for r in read_outcomes(world)}
    assert rows["D1"]["withdrawal"]["withdrawn_at"] == "2024-04-20" and rows["D3"]["withdrawal"]["period_close"] == "2024-06-30"
    assert rows["D4"]["withdrawal"] is None
    assert set(llm.RawArchive("04_withdrawals").load()) == {"w-D1", "w-D3"}  # D4 was never sent
    out = capsys.readouterr().out
    assert "04_withdrawals: 2 requests" in out and "budget $10.00" in out  # the cost gate is printed before the stage
    assert "EXMP: 3 drafts" in out and "2 withdrawn" in out


def test_the_withdrawal_stage_can_be_run_alone(world, config_path, monkeypatch) -> None:
    withdrawal_world(world)
    seed_drafts(world, draft())
    monkeypatch.setattr(llm, "make_client", lambda: fakes.FakeAnthropic(respond=respond))
    assert outcomes.main(["--config", str(config_path), "--stage", "withdrawal", "--submit"]) == 0
    assert {e["step"] for e in llm.Ledger().entries()} == {"04_withdrawals"}
    (row,) = read_outcomes(world)
    assert row["withdrawal"] is not None and row["outcome"] is None


def test_a_rerun_sends_no_withdrawal_request_twice(world, config_path, monkeypatch) -> None:
    withdrawal_world(world)
    seed_drafts(world, draft())
    monkeypatch.setattr(llm, "make_client", lambda: fakes.FakeAnthropic(respond=respond))
    outcomes.main(["--config", str(config_path), "--submit"])
    again = fakes.FakeAnthropic(respond=respond)
    monkeypatch.setattr(llm, "make_client", lambda: again)
    assert outcomes.main(["--config", str(config_path), "--submit"]) == 0
    assert again.messages.create_calls == [] and again.messages.batches.created == []
    assert read_outcomes(world)[0]["withdrawal"] is not None


def test_every_withdrawal_pattern_in_the_config_compiles_and_is_used(world) -> None:
    assert len(OCFG["withdrawal_phrases"]) >= 3 and all(re.compile(p) for p in OCFG["withdrawal_phrases"]) and re.compile(OCFG["withdrawal_subjects"])


# --- a batch that has not ended, a dry run, a re-run: nothing is lost or overwritten -------------------------


def outcomes_snapshot(world):
    folder = world / "outcomes"
    return {p.name: p.read_bytes() for p in sorted(folder.glob("*"))} if folder.exists() else {}


def two_drafts(world):
    seed_drafts(world, draft(), draft("D3", low=4.0, high=5.0))


def test_a_batch_that_has_not_ended_writes_nothing_to_data_outcomes_and_exits_5(world, config_path, monkeypatch, capsys) -> None:
    two_drafts(world)
    client = fakes.FakeAnthropic(respond=respond, batch_ready=False)
    monkeypatch.setattr(llm, "make_client", lambda: client)
    assert outcomes.main(["--config", str(config_path), "--submit", "--wait-minutes", "0"]) == 5
    assert not (world / "outcomes").exists()
    err = capsys.readouterr().err
    assert "has not ended, so nothing was written to data/outcomes/" in err and "04_outcomes" in err
    assert llm.RawArchive("04_outcomes").state_path.exists()


def test_a_pending_batch_leaves_complete_outcomes_byte_for_byte_alone(world, config_path, monkeypatch) -> None:
    two_drafts(world)
    monkeypatch.setattr(llm, "make_client", lambda: fakes.FakeAnthropic(respond=respond))
    outcomes.main(["--config", str(config_path), "--submit"])
    before = outcomes_snapshot(world)
    assert before[f"{COMPANY.cik}.jsonl"]
    monkeypatch.setattr(outcomes, "OUTCOME_SYSTEM", outcomes.OUTCOME_SYSTEM + "\n- Be careful.")  # every answer is stale
    monkeypatch.setattr(llm, "make_client", lambda: fakes.FakeAnthropic(respond=respond, batch_ready=False))
    assert outcomes.main(["--config", str(config_path), "--submit", "--wait-minutes", "0"]) == 5
    assert outcomes_snapshot(world) == before


def test_resuming_a_pending_batch_writes_every_outcome_and_not_just_the_batch(world, config_path, monkeypatch) -> None:
    two_drafts(world)
    client = fakes.FakeAnthropic(respond=respond, batch_ready=False)
    monkeypatch.setattr(llm, "make_client", lambda: client)
    assert outcomes.main(["--config", str(config_path), "--submit", "--wait-minutes", "0"]) == 5
    client.batch_ready = True
    assert outcomes.main(["--config", str(config_path), "--submit", "--wait-minutes", "0"]) == 0
    rows = {r["draft_id"]: r for r in read_outcomes(world)}
    assert rows["D1"]["outcome"]["reported_value"] == 4.8 and rows["D3"]["outcome"]["reported_value"] == 4.8  # D1 was the canary
    assert len(client.messages.batches.created) >= 1 and all(len(b) == 1 for b in client.messages.batches.created[:1])


def test_rerunning_after_a_failed_request_keeps_the_outcomes_the_first_run_found(world, config_path, monkeypatch) -> None:
    two_drafts(world)
    monkeypatch.setattr(llm, "make_client", lambda: fakes.FakeAnthropic(respond=respond, fail_ids=("o-D3",)))
    assert outcomes.main(["--config", str(config_path), "--submit"]) == 0
    rows = {r["draft_id"]: r for r in read_outcomes(world)}
    assert rows["D1"]["outcome"] is not None and rows["D3"]["outcome"] is None
    monkeypatch.setattr(llm, "make_client", lambda: fakes.FakeAnthropic(respond=respond))
    assert outcomes.main(["--config", str(config_path), "--submit"]) == 0
    rows = {r["draft_id"]: r for r in read_outcomes(world)}
    assert rows["D1"]["outcome"] is not None and rows["D3"]["outcome"] is not None  # D1 used to come back empty: the re-run only sent D3


def test_a_dry_run_leaves_existing_outcomes_byte_for_byte_alone(world, config_path, monkeypatch) -> None:
    two_drafts(world)
    monkeypatch.setattr(llm, "make_client", lambda: fakes.FakeAnthropic(respond=respond))
    outcomes.main(["--config", str(config_path), "--submit"])
    before = outcomes_snapshot(world)
    monkeypatch.setattr(outcomes, "OUTCOME_SYSTEM", outcomes.OUTCOME_SYSTEM + "\n- Be careful.")  # the archived answers no longer answer these requests
    assert outcomes.main(["--config", str(config_path)]) == 0
    assert outcomes_snapshot(world) == before


def test_stopping_for_a_missing_credential_or_the_budget_writes_nothing(world, config_path, monkeypatch) -> None:
    two_drafts(world)
    monkeypatch.setattr(llm, "make_client", lambda: fakes.FakeAnthropic(authenticated=False))
    assert outcomes.main(["--config", str(config_path), "--submit"]) == 3 and not (world / "outcomes").exists()
