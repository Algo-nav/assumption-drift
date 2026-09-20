"""The seven fixes from the second pilot review, each tested against the pilot rows that called for it.

The fixture, fixtures/pilot_run2.json, is the 30-request pilot that ran after the block and metrics fixes
(NVIDIA, Target and Salesforce, 10 requests each): the candidates that were sent, the model's raw answer to
each, the drafts 03 wrote, and `wrong`, an audit of those drafts against the filings. The repo does not record
which rows were flagged in the review itself, so `wrong` is a re-audit: 13 rows with a wrong value, metric or
period, and 5 whose values are in the filing but whose excerpt is the wrong sentence.

Every guard is exercised twice: directly, on the smallest input that shows it, and by replaying the pilot's
own raw answers through `derive_drafts` as the code is today. Nothing here calls a model or touches data/.
"""

from __future__ import annotations

import importlib
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from pipeline import llm
from research_record.schema import Evidence
from research_record.text import html_to_text

structure = importlib.import_module("pipeline.03_structure")
candidates = importlib.import_module("pipeline.02_candidates")
review = importlib.import_module("pipeline.05_review")
common = importlib.import_module("pipeline.common")

PILOT = json.loads((Path(__file__).parent / "fixtures" / "pilot_run2.json").read_text(encoding="utf-8"))
CONFIG = common.load_config()
METRICS, KINDS = CONFIG["metrics"], CONFIG["metric_kinds"]
COMPANIES = {c["ticker"]: common.Company(c["name"], c["ticker"], c["cik"]) for c in CONFIG["companies"]}
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


def replay(cid: str, answer: dict | str | None = None, *, candidate: dict | None = None):
    """One pilot request pushed back through derive_drafts. `answer` replaces the model's raw answer."""
    cand = candidate or {k: v for k, v in CANDS[cid].items() if k not in ("ticker", "custom_id")}
    company = COMPANIES[CANDS[cid]["ticker"]]
    text = PILOT["raw"][cid] if answer is None else (answer if isinstance(answer, str) else json.dumps(answer))
    drafts, rejects, _ = structure.derive_drafts({cid: (company, cand)}, {cid: llm.Result(cid, "succeeded", text, 1, 1, None, "b")}, METRICS, KINDS)
    return drafts.get(company.cik, []), rejects.get(company.cik, [])


def raw_items(cid: str) -> list[dict]:
    return json.loads(PILOT["raw"][cid])["items"]


def raw_item(w: dict) -> dict:
    return next(i for i in raw_items(w["custom_id"]) if (i["metric"], i["target_period"]) == (w["metric"], w["target_period"]))


def produced(drafts: list[dict], w: dict) -> bool:
    return any((d["assumption"]["metric"], d["assumption"]["target_period"]) == (w["metric"], w["target_period"]) for d in drafts)


def reason(rejects: list[dict], w: dict) -> str:
    return next(r["reason"] for r in rejects if (r["item"]["metric"], r["item"]["target_period"]) == (w["metric"], w["target_period"]))


def wrong_by(fix: int) -> list[dict]:
    return [w for w in WRONG if fix in w["fixes"]]


def row_id(w: dict) -> str:
    return f"{CANDS[w['custom_id']]['ticker']}-{CANDS[w['custom_id']]['filed_at']}-{w['metric'].replace(' ', '_')}-{w['target_period'].replace(' ', '_')}"


# --- the fixture -------------------------------------------------------------


def test_the_fixture_is_the_thirty_request_pilot_and_its_audit() -> None:
    assert len(PILOT["candidates"]) == 30 and set(PILOT["raw"]) == set(CANDS)
    assert {c["ticker"] for c in PILOT["candidates"]} == {"NVDA", "TGT", "CRM"}
    assert len(PILOT["drafts"]) == 78
    assert (len(WRONG), sum(w["kind"] == "wrong-value" for w in WRONG), sum(w["kind"] == "wrong-evidence" for w in WRONG)) == (18, 13, 5)
    frozen = {(d["custom_id"], d["assumption"]["metric"], d["assumption"]["target_period"]) for d in PILOT["drafts"]}
    assert all((w["custom_id"], w["metric"], w["target_period"]) in frozen for w in WRONG)  # every wrong row was really drafted


# --- fix 1: operating margin, and a unit that fits the metric ---------------------


def test_operating_margin_is_a_metric_with_a_gaap_and_a_non_gaap_entry() -> None:
    assert "operating margin GAAP" in METRICS and "operating margin non-GAAP" in METRICS
    assert KINDS["operating margin GAAP"] == KINDS["operating margin non-GAAP"] == "percent"
    assert KINDS["operating income"] == "dollars"  # the amount, never the margin


def test_every_metric_has_a_kind_and_every_kind_is_one_a_unit_can_have() -> None:
    assert set(KINDS) == set(METRICS)
    assert set(KINDS.values()) <= set(structure.UNIT_KIND.values())
    assert set(structure.UNIT_KIND) == set(structure.UNITS)


@pytest.mark.parametrize("metric", [m for m, k in KINDS.items() if k == "dollars"])
@pytest.mark.parametrize("unit", ["percent", "percentage points", "basis points", "USD per share"])
def test_a_dollar_metric_rejects_a_percent_and_a_per_share_unit(metric, unit) -> None:
    with pytest.raises(ValueError, match="is measured in dollars"):
        structure.check_unit_kind(metric, unit, KINDS)


@pytest.mark.parametrize("metric", [m for m, k in KINDS.items() if k == "percent"])
@pytest.mark.parametrize("unit", ["USD", "USD thousands", "USD millions", "USD billions", "USD per share"])
def test_a_percent_metric_rejects_dollars(metric, unit) -> None:
    with pytest.raises(ValueError, match="is measured in percent"):
        structure.check_unit_kind(metric, unit, KINDS)


@pytest.mark.parametrize(
    "metric, unit",
    [("revenue", "USD billions"), ("operating income", "USD millions"), ("free cash flow", "USD"), ("other income and expense", "USD millions"),
     ("operating margin GAAP", "percent"), ("gross margin non-GAAP", "basis points"), ("tax rate", "percent"), ("comparable sales", "percent"),
     ("EPS GAAP", "USD per share"), ("EPS non-GAAP", "USD per share")],
)
def test_a_unit_that_fits_the_metric_passes(metric, unit) -> None:
    structure.check_unit_kind(metric, unit, KINDS)


def test_eps_is_dollars_per_share_and_not_plain_dollars() -> None:
    with pytest.raises(ValueError, match="is measured in per share"):
        structure.check_unit_kind("EPS GAAP", "USD", KINDS)


@pytest.mark.parametrize("w", [w for w in wrong_by(1)], ids=row_id)
def test_the_pilots_margins_and_growth_rates_filed_as_dollar_metrics_are_rejected(w) -> None:
    assert raw_item(w)["unit"] == "percent" and KINDS[w["metric"]] == "dollars"  # what the model did
    drafts, rejects = replay(w["custom_id"])
    assert not produced(drafts, w)
    assert "is measured in dollars, but the unit is 'percent'" in reason(rejects, w)


def test_the_same_pilot_evidence_is_accepted_once_it_is_named_as_the_margin_it_is() -> None:
    w = next(w for w in WRONG if w["metric"] == "operating income" and w["target_period"] == "FY2023")
    answer = {"items": [{**raw_item(w), "metric": "operating margin GAAP"}]}
    drafts, rejects = replay(w["custom_id"], answer)
    assert [(d["assumption"]["metric"], d["assumption"]["target_low"], d["assumption"]["unit"]) for d in drafts] == [("operating margin GAAP", 3.8, "percent")]
    assert drafts[0]["assumption"]["evidence"]["excerpt"] == "GAAP operating margin(1)\n~3.8%"


@pytest.mark.parametrize("block", [False, True])
def test_the_prompt_names_operating_margin_and_tells_a_margin_from_an_amount(block) -> None:
    text = structure.system_prompt(METRICS, block=block)
    assert '"operating margin GAAP"' in text and '"operating margin non-GAAP"' in text
    assert '"Operating margin" is never "operating income"' in text and "free cash flow growth 9% - 10%" in text


# --- fix 2: every number the item states is in its evidence ----------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("GAAP operating expenses $ 1,535", {1535.0}),
        ("$1.30 - $1.70 $7.75 - $8.75", {1.3, 1.7, 7.75, 8.75}),
        ("($0.03) - ($0.02) $0.38 - $0.40", {0.03, 0.02, 0.38, 0.4}),
        ("GAAP earnings (loss) per share range(1)(2)\n($0.03) - ($0.02)", {0.03, 0.02}),  # (1)(2) are footnotes
        ("Non-GAAP operating margin(1)\n~20.4%", {20.4}),
        ("Stock-based compensation expense (200)", {200.0}),  # a negative in brackets is a value
        ("FY24: high end of range $34B to $35B", {24.0, 34.0, 35.0}),
        ("Revenue is expected to be $65.0 billion, plus or minus 2%.", {65.0, 2.0}),
    ],
)
def test_numbers_in_reads_what_is_printed(text, expected) -> None:
    assert structure.numbers_in(text) == expected


def test_a_number_that_is_printed_passes_whatever_its_sign_or_format() -> None:
    structure.check_numbers_in_evidence({"value_low": 2.2, "value_high": 2.2}, "expected to be $2.20 billion")
    structure.check_numbers_in_evidence({"value_low": -0.03, "value_high": -0.02}, "($0.03) - ($0.02)")
    structure.check_numbers_in_evidence({"value_low": -10.0, "value_high": -10.0}, "an expense of approximately $10 million")
    structure.check_numbers_in_evidence({"value_low": 1535.0, "value_high": 1535.0}, "GAAP operating expenses $ 1,535")
    structure.check_numbers_in_evidence({"value_low": 1.3, "value_high": None}, "$1.30+(a)")  # "at least"
    structure.check_numbers_in_evidence({"value_low": 65.0, "value_high": 65.0, "plus_minus": 2.0}, "$65.0 billion, plus or minus 2%")


@pytest.mark.parametrize(
    "item, evidence",
    [
        ({"value_low": 8.6, "value_high": 9.6}, "the second quarter ... GAAP and Adjusted EPS of $1.95 to $2.35."),  # from the next line
        ({"value_low": 22.0, "value_high": 22.0}, "the Company used a projected non-GAAP tax rate of 23.5% and 21.5%"),
        ({"value_low": 2.0, "value_high": 2.0}, "GAAP operating margin(2)\n~20.4%"),  # a footnote marker is not a 2
        ({"value_low": 12.0, "value_high": 12.0}, "revenue of $112.0 billion"),  # 12 is not inside 112.0
        ({"value_low": 5.0, "value_high": 6.0}, "revenue of $5.0 billion"),  # one end printed, the other not
    ],
)
def test_a_number_that_is_not_printed_in_the_evidence_is_rejected(item, evidence) -> None:
    with pytest.raises(ValueError, match="is not in the evidence text"):
        structure.check_numbers_in_evidence(item, evidence)


@pytest.mark.parametrize("w", wrong_by(2), ids=row_id)
def test_the_pilots_figures_taken_from_the_neighbouring_line_are_rejected(w) -> None:
    item = raw_item(w)
    cand = CANDS[w["custom_id"]]
    assert not {abs(item["value_low"]), abs(item["value_high"])} <= structure.numbers_in(cand["sentence"])  # not in what it cites
    drafts, rejects = replay(w["custom_id"])
    assert not produced(drafts, w)
    assert "is not in the evidence text" in reason(rejects, w)


def test_the_full_year_eps_that_the_sentence_draft_had_shadowed_now_comes_from_its_own_table() -> None:
    """Target 2024-05-22: the sentence draft (wrong excerpt) used to win the dedupe over the table block that holds 8.60 to 9.60."""
    sentence, block = "c-0000027419-000002741924000126-2918", "b-0000027419-000002741924000126-15105"
    assert not any(d["assumption"]["metric"] == "EPS GAAP" and d["assumption"]["target_period"] == "FY2024"
                   for d in [d for d in PILOT["drafts"] if d["custom_id"] == block])  # the block's answer was dropped
    company = COMPANIES["TGT"]
    index = {c: (company, {k: v for k, v in CANDS[c].items() if k not in ("ticker", "custom_id")}) for c in (sentence, block)}
    results = {c: llm.Result(c, "succeeded", PILOT["raw"][c], 1, 1, None, "b") for c in (sentence, block)}
    drafts, _, _ = structure.derive_drafts(index, results, METRICS, KINDS)
    eps = [d for d in drafts[company.cik] if d["assumption"]["metric"] == "EPS GAAP" and d["assumption"]["target_period"] == "FY2024"]
    assert len(eps) == 1 and eps[0]["capture_method"] == "section"
    assert (eps[0]["assumption"]["target_low"], eps[0]["assumption"]["target_high"]) == (8.6, 9.6)
    assert eps[0]["assumption"]["evidence"]["excerpt"] == "$1.95 - $2.35 $8.60 - $9.60"  # the figures are in their own excerpt


# --- fix 3: the table header row -------------------------------------------------


def lines_above(cid: str) -> list[str]:
    return PILOT["lines_above"][cid]


def header_for(cid: str) -> str | None:
    above = lines_above(cid)
    return candidates.find_table_header(above, len(above), SETTINGS)  # the block starts right after these lines


def section_cid(ticker: str, filed_at: str) -> str:
    return next(c["custom_id"] for c in PILOT["candidates"] if c["ticker"] == ticker and c["filed_at"] == filed_at and c["capture_method"] == "section")


def test_targets_header_rows_are_found_above_the_real_blocks() -> None:
    assert header_for(section_cid("TGT", "2023-05-17")) == "Q2 2023 Full Year 2023"  # five lines above the block
    assert header_for(section_cid("TGT", "2024-05-22")) == "Q2 2024 Full Year 2024"  # three lines above, and not "May 4, 2024 April 29, 2023 Change"


@pytest.mark.parametrize("line, labels", [
    ("Q2 2023 Full Year 2023", 2), ("Q1 FY26 Q2 FY26 Q3 FY26", 3), ("FY2025 FY2026", 2), ("Second Quarter 2023 Full Year 2023", 2),
    ("Q4 FY21", 1), ("Full Year 2026", 1), ("Q1 FY22 Guidance", 1),
    ("May 4, 2024 April 29, 2023 Change", 0), ("$1.30 - $1.70 $7.75 - $8.75", 0), ("For the second quarter, the Company expects", 0),
])
def test_a_period_label_is_a_quarter_or_a_full_year_with_its_year(line, labels) -> None:
    assert candidates.period_labels(line) == labels


def test_the_search_reaches_ten_lines_up_and_no_further() -> None:
    header = "Q3 FY27 Full Year FY27"
    lines = [header] + [f"filler {i}" for i in range(1, 11)]
    assert candidates.find_table_header(lines, 10, SETTINGS) == header  # ten lines above the block
    assert candidates.find_table_header(lines, 11, SETTINGS) is None  # eleven


def test_the_nearest_header_wins_and_a_lone_period_is_not_a_header() -> None:
    lines = ["Q1 2020 Full Year 2020", "x", "Q3 2021 Full Year 2021", "GAAP diluted earnings per share guidance"]
    assert candidates.find_table_header(lines, 4, SETTINGS) == "Q3 2021 Full Year 2021"
    assert candidates.find_table_header(["Full Year FY23", "Guidance"], 2, SETTINGS) is None


def test_a_sentence_that_names_two_periods_is_not_a_header() -> None:
    crm = section_cid("CRM", "2026-05-27")
    assert any(candidates.period_labels(line) >= 2 for line in lines_above(crm))  # the lead-in names two periods...
    assert header_for(crm) is None  # ...and is too long, and ends in a colon, to be a header row
    assert candidates.find_table_header(["for the second quarter fiscal 2027 and full-year fiscal 2027:"], 1, SETTINGS) is None


def test_a_header_split_over_several_lines_is_gathered_now_and_was_not_by_the_one_line_rule() -> None:
    """Salesforce 2020-12-01 has Q4 FY21, Full Year FY21, Q1 FY22 and Full Year FY22 on four separate lines, and Target 2026-03-03
    has Q1 2026 and Full Year 2026 on two: no line names two periods. The one-line rule found nothing for either (the round-2
    version of this test asserted None); gathering the cells finds both. The gathering has its own tests in test_pilot_round3.py."""
    crm2020, tgt2026 = section_cid("CRM", "2020-12-01"), section_cid("TGT", "2026-03-03")
    short = lambda cid: [l for l in lines_above(cid) if len(l.split()) <= SETTINGS.table_header_max_words and candidates.period_labels(l)]
    assert short(crm2020) == ["Q4 FY21", "Full Year FY21", "Q1 FY22 Guidance", "Full Year FY22"]  # one label per line, none with two
    assert short(tgt2026) == ["Q1 2026", "Full Year 2026"]
    assert header_for(crm2020) == "Q4 FY21 | Full Year FY21 | Q1 FY22 Guidance | Full Year FY22"
    assert header_for(tgt2026) == "Q1 2026 | Full Year 2026"


META = {"cik": "0000000123", "accession": "0000000123-24-000001", "filing_type": "8-K", "filed_at": "2024-02-01"}
TARGET_LIKE = (b"<html><body><p>Reconciliation of Non-GAAP</p><p>Adjusted EPS Guidance Guidance</p><p>Q2 2023 Full Year 2023</p>"
               b"<p>(unaudited)</p><p>Per Share</p><p>GAAP diluted earnings per share guidance</p><p>$1.30 - $1.70 $7.75 - $8.75</p>"
               b"<p>Estimated adjustments</p></body></html>")


def test_a_section_candidate_carries_its_header_and_a_sentence_candidate_does_not() -> None:
    rows, _ = candidates.candidates_for_document(META, TARGET_LIKE + b"<p>Revenue is expected to be $65.0 billion.</p>", SETTINGS)
    (section,) = [r for r in rows if r["capture_method"] == "section"]
    assert section["table_header"] == "Q2 2023 Full Year 2023" and section["heading"] == "GAAP diluted earnings per share guidance"
    assert section["sentence"].split("\n")[0] == "$1.30 - $1.70 $7.75 - $8.75"  # the block itself is unchanged: the header is not in it
    assert all(r["table_header"] is None for r in rows if r["capture_method"] == "sentence")


def test_the_header_is_not_part_of_the_block_so_offsets_and_request_ids_stay_where_they_were() -> None:
    """The block still starts at its own first line, so text[char_start:char_end] == sentence holds, and the request id
    (which ends in char_start) is the same with or without a header. That is what keeps a re-run the same pilot."""
    (section,) = [r for r in candidates.candidates_for_document(META, TARGET_LIKE, SETTINGS)[0] if r["capture_method"] == "section"]
    assert html_to_text(TARGET_LIKE)[section["char_start"] : section["char_end"]] == section["sentence"]
    assert section["table_header"] and structure.custom_id(section).endswith(f"-{section['char_start']}")
    assert structure.custom_id(section) == structure.custom_id({**section, "table_header": None})


def test_the_prompt_puts_the_header_first_and_leaves_the_numbering_alone() -> None:
    cand = {"cik": "0000000123", "accession": "0000000123-24-000001", "filing_type": "8-K", "filed_at": "2024-02-01", "char_start": 5,
            "sentence": "$1.30 - $1.70 $7.75 - $8.75\nEstimated adjustments", "capture_method": "section", "heading": "GAAP diluted earnings per share guidance",
            "lead_in": None, "context_before": [], "context_after": [], "table_header": "Q2 2023 Full Year 2023"}
    company = common.Company("Example Corp", "EXMP", "0000000123")
    with_header = structure.build_block_prompt(cand, company, None)
    assert "Section lines:\n  [header] Q2 2023 Full Year 2023\n  [1] $1.30 - $1.70 $7.75 - $8.75\n  [2] Estimated adjustments" in with_header
    without = structure.build_block_prompt({**cand, "table_header": None}, company, None)
    assert "[header]" not in without and "Section lines:\n  [1] $1.30" in without
    assert "[header]" in structure.system_prompt(METRICS, block=True) and "never cite it" in structure.system_prompt(METRICS, block=True)


def test_the_header_is_context_and_never_part_of_the_evidence_excerpt() -> None:
    cand = {**{k: v for k, v in CANDS[section_cid("TGT", "2023-05-17")].items() if k not in ("ticker", "custom_id")}, "table_header": "Q2 2023 Full Year 2023"}
    cid = section_cid("TGT", "2023-05-17")
    drafts, _ = replay(cid, candidate=cand)
    assert {d["assumption"]["evidence"]["excerpt"] for d in drafts} == {"$1.30 - $1.70 $7.75 - $8.75"}
    assert all(d["table_header"] == "Q2 2023 Full Year 2023" for d in drafts)


# --- fix 4: section against section is a conflict, not a duplicate -----------------


def stub(draft_id, method, start, low, high, metric="revenue", period="Q4 FY2026", unit="USD billions"):
    return {"draft_id": draft_id, "custom_id": draft_id, "cik": "0000000123", "capture_method": method, "char_start": start, "item_index": 0,
            "conflict": False,
            "assumption": {"metric": metric, "target_period": period, "stated_at": "2024-02-01", "target_low": low, "target_high": high, "unit": unit,
                           "text": "t", "evidence": {"accession_number": "0000000123-24-000001"}}}


def test_two_section_drafts_that_disagree_are_both_kept_both_flagged_and_neither_is_dropped() -> None:
    a, b = stub("a", "section", 10, 5.0, 6.0), stub("b", "section", 90, 7.0, 8.0)
    kept, dropped = structure.dedupe_drafts([b, a])
    assert [d["draft_id"] for d in kept] == ["a", "b"] and dropped == []
    assert all(d["conflict"] is True for d in kept)


def test_dedupe_does_not_change_the_drafts_it_is_given() -> None:
    a, b = stub("a", "section", 10, 5.0, 6.0), stub("b", "section", 90, 7.0, 8.0)
    structure.dedupe_drafts([a, b])
    assert a["conflict"] is False and b["conflict"] is False


def test_two_section_drafts_with_the_same_numbers_are_one_draft_seen_twice_and_not_a_conflict() -> None:
    kept, dropped = structure.dedupe_drafts([stub("a", "section", 10, 5.0, 6.0), stub("b", "section", 90, 5.0, 6.0)])
    assert [(d["draft_id"], d["conflict"]) for d in kept] == [("a", False)] and [e["dropped"]["draft_id"] for e in dropped] == ["b"]
    assert "the numbers differ" not in dropped[0]["reason"]


def test_of_three_section_drafts_two_that_agree_are_one_and_the_third_conflicts_with_them() -> None:
    kept, dropped = structure.dedupe_drafts([stub("a", "section", 10, 5.0, 6.0), stub("b", "section", 50, 5.0, 6.0), stub("c", "section", 90, 7.0, 8.0)])
    assert {d["draft_id"]: d["conflict"] for d in kept} == {"a": True, "c": True}
    assert [e["dropped"]["draft_id"] for e in dropped] == ["b"] and dropped[0]["kept"]["draft_id"] == "a"


def test_a_sentence_draft_still_wins_over_sections_that_disagree_with_each_other() -> None:
    kept, dropped = structure.dedupe_drafts([stub("s", "sentence", 50, 9.0, 9.0), stub("a", "section", 10, 5.0, 6.0), stub("b", "section", 90, 7.0, 8.0)])
    assert [(d["draft_id"], d["conflict"]) for d in kept] == [("s", False)] and {e["dropped"]["draft_id"] for e in dropped} == {"a", "b"}


def test_conflicts_are_only_within_one_key() -> None:
    kept, dropped = structure.dedupe_drafts([stub("a", "section", 10, 5.0, 6.0), stub("b", "section", 90, 7.0, 8.0, period="Q1 FY2027"),
                                             stub("c", "section", 95, 7.0, 8.0, metric="tax rate", unit="percent")])
    assert len(kept) == 3 and dropped == [] and not any(d["conflict"] for d in kept)


def section_candidate(start, block, heading="Q4 FY26 Guidance"):
    return {"cik": "0000000123", "accession": "0000000123-24-000001", "filing_type": "8-K", "filed_at": "2024-02-01", "char_start": start,
            "char_end": start + len(block), "sentence": block, "text_version": 1, "capture_method": "section", "heading": heading, "lead_in": None,
            "context_before": [], "context_after": [], "block_lines": len(block.split("\n")), "block_end": "gap", "table_header": None}


def test_two_sections_of_one_filing_that_disagree_reach_the_review_csv_as_two_rows_marked_true() -> None:
    company = common.Company("Example Corp", "EXMP", "0000000123")
    one, two = section_candidate(100, "Revenue\n$5.0 - $6.0 billion"), section_candidate(900, "Revenue\n$7.0 - $8.0 billion")
    item = lambda lo, hi: {"metric": "revenue", "unit": "USD billions", "target_period": "Q4 FY2026", "value_low": lo, "value_high": hi,
                           "plus_minus": None, "plus_minus_kind": "none", "line_first": 1, "line_last": 2}
    index = {"b-1": (company, one), "b-2": (company, two)}
    results = {"b-1": llm.Result("b-1", "succeeded", json.dumps({"items": [item(5.0, 6.0)]}), 1, 1, None, "b"),
               "b-2": llm.Result("b-2", "succeeded", json.dumps({"items": [item(7.0, 8.0)]}), 1, 1, None, "b")}
    drafts, rejects, dupes = structure.derive_drafts(index, results, METRICS, KINDS)
    assert len(drafts[company.cik]) == 2 and dupes[company.cik] == [] and rejects[company.cik] == []
    assert [d["conflict"] for d in drafts[company.cik]] == [True, True]
    rows = [review.build_row(d, None, date(2026, 9, 20)) for d in drafts[company.cik]]
    assert [(r["conflict"], r["approved"]) for r in rows] == [("true", "false"), ("true", "false")]  # neither chosen, neither approved
    assert {r["assumption.target_low"] for r in rows} == {"5.0", "7.0"}


# --- fix 5: quarters and fiscal years only ------------------------------------------


@pytest.mark.parametrize("period", ["H2 FY2019", "2H FY2019", "First half FY2024", "H1 2025", "Second half of FY2019", "Six months FY2026"])
def test_a_half_year_label_is_rejected_as_one(period) -> None:
    with pytest.raises(ValueError, match="is a half-year"):
        structure.check_period(period, "anything")


@pytest.mark.parametrize("period", ["FY19", "Q3 2019", "third quarter", "2019", "Q5 FY2019", "FY2019 Q3"])
def test_a_period_that_is_not_in_the_house_style_is_rejected(period) -> None:
    with pytest.raises(ValueError, match="is not like FY2027 or Q3 FY2027"):
        structure.check_period(period, "anything")


def test_a_quarter_and_a_fiscal_year_pass() -> None:
    structure.check_period("Q3 FY2019", "For the third quarter")
    structure.check_period("FY2019", "For fiscal 2019, the Company expects")


@pytest.mark.parametrize("evidence", [
    "For both the third quarter and second half of 2019, Target expects comparable sales growth",
    "The Company expects positive single-digit comparable sales growth in the last two quarters of the year",
    "Revenue in the first half is expected to be $5.0 billion",
    "Revenue for 2H is expected to be $5.0 billion",
])
def test_a_fiscal_year_drawn_from_evidence_about_a_half_is_rejected(evidence) -> None:
    with pytest.raises(ValueError, match="full year, but the evidence is about a half-year"):
        structure.check_period("FY2019", evidence)
    structure.check_period("Q3 FY2019", evidence)  # a quarter in the same words is not what is refused


def test_a_fiscal_year_is_fine_when_the_evidence_also_speaks_of_the_full_year() -> None:
    structure.check_period("FY2019", "Full-year revenue is expected to be $10 billion, with the second half stronger than the first half")


def test_the_pilots_second_half_of_2019_is_no_longer_a_fiscal_year_but_its_third_quarter_stays() -> None:
    (w,) = wrong_by(5)
    cid = w["custom_id"]
    assert {i["target_period"] for i in raw_items(cid)} == {"Q3 FY2019", "FY2019"}  # what the model did
    drafts, rejects = replay(cid)
    assert [(d["assumption"]["metric"], d["assumption"]["target_period"]) for d in drafts] == [("comparable sales", "Q3 FY2019")]
    assert "the evidence is about a half-year" in reason(rejects, w)


# --- fix 6: GAAP or non-GAAP comes from the heading, by code -------------------------


@pytest.mark.parametrize("text, bases", [
    ("GAAP diluted earnings per share guidance", {"GAAP"}),
    ("Adjusted diluted earnings per share guidance", {"non-GAAP"}),
    ("Non-GAAP operating margin", {"non-GAAP"}), ("NON-GAAP tax rate", {"non-GAAP"}), ("nonGAAP EPS", {"non-GAAP"}),
    ("GAAP and non-GAAP gross margins", {"GAAP", "non-GAAP"}), ("Reconciliation of GAAP to Adjusted EPS", {"GAAP", "non-GAAP"}),
    ("Guidance", set()), ("Q2 FY27 Guidance", set()), ("Full Year FY26 Guidance", set()),
])
def test_named_bases_reads_gaap_and_non_gaap_and_adjusted(text, bases) -> None:
    assert structure.named_bases(text) == bases


def block_candidate(heading, lines="$1.30 - $1.70 $7.75 - $8.75"):
    return section_candidate(5, lines, heading)


@pytest.mark.parametrize("model_said, heading, evidence, expected", [
    ("EPS GAAP", "Adjusted diluted earnings per share guidance", "$1.30+(a)", "EPS non-GAAP"),
    ("EPS non-GAAP", "GAAP diluted earnings per share guidance", "$1.30 - $1.70", "EPS GAAP"),
    ("EPS GAAP", "Non-GAAP guidance", "Diluted net income per share $1.74 - $1.76", "EPS non-GAAP"),
    ("gross margin non-GAAP", "GAAP gross margin guidance", "62.3%, plus or minus 50 bps", "gross margin GAAP"),
    ("operating margin GAAP", "Non-GAAP operating margin guidance", "~34.0%", "operating margin non-GAAP"),
    ("operating expenses GAAP", "Non-GAAP outlook", "$915 million", "operating expenses non-GAAP"),
])
def test_the_heading_decides_the_basis_over_the_model(model_said, heading, evidence, expected) -> None:
    assert structure.with_heading_basis(model_said, block_candidate(heading), evidence, METRICS) == expected


@pytest.mark.parametrize("model_said, heading, evidence", [
    ("EPS GAAP", "Guidance", "$1.30 - $1.70"),  # the heading names neither
    ("EPS GAAP", "GAAP and non-GAAP guidance", "$1.30 - $1.70"),  # ...or both
    ("EPS GAAP", "Adjusted diluted earnings per share guidance", "GAAP earnings per share $1.30"),  # the lines say GAAP themselves
    ("EPS non-GAAP", "GAAP diluted earnings per share guidance", "Non-GAAP diluted earnings per share $3.25"),
    ("EPS GAAP", "Adjusted EPS guidance", "GAAP and non-GAAP EPS $1.30 and $3.25"),  # the lines name both
    ("revenue", "Adjusted diluted earnings per share guidance", "$11.27 - $11.35 billion"),  # no GAAP entry to choose between
    ("tax rate", "Non-GAAP guidance", "17.0%"),
    ("operating income", "GAAP guidance", "$1,535"),
])
def test_the_heading_is_not_used_when_it_is_silent_or_double_or_the_lines_say_otherwise(model_said, heading, evidence) -> None:
    assert structure.with_heading_basis(model_said, block_candidate(heading), evidence, METRICS) == model_said


def test_a_sentence_is_never_given_a_basis_from_a_heading() -> None:
    cand = {**block_candidate("Adjusted diluted earnings per share guidance"), "capture_method": "sentence"}
    assert structure.with_heading_basis("EPS GAAP", cand, "GAAP EPS of $8.00 to $10.00", METRICS) == "EPS GAAP"


@pytest.mark.parametrize("date_, wrong_metric, right_metric, heading", [
    ("2023-05-17", "EPS non-GAAP", "EPS GAAP", "GAAP diluted earnings per share guidance"),
    ("2024-05-22", "EPS non-GAAP", "EPS GAAP", "GAAP diluted earnings per share guidance"),
    ("2026-03-03", "EPS GAAP", "EPS non-GAAP", "Adjusted diluted earnings per share guidance"),
])
def test_on_the_pilots_target_tables_a_model_that_gets_the_basis_wrong_is_overruled(date_, wrong_metric, right_metric, heading) -> None:
    """The pilot's model happened to get these three right. Its answer is flipped here to show that code, not the model,
    decides: the heading over these bare value lines is all that says which basis they are."""
    cid = section_cid("TGT", date_)
    assert CANDS[cid]["heading"] == heading
    flipped = {"items": [{**i, "metric": wrong_metric} for i in raw_items(cid)]}
    assert flipped["items"], "the pilot answered this request"
    drafts, _ = replay(cid, flipped)
    assert drafts and {d["assumption"]["metric"] for d in drafts} == {right_metric}


# --- fix 7: past tense ---------------------------------------------------------------


@pytest.mark.parametrize("evidence", [
    "For fiscal 2024 and 2023, the Company used a projected non-GAAP tax rate of 23.5% and 22.0%, respectively.",
    "For fiscal 2025 and 2026, the Company used a projected non-GAAP tax rate of 22.0%.",
    "In fiscal 2019, NVIDIA returned $1.95 billion to shareholders through share repurchases and dividends.",
    "For fiscal 2019, revenue was $11.72 billion, up 21 percent from $9.71 billion a year earlier.",
    "Second-quarter revenue was $89.0 billion, up 18% from the previous quarter.",
    "Full-year net sales decreased 1.7 percent to $104.8 billion from $106.6 billion last year.",
    "Comparable sales declined 3.7 percent in the first quarter.",
    "Revenue was $3.0 billion, above the high end of our guidance.",
])
def test_evidence_that_reports_a_result_and_looks_ahead_at_nothing_is_rejected(evidence) -> None:
    with pytest.raises(ValueError, match="past tense"):
        structure.check_not_past_tense(evidence)


@pytest.mark.parametrize("evidence", [
    "Target expects comparable sales growth in line with the 3.4 percent comparable sales growth the company delivered in the second quarter of 2019.",
    "For fiscal 2025, the Company uses a projected non-GAAP tax rate of 22.0%, which reflects currently available information.",
    "For fiscal 2025, the Company now expects a low-single digit decline in sales, and GAAP EPS of $8.00 to $10.00.",
    "Fourth quarter revenue expected to be $2.20 billion versus previous guidance of $2.70 billion",
    "GAAP and non-GAAP other income and expense are both expected to be income of approximately $25 million.",
    "(1) The Company's GAAP tax provision is expected to be approximately 57% for the three months ended July 30, 2022",
    "Revenue was $3.0 billion. The Company expects fourth quarter revenue of $3.2 billion.",
    "GAAP operating margin(1)\n~3.8%",
    "$1.30 - $1.70 $7.75 - $8.75",
    "Non-GAAP operating expenses $ 3,400",
])
def test_guidance_is_not_rejected_for_mentioning_a_past_figure_or_for_having_no_verb(evidence) -> None:
    structure.check_not_past_tense(evidence)


@pytest.mark.parametrize("w", wrong_by(7), ids=row_id)
def test_the_pilots_used_a_projected_rate_sentence_is_rejected_as_past_tense(w) -> None:
    drafts, rejects = replay(w["custom_id"])
    assert not produced(drafts, w)
    assert "past tense" in reason(rejects, w) or "is not in the evidence text" in reason(rejects, w)  # the FY2027 row has two faults; the first check to fail speaks
    if w["target_period"] == "FY2025":  # 22.0 is printed here, but for FY2023: only the tense check can tell
        assert "past tense ('used')" in reason(rejects, w) and 22.0 in structure.numbers_in(CANDS[w["custom_id"]]["sentence"])


def test_the_prompt_tells_the_model_that_past_tense_evidence_is_not_guidance() -> None:
    assert "used a tax rate of 23.5% and 22.0%" in structure.system_prompt(METRICS, block=False)


# --- all seven together, on the whole pilot ------------------------------------------


def replay_all():
    index, results = {}, {}
    for c in PILOT["candidates"]:
        cand = {k: v for k, v in c.items() if k not in ("ticker", "custom_id")}
        index[c["custom_id"]] = (COMPANIES[c["ticker"]], cand)
        results[c["custom_id"]] = llm.Result(c["custom_id"], "succeeded", PILOT["raw"][c["custom_id"]], 1, 1, None, "b")
    return structure.derive_drafts(index, results, METRICS, KINDS)


def key(d):
    a = d["assumption"]
    return d["custom_id"], a["metric"], a["target_period"]


def test_no_row_the_audit_found_right_is_lost_to_the_new_guards() -> None:
    drafts, _, _ = replay_all()
    now = {key(d) for v in drafts.values() for d in v}
    wrong = {(w["custom_id"], w["metric"], w["target_period"]) for w in WRONG}
    before = {key(d) for d in PILOT["drafts"]}
    assert (before - wrong) <= now
    # ...and the one row that appears is Target's full-year GAAP EPS, from its own table, no longer shadowed.
    assert now - before == {("b-0000027419-000002741924000126-15105", "EPS GAAP", "FY2024")}


def test_eleven_of_the_eighteen_wrong_pilot_rows_are_now_rejected_and_seven_are_not() -> None:
    drafts, rejects, _ = replay_all()
    now = {key(d) for v in drafts.values() for d in v}
    still = {(CANDS[w["custom_id"]]["ticker"], w["metric"], w["target_period"]) for w in WRONG if (w["custom_id"], w["metric"], w["target_period"]) in now}
    assert len(WRONG) - len(still) == 11
    assert still == {
        ("TGT", "comparable sales", "FY2024"),  # no guard: its numbers are in its excerpt and the excerpt says nothing that contradicts FY2024
        ("CRM", "revenue", "Q1 FY2022"), ("CRM", "revenue", "FY2022"),  # Salesforce 2020-12-01: header split over four lines, see the header test
        ("CRM", "EPS GAAP", "Q1 FY2022"), ("CRM", "EPS GAAP", "FY2022"), ("CRM", "EPS non-GAAP", "Q1 FY2022"), ("CRM", "EPS non-GAAP", "FY2022"),
    }
    # 15 rejects: the 2 that already were ("no number stated"), the 11 wrong rows, and 2 more operating-margin items
    # (Salesforce's non-GAAP columns, filed as operating income) that used to vanish silently as dedupe duplicates.
    assert sum(len(v) for v in rejects.values()) == 15


def test_every_row_that_survives_has_a_unit_that_fits_and_numbers_in_its_excerpt_and_no_half_year() -> None:
    drafts, _, _ = replay_all()
    for v in drafts.values():
        for d in v:
            a = d["assumption"]
            structure.check_unit_kind(a["metric"], a["unit"], KINDS)
            structure.check_not_past_tense(a["evidence"]["excerpt"])
            structure.check_period(a["target_period"], a["evidence"]["excerpt"])
            excerpt, low, high = a["evidence"]["excerpt"], a["target_low"], a["target_high"]
            if not structure._PLUS_MINUS.search(excerpt):  # with plus or minus the range is worked out, so its ends are not printed
                for value in (low, high):
                    assert value is None or round(abs(value), 6) in structure.numbers_in(excerpt), d["draft_id"]
