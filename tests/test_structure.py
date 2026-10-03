"""03_structure.py, against a fake client. Nothing here reaches the network or the real data/ directory.

The four fixes are tested against the NVIDIA pilot, frozen in fixtures/pilot_nvidia.json: the 30
candidates that were sent, the model's raw answer to each, and the drafts 03 derived from them.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import yaml

import fakes
from pipeline import llm
from research_record.schema import Evidence

structure = importlib.import_module("pipeline.03_structure")
common = importlib.import_module("pipeline.common")

COMPANY = common.Company("Example Corp", "EXMP", "0000000123")
NVIDIA = common.Company("NVIDIA Corporation", "NVDA", "0001045810")
ACC = "0000000123-24-000001"
FILE_URL = "https://www.sec.gov/Archives/edgar/data/123/000000012324000001/release.htm"
METRICS = common.load_config()["metrics"]
METRIC_KINDS = common.load_config()["metric_kinds"]

PILOT = json.loads((Path(__file__).parent / "fixtures" / "pilot_nvidia.json").read_text(encoding="utf-8"))
PILOT_DRAFTS = PILOT["drafts"]
PILOT_BLOCK = PILOT["block_2019"]


def pilot_cid(c: dict) -> str:
    return f"c-{c['cik']}-{c['accession'].replace('-', '')}-{c['char_start']}"


def item(metric="revenue", unit="USD billions", period="Q4 FY2026", low=65.0, high=65.0, pm=None, kind="none", lines=None):
    out = {"metric": metric, "unit": unit, "target_period": period, "value_low": low, "value_high": high,
           "plus_minus": pm, "plus_minus_kind": kind}
    if lines:
        out["line_first"], out["line_last"] = lines
    return out


def candidate(char_start, sentence, *, method="sentence", heading=None, lead_in=None, before=(), after=(),
              form="8-K", accession=ACC, filed_at="2024-02-01"):
    return {"cik": COMPANY.cik, "accession": accession, "filing_type": form, "filed_at": filed_at,
            "char_start": char_start, "char_end": char_start + len(sentence), "sentence": sentence, "text_version": 1,
            "capture_method": method, "heading": heading, "lead_in": lead_in,
            "context_before": list(before), "context_after": list(after),
            "block_lines": len(sentence.split("\n")) if method == "section" else None,
            "block_end": "gap" if method == "section" else None}


def result(cid: str, *items) -> llm.Result:
    return llm.Result(cid, "succeeded", json.dumps({"items": list(items)}), 100, 20, None, "b1")


@pytest.fixture
def world(tmp_path, monkeypatch):
    raw, cands = tmp_path / "raw", tmp_path / "candidates"
    (raw / COMPANY.cik).mkdir(parents=True)
    cands.mkdir()
    html = (b"<html><body><p>Example Corp Announces Financial Results for Third Quarter Fiscal 2026</p>"
            b"<p>Revenue is expected to be $65.0 billion.</p></body></html>")
    (raw / COMPANY.cik / f"{ACC}.html").write_bytes(html)
    meta = {"cik": COMPANY.cik, "accession": ACC, "filing_type": "8-K", "doc_type": "EX-99.1", "filed_at": "2024-02-01",
            "final_url": FILE_URL, "http_status": 200, "fetched_at": "2026-09-20T12:00:00+00:00",
            "content_sha256": hashlib.sha256(html).hexdigest()}
    (raw / COMPANY.cik / f"{ACC}.meta.json").write_text(json.dumps(meta))
    monkeypatch.setattr(common, "RAW_DIR", raw)
    monkeypatch.setattr(structure, "CANDIDATES_DIR", cands)
    monkeypatch.setattr(structure, "DRAFTS_DIR", tmp_path / "drafts")
    monkeypatch.setattr(llm, "BATCH_DIR", tmp_path / "batches")
    return tmp_path


def write_candidates(world, rows):
    (world / "candidates" / f"{COMPANY.cik}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


# --- turning stated numbers into a range -----------------------------------


@pytest.mark.parametrize(
    "it, expected",
    [
        (item(low=108.0, high=108.0, pm=2.0, kind="percent"), (105.84, 110.16)),  # NVIDIA: plus or minus 2%
        (item(low=74.0, high=74.0, pm=0.5, kind="absolute"), (73.5, 74.5)),  # 50 basis points on a percent
        (item(low=5000, high=6000), (5000, 6000)),
        (item(low=5000, high=None), (5000, None)),  # "at least"
        (item(low=None, high=6000), (None, 6000)),  # "no more than"
        (item(low=2.5, high=2.5), (2.5, 2.5)),  # point guidance
        (item(low=-0.5, high=-0.5, pm=10.0, kind="percent"), (-0.55, -0.45)),  # a loss: spread uses the magnitude
    ],
)
def test_resolve_range_does_the_arithmetic_in_code(it, expected) -> None:
    assert structure.resolve_range(it) == pytest.approx(expected)


@pytest.mark.parametrize(
    "it",
    [item(low=None, high=None), item(low=6000, high=5000), item(low=100, high=110, pm=2.0, kind="percent"), item(low=100, high=100, pm=None, kind="percent")],
)
def test_resolve_range_refuses_what_it_cannot_work_out(it) -> None:
    with pytest.raises(ValueError):
        structure.resolve_range(it)


# --- requests: the two kinds -----------------------------------------------


def test_custom_ids_say_which_kind_of_request_they_are_and_fit_the_batch_limit() -> None:
    sentence = structure.custom_id(candidate(1234567, "x"))
    block = structure.custom_id(candidate(1234567, "x\ny", method="section", heading="Outlook"))
    assert sentence == "c-0000000123-000000012324000001-1234567" and block == "b-0000000123-000000012324000001-1234567"
    assert len(sentence) <= 64


def test_only_8k_candidates_are_sent_by_default_and_10k_10q_never(world) -> None:
    write_candidates(world, [
        candidate(10, "Revenue is expected to be $65.0 billion."),
        candidate(200, "We expect capex of $5 billion.", form="10-K", accession="0000000123-24-000009"),
        candidate(300, "We expect capex of $2 billion.", form="10-Q", accession="0000000123-24-000010"),
    ])
    requests, index, _ = structure.build_requests([COMPANY], {"8-K"})
    assert [index[r.custom_id][1]["filing_type"] for r in requests] == ["8-K"]


def test_a_sentence_too_long_for_an_excerpt_is_skipped_and_logged_but_a_long_block_is_not(world) -> None:
    write_candidates(world, [candidate(10, "x" * 401), candidate(500, "y" * 400),
                             candidate(900, "\n".join(["Revenue $5 billion"] * 40), method="section", heading="Outlook")])
    requests, _, skipped = structure.build_requests([COMPANY], {"8-K"})
    assert len(requests) == 2 and "401 chars" in skipped[COMPANY.cik][0]["reason"]
    assert sum(r.custom_id.startswith("b-") for r in requests) == 1


def test_an_identical_prompt_in_the_same_filing_is_sent_once(world) -> None:
    same = candidate(10, "Revenue growth 11%", heading="Q4 Guidance")
    write_candidates(world, [same, {**same, "char_start": 900, "char_end": 918}])
    requests, _, skipped = structure.build_requests([COMPANY], {"8-K"})
    assert len(requests) == 1 and "identical prompt" in skipped[COMPANY.cik][0]["reason"]


def test_the_same_words_under_a_different_heading_are_not_duplicates(world) -> None:
    write_candidates(world, [candidate(10, "11% - 12%", heading="Q4 FY26 Guidance"), candidate(900, "11% - 12%", heading="Full Year FY26 Guidance")])
    assert len(structure.build_requests([COMPANY], {"8-K"})[0]) == 2


def test_limit_caps_the_number_of_requests(world) -> None:
    write_candidates(world, [candidate(10 * i, f"Revenue is expected to be ${i} billion.") for i in range(1, 6)])
    requests, index, _ = structure.build_requests([COMPANY], {"8-K"}, limit=2)
    assert len(requests) == 2 and set(index) == {r.custom_id for r in requests}


def test_per_company_takes_half_sections_and_spreads_evenly_and_is_deterministic(world) -> None:
    rows = [candidate(10 * i, f"Revenue is expected to be ${i} billion.") for i in range(1, 21)]
    rows += [candidate(1000 + 10 * i, f"Revenue\n${i} billion", method="section", heading=f"Q{i} Guidance") for i in range(1, 9)]
    write_candidates(world, rows)
    requests, index, _ = structure.build_requests([COMPANY], {"8-K"}, per_company=10)
    kinds = [r.custom_id[0] for r in requests]
    assert len(requests) == 10 and kinds.count("b") == 5 and kinds.count("c") == 5
    starts = [index[r.custom_id][1]["char_start"] for r in requests if r.custom_id[0] == "c"]
    assert starts == sorted(starts) and starts[0] < 60 and starts[-1] > 150  # spread across the list, not the first five
    assert requests == structure.build_requests([COMPANY], {"8-K"}, per_company=10)[0]


def test_per_company_fills_from_the_other_kind_when_one_runs_short(world) -> None:
    write_candidates(world, [candidate(10 * i, f"Revenue is expected to be ${i} billion.") for i in range(1, 9)]
                     + [candidate(500, "Revenue\n$5 billion", method="section", heading="Outlook")])
    requests, _, _ = structure.build_requests([COMPANY], {"8-K"}, per_company=6)
    assert [r.custom_id[0] for r in requests].count("b") == 1 and len(requests) == 6


def test_a_sentence_prompt_shows_everything_the_model_needs(world) -> None:
    c = candidate(50, "GAAP operating expenses are expected to be $6.7 billion.", heading="Outlook",
                  lead_in="Our outlook for the fourth quarter of fiscal 2026 is as follows:", before=["Revenue is expected to be $65.0 billion."])
    text = structure.build_prompt(c, COMPANY, "Example Corp Announces Financial Results for Third Quarter Fiscal 2026")
    for needle in ["Example Corp (EXMP)", "filed 2024-02-01", "Third Quarter Fiscal 2026", "Section heading: Outlook",
                   "Lead-in line: Our outlook for the fourth quarter", "  Revenue is expected to be $65.0 billion.",
                   "Line to assess: GAAP operating expenses", "Lines after: (none)"]:
        assert needle in text
    bare = structure.build_prompt(candidate(1, "x"), COMPANY, None)
    assert "Section heading: (none)" in bare and "Lead-in line: (none)" in bare and "Title: (not found)" in bare


def test_the_title_is_the_first_line_that_reads_like_a_headline() -> None:
    text = "EX-99\n2\nfile.htm\nExhibit 99\nNVIDIA Announces Financial Results for Third Quarter Fiscal 2026\nContact: press"
    assert structure.document_title(text) == "NVIDIA Announces Financial Results for Third Quarter Fiscal 2026"
    assert structure.document_title("Nothing here\nreads like one") is None


# --- fix 1: a section is sent whole, never as a bare value line ------------


def test_the_pilots_problem_table_is_one_request_with_every_line_numbered_and_its_labels() -> None:
    text = structure.build_block_prompt(PILOT_BLOCK, NVIDIA, "NVIDIA Announces Preliminary Fourth Quarter Results")
    assert "Section heading: Updated Q4 Fiscal 2019 Guidance" in text
    assert "Previous Q4 Fiscal 2019 Guidance" in text  # the neighbouring heading is in the context
    for n, line in enumerate(PILOT_BLOCK["sentence"].split("\n"), 1):
        assert f"  [{n}] {line}" in text
    assert "[1] Revenue" in text and "[4] Gross margin - GAAP" in text and "[12] $915 million" in text and "[21]" in text


def test_a_block_becomes_one_request_of_its_own_kind_with_the_block_prompt_and_schema(world) -> None:
    write_candidates(world, [candidate(10, "Revenue is expected to be $65.0 billion."),
                             candidate(200, "Revenue\n$5.0 billion to $6.0 billion", method="section", heading="Q4 FY26 Guidance")])
    requests, _, _ = structure.build_requests([COMPANY], {"8-K"})
    sentence, block = sorted(requests, key=lambda r: r.custom_id)[1], sorted(requests, key=lambda r: r.custom_id)[0]
    assert block.custom_id.startswith("b-") and "Section lines:" in block.user and "[1] Revenue" in block.user
    assert block.system == structure.system_prompt(METRICS, block=True) and block.max_tokens == structure.MAX_TOKENS_BLOCK
    assert "line_first" in block.schema["properties"]["items"]["items"]["properties"]
    assert sentence.system == structure.system_prompt(METRICS, block=False)
    assert "line_first" not in sentence.schema["properties"]["items"]["items"]["properties"]


def test_no_request_is_ever_a_lone_value_line(world) -> None:
    write_candidates(world, [candidate(200, "Revenue\n$915 million\n$755 million", method="section", heading="Guidance")])
    (request,), _, _ = structure.build_requests([COMPANY], {"8-K"})
    assert "Section heading: Guidance" in request.user and "[1] Revenue" in request.user  # value lines arrive with their label


def test_the_evidence_of_a_block_item_is_the_lines_it_points_at_verbatim(world) -> None:
    block = candidate(10, "Revenue\n$5.0 billion to $6.0 billion\nGross margin\n74% to 75%", method="section", heading="Outlook")
    (cid,) = [structure.custom_id(block)]
    write_candidates(world, [block])
    _, index, _ = structure.build_requests([COMPANY], {"8-K"})
    res = {cid: result(cid, item("revenue", low=5.0, high=6.0, lines=(1, 2)), item("gross margin GAAP", "percent", low=74, high=75, lines=(3, 4)))}
    (drafts, rejects, _) = structure.derive_drafts(index, res)
    first, second = drafts[COMPANY.cik]
    assert first["assumption"]["evidence"]["excerpt"] == "Revenue\n$5.0 billion to $6.0 billion" == first["assumption"]["text"]
    assert second["assumption"]["evidence"]["excerpt"] == "Gross margin\n74% to 75%"
    assert first["evidence_lines"] == [1, 2] and first["capture_method"] == "section" and rejects[COMPANY.cik] == []


@pytest.mark.parametrize("lines", [(0, 1), (3, 2), (1, 9), (None, None), ("1", "2")])
def test_a_block_item_that_points_at_lines_that_are_not_there_is_rejected(world, lines) -> None:
    block = candidate(10, "Revenue\n$5.0 billion to $6.0 billion", method="section", heading="Outlook")
    write_candidates(world, [block])
    _, index, _ = structure.build_requests([COMPANY], {"8-K"})
    cid = structure.custom_id(block)
    (drafts, rejects, _) = structure.derive_drafts(index, {cid: result(cid, item(low=5.0, high=6.0, lines=lines))})
    assert drafts[COMPANY.cik] == [] and "line" in rejects[COMPANY.cik][0]["reason"]


def test_evidence_that_is_too_long_falls_back_to_the_value_line_or_is_rejected(world) -> None:
    long_label = "L" * 390
    block = candidate(10, f"{long_label}\n$5.0 billion to $6.0 billion", method="section", heading="Outlook")
    write_candidates(world, [block])
    _, index, _ = structure.build_requests([COMPANY], {"8-K"})
    cid = structure.custom_id(block)
    (drafts, _, _) = structure.derive_drafts(index, {cid: result(cid, item(low=5.0, high=6.0, lines=(1, 2)))})
    assert drafts[COMPANY.cik][0]["assumption"]["evidence"]["excerpt"] == "$5.0 billion to $6.0 billion"  # the value line alone
    huge = candidate(10, "V" * 401 + " to 5\nx", method="section", heading="Outlook")
    write_candidates(world, [huge])
    _, index, _ = structure.build_requests([COMPANY], {"8-K"})
    cid = structure.custom_id(huge)
    (drafts, rejects, _) = structure.derive_drafts(index, {cid: result(cid, item(low=5.0, high=6.0, lines=(1, 1)))})
    assert drafts[COMPANY.cik] == [] and "excerpt limit" in rejects[COMPANY.cik][0]["reason"]


def test_the_block_prompt_tells_the_model_to_ignore_what_follows_the_guidance() -> None:
    block = structure.system_prompt(METRICS, block=True)
    assert "ignore all of that" in block and "line_first and line_last" in block and "a figure means nothing without its label" in block
    assert "line_first" not in structure.system_prompt(METRICS, block=False)


# --- fix 2: the prompt rules, and the guard that does not rely on them -----


@pytest.mark.parametrize("block", [False, True])
def test_the_prompt_states_the_range_and_past_tense_rules(block) -> None:
    text = structure.system_prompt(METRICS, block=block)
    for rule in [
        'A range needs the word "to" or "between", a dash between two numbers in the same figure',
        "plus or minus language",
        "Two separate figures are never a range",
        'Two values joined by "and" or "respectively" are two point items',
        "Anything in the past tense, or about a period that has already finished, returns nothing",
        "If the text is not forward guidance, return an empty list",
    ]:
        assert rule in text
    assert '{"items":[]}' in text  # and the examples show empty answers for past-tense and no-number lines


def pilot_item(sentence: str) -> tuple[dict, dict]:
    """The pilot's raw answer for the candidate whose text is exactly `sentence`, and that candidate."""
    for c in PILOT["candidates"]:
        if c["sentence"] == sentence:
            return json.loads(PILOT["raw"][pilot_cid(c)])["items"][0], c
    raise LookupError(sentence)


def test_the_pilots_false_ranges_are_rejected_by_the_guard_whatever_the_model_did() -> None:
    # "$755 million" alone came back as 755 to 915: two figures from different lines fused into a range.
    for sentence in ("$755 million",):
        it, cand = pilot_item(sentence)
        assert (it["value_low"], it["value_high"]) == (755.0, 915.0)
        with pytest.raises(ValueError, match="no 'to', 'between', dash or plus or minus"):
            structure.check_range_language(it, cand["sentence"])
    both = [json.loads(PILOT["raw"][pilot_cid(c)])["items"][0] for c in PILOT["candidates"] if c["sentence"] == "$755 million"]
    assert len(both) == 2 and all((i["value_low"], i["value_high"]) == (755.0, 915.0) for i in both)


@pytest.mark.parametrize(
    "evidence, low, high, kind",
    [
        ("Revenue is expected to be $5.0 billion to $6.0 billion.", 5.0, 6.0, "none"),
        ("Revenue is expected to be between $5.0 billion and $6.0 billion.", 5.0, 6.0, "none"),
        ("$11.13 - $11.23 billion", 11.13, 11.23, "none"),
        ("~$7.00 - $8.00", 7.0, 8.0, "none"),
        ("from 10 to 12 percent", 10.0, 12.0, "none"),
        ("Full-year GAAP EPS is now expected to be approximately $7.70 to $8.70.", 7.7, 8.7, "none"),
        ("4% \u2013 5% growth", 4.0, 5.0, "none"),
        ("$65.0 billion, plus or minus 2%", 65.0, 65.0, "percent"),
        ("74.8%, plus or minus 50 bps", 74.8, 74.8, "absolute"),
        ("gross margins of 58.8 percent and 59.0 percent, respectively", 58.8, 58.8, "none"),  # a point: low == high
    ],
)
def test_range_language_that_is_there_is_accepted(evidence, low, high, kind) -> None:
    structure.check_range_language(item(low=low, high=high, pm=1.0 if kind != "none" else None, kind=kind), evidence)


@pytest.mark.parametrize(
    "evidence, low, high, kind",
    [
        ("$755 million", 755.0, 915.0, "none"),
        ("GAAP and non-GAAP operating expenses are approximately $930 million and $755 million, respectively.", 755.0, 930.0, "none"),
        ("Revenue is expected to be $65.0 billion.", 65.0, 65.0, "percent"),  # a plus or minus with no plus or minus in the words
        ("Revenue is expected to be between the two.", 5.0, 6.0, "none"),  # "between" with no figures
        ("We expect to reach 5 to be safe about 6", 5.0, 6.0, "none"),  # a "to" that is not between figures
    ],
)
def test_a_range_without_range_language_is_rejected(evidence, low, high, kind) -> None:
    with pytest.raises(ValueError):
        structure.check_range_language(item(low=low, high=high, pm=2.0 if kind != "none" else None, kind=kind), evidence)


def test_two_values_joined_by_and_or_respectively_are_two_point_drafts(world) -> None:
    s = "GAAP and non-GAAP operating expenses are expected to be approximately $6.7 billion and $5.0 billion, respectively."
    write_candidates(world, [candidate(10, s)])
    _, index, _ = structure.build_requests([COMPANY], {"8-K"})
    cid = structure.custom_id(candidate(10, s))
    two_points = result(cid, item("operating expenses GAAP", low=6.7, high=6.7), item("operating expenses non-GAAP", low=5.0, high=5.0))
    (drafts, rejects, _) = structure.derive_drafts(index, {cid: two_points})
    assert [(d["assumption"]["metric"], d["assumption"]["target_low"], d["assumption"]["target_high"]) for d in drafts[COMPANY.cik]] == [
        ("operating expenses GAAP", 6.7, 6.7), ("operating expenses non-GAAP", 5.0, 5.0)]
    fused = result(cid, item("operating expenses GAAP", low=5.0, high=6.7))  # the model fuses them into a range instead
    (drafts, rejects, _) = structure.derive_drafts(index, {cid: fused})
    assert drafts[COMPANY.cik] == [] and "range" in rejects[COMPANY.cik][0]["reason"]


# --- fix 3: only the configured metrics -------------------------------------

EXPECTED_METRICS = [
    "revenue", "gross margin GAAP", "gross margin non-GAAP", "operating expenses GAAP", "operating expenses non-GAAP",
    "operating margin GAAP", "operating margin non-GAAP",  # added after the second pilot, where margins came back as operating income
    "operating income", "EPS GAAP", "EPS non-GAAP", "tax rate", "other income and expense", "comparable sales", "free cash flow",
    "total expenses", "capital expenditures",  # added after the fourteen-company run, where Meta's came back as opex and other income
]


def test_config_holds_exactly_the_agreed_metrics() -> None:
    assert METRICS == EXPECTED_METRICS


@pytest.mark.parametrize("block", [False, True])
def test_the_model_can_only_pick_from_the_list_because_the_schema_says_so(block) -> None:
    props = structure.item_schema(METRICS, block=block)["properties"]["items"]["items"]["properties"]
    assert props["metric"] == {"type": "string", "enum": EXPECTED_METRICS}
    assert props["unit"]["enum"] == structure.UNITS


@pytest.mark.parametrize("block", [False, True])
def test_the_prompt_lists_every_metric_and_excludes_capital_return(block) -> None:
    text = structure.system_prompt(METRICS, block=block)
    assert all(f'"{m}"' in text for m in EXPECTED_METRICS)
    assert "capital return, share repurchases and dividends" in text and "gross profit" in text


def test_the_pilots_wrong_metrics_are_all_outside_the_list_and_none_can_come_back(monkeypatch) -> None:
    pilot_metrics = {d["assumption"]["metric"] for d in PILOT_DRAFTS}
    wrong = {"gross profit", "capital return to shareholders", "capital return", "share repurchases and dividends"}
    assert wrong <= pilot_metrics and not wrong & set(EXPECTED_METRICS)  # the pilot did produce them

    # Replay the pilot's raw answers, with the bare line as evidence as it was then.
    monkeypatch.setattr(structure, "evidence_for", lambda company, cand, excerpt: Evidence(
        source_url=FILE_URL, accession_number=cand["accession"], filing_type="8-K", filed_at=date.fromisoformat(cand["filed_at"]),
        fetched_at=datetime(2026, 9, 20, tzinfo=timezone.utc), content_sha256="a" * 64, excerpt=excerpt))
    index = {pilot_cid(c): (NVIDIA, {**c, "capture_method": "sentence"}) for c in PILOT["candidates"]}
    results = {cid: llm.Result(cid, "succeeded", text, 1, 1, None, "b") for cid, text in PILOT["raw"].items()}
    drafts, rejects, _ = structure.derive_drafts(index, results, METRICS)
    survivors = drafts.get(NVIDIA.cik, [])
    assert all(d["assumption"]["metric"] in EXPECTED_METRICS for d in survivors)
    rejected_metrics = {r["item"]["metric"] for r in rejects[NVIDIA.cik] if "metrics list" in r["reason"]}
    assert wrong <= rejected_metrics
    # ...and nothing that survives is a range without range language.
    for d in survivors:
        a = d["assumption"]
        if a["target_low"] != a["target_high"]:
            assert structure.has_range_language(a["evidence"]["excerpt"]) or structure._PLUS_MINUS.search(a["evidence"]["excerpt"])


def test_a_metric_outside_the_list_is_rejected_with_a_reason(world) -> None:
    s = "Gross profit is expected to be $2.20 billion."
    write_candidates(world, [candidate(10, s)])
    _, index, _ = structure.build_requests([COMPANY], {"8-K"})
    cid = structure.custom_id(candidate(10, s))
    for bad in ("gross profit", "capital return", "gaap gross margin", "Revenue"):  # near misses count: spelled exactly
        (drafts, rejects, _) = structure.derive_drafts(index, {cid: result(cid, item(bad, low=2.2, high=2.2))})
        assert drafts[COMPANY.cik] == [] and "not in the metrics list" in rejects[COMPANY.cik][0]["reason"]


# --- fix 4: one draft per metric, period and date; the sentence wins -------


def dropped_ids(dropped):
    return {entry["dropped"]["draft_id"] for entry in dropped}


def test_on_the_pilot_every_section_draft_with_a_sentence_twin_is_dropped_and_logged() -> None:
    kept, dropped = structure.dedupe_drafts(PILOT_DRAFTS)
    # 5 drops, not the 7 there used to be: two pairs of section drafts that disagree are now kept and flagged (see the conflict tests).
    assert len(PILOT_DRAFTS) == 30 and len(kept) == 25 and len(dropped) == 5
    assert all(entry["dropped"]["capture_method"] == "section" for entry in dropped)
    # Every sentence-captured draft survives.
    sentence_ids = {d["draft_id"] for d in PILOT_DRAFTS if d["capture_method"] == "sentence"}
    assert sentence_ids <= {d["draft_id"] for d in kept}


def test_the_pilots_gross_margin_pair_keeps_the_range_and_drops_the_point_inside_it() -> None:
    # The same winner as before, for a better reason: the point 58.8 lies inside the sentence's 58.3 to 59.3, so it is the same
    # statement made less precisely. (This used to be logged as "kept the sentence-captured draft ... the numbers differ".)
    _, dropped = structure.dedupe_drafts(PILOT_DRAFTS)
    entry = next(e for e in dropped if e["dropped"]["target_low"] == 58.8 and e["dropped"]["capture_method"] == "section")
    assert entry["kept"]["capture_method"] == "sentence" and (entry["kept"]["target_low"], entry["kept"]["target_high"]) == (58.3, 59.3)
    assert entry["reason"].startswith("range kept over point") and "states 58.3 to 59.3" in entry["reason"] and "(58.8) lies inside it" in entry["reason"]
    assert entry["custom_id"].startswith("c-0001045810-")


def test_between_two_section_drafts_that_disagree_neither_wins_both_are_kept_and_flagged() -> None:
    # This used to be "the earliest wins". The pilot's two 'gaap gross margin' Q4 FY2019 drafts came from different
    # columns of one table (54.0 to 56.0 and 61.8 to 62.8): nothing in the text says which is the real one, so code must not.
    gm = [d for d in PILOT_DRAFTS if d["assumption"]["metric"] == "gaap gross margin" and d["assumption"]["target_period"] == "Q4 FY2019"]
    assert len(gm) == 2 and {d["capture_method"] for d in gm} == {"section"}
    kept, dropped = structure.dedupe_drafts(gm)
    assert {d["draft_id"] for d in kept} == {d["draft_id"] for d in gm} and dropped == []
    assert all(d["conflict"] is True for d in kept)


def draft_stub(cid, method, start, low, high, metric="revenue", period="Q4 FY2026", stated="2024-02-01", cik="0000000123"):
    return {"draft_id": cid, "custom_id": cid, "cik": cik, "capture_method": method, "char_start": start, "item_index": 0,
            "assumption": {"metric": metric, "target_period": period, "stated_at": stated, "target_low": low, "target_high": high,
                           "unit": "USD billions", "text": "t", "evidence": {"accession_number": ACC}}}


def test_the_key_is_cik_metric_period_and_stated_date() -> None:
    base = draft_stub("a", "sentence", 10, 5, 5)
    others = [draft_stub("b", "section", 20, 6, 6, metric="tax rate"), draft_stub("c", "section", 20, 6, 6, period="Q1 FY2027"),
              draft_stub("d", "section", 20, 6, 6, stated="2024-03-01"), draft_stub("e", "section", 20, 6, 6, cik="0000000999")]
    kept, dropped = structure.dedupe_drafts([base] + others)
    assert len(kept) == 5 and dropped == []  # none share the whole key
    kept, dropped = structure.dedupe_drafts([base, draft_stub("f", "section", 20, 6, 6)])
    assert [d["draft_id"] for d in kept] == ["a"] and dropped_ids(dropped) == {"f"}


def test_a_section_draft_seen_first_still_loses_to_a_later_sentence_draft() -> None:
    kept, dropped = structure.dedupe_drafts([draft_stub("sec", "section", 5, 1, 1), draft_stub("sen", "sentence", 50, 2, 2)])
    assert [d["draft_id"] for d in kept] == ["sen"] and dropped_ids(dropped) == {"sec"}


def test_dedup_runs_on_what_derive_drafts_returns_and_logs_the_drop(world) -> None:
    s1 = "Revenue is expected to be $65.0 billion, plus or minus 2%."
    block = "Revenue\n$65.0 billion, plus or minus 2%"
    rows = [candidate(10, s1), candidate(300, block, method="section", heading="Outlook")]
    write_candidates(world, rows)
    _, index, _ = structure.build_requests([COMPANY], {"8-K"})
    sid, bid = structure.custom_id(rows[0]), structure.custom_id(rows[1])
    res = {sid: result(sid, item(low=65.0, high=65.0, pm=2.0, kind="percent")),
           bid: result(bid, item(low=65.0, high=65.0, pm=2.0, kind="percent", lines=(1, 2)))}
    drafts, _, dupes = structure.derive_drafts(index, res)
    assert [d["capture_method"] for d in drafts[COMPANY.cik]] == ["sentence"]
    assert len(dupes[COMPANY.cik]) == 1 and dupes[COMPANY.cik][0]["dropped"]["capture_method"] == "section"


# --- from model output to drafts -------------------------------------------


def derive(world, rows, results):
    write_candidates(world, rows)
    _, index, _ = structure.build_requests([COMPANY], {"8-K"})
    return structure.derive_drafts(index, results), index


def cid(start):
    return structure.custom_id(candidate(start, "x"))


def test_a_draft_takes_its_facts_from_the_filing_and_its_numbers_from_the_model(world) -> None:
    s = "Revenue is expected to be $108.0 billion, plus or minus 2%."
    (drafts, rejects, dupes), _ = derive(world, [candidate(10, s)], {cid(10): result(cid(10), item(low=108.0, high=108.0, pm=2.0, kind="percent"))})
    (draft,) = drafts[COMPANY.cik]
    a = draft["assumption"]
    assert (a["target_low"], a["target_high"], a["unit"], a["target_period"], a["metric"]) == (105.84, 110.16, "USD billions", "Q4 FY2026", "revenue")
    assert a["text"] == s and a["evidence"]["excerpt"] == s  # verbatim, not model text
    assert a["stated_at"] == "2024-02-01"  # the filing date
    assert a["evidence"]["source_url"] == FILE_URL and a["evidence"]["accession_number"] == ACC
    assert re.fullmatch(r"[0-7][0-9A-HJKMNP-TV-Z]{25}", draft["draft_id"])
    assert rejects[COMPANY.cik] == [] and dupes[COMPANY.cik] == []


def test_one_sentence_can_yield_several_drafts(world) -> None:
    both = result(cid(10), item("gross margin GAAP", "percent", low=74.8, high=74.8), item("gross margin non-GAAP", "percent", low=75.0, high=75.0))
    (drafts, _, _), _ = derive(world, [candidate(10, "GAAP and non-GAAP gross margins are expected to be 74.8% and 75.0%.")], {cid(10): both})
    assert [d["assumption"]["metric"] for d in drafts[COMPANY.cik]] == ["gross margin GAAP", "gross margin non-GAAP"]
    assert len({d["draft_id"] for d in drafts[COMPANY.cik]}) == 2


def test_an_empty_answer_is_simply_not_guidance(world) -> None:
    (drafts, rejects, _), _ = derive(world, [candidate(10, "Revenue was $89.0 billion.")], {cid(10): result(cid(10))})
    assert drafts[COMPANY.cik] == [] and rejects[COMPANY.cik] == []


@pytest.mark.parametrize(
    "bad, why",
    [
        (item(period="third quarter"), "target_period"),
        (item(period="FY27"), "target_period"),
        (item(metric=""), "metrics list"),
        (item(low=None, high=None), "no number"),
        (item(low=9.0, high=1.0), "reversed"),
    ],
)
def test_output_that_breaks_the_rules_is_rejected_with_a_reason_not_dropped(world, bad, why) -> None:
    (drafts, rejects, _), _ = derive(world, [candidate(10, "Revenue is expected to be $5 billion to $6 billion.")], {cid(10): result(cid(10), bad)})
    assert drafts[COMPANY.cik] == []
    assert why in rejects[COMPANY.cik][0]["reason"]


def test_output_that_is_not_the_expected_json_is_rejected(world) -> None:
    junk = llm.Result(cid(10), "succeeded", "I think this is guidance.", 1, 1, None, "b")
    (drafts, rejects, _), _ = derive(world, [candidate(10, "s")], {cid(10): junk})
    assert drafts[COMPANY.cik] == [] and "did not parse" in rejects[COMPANY.cik][0]["reason"]


def test_results_that_did_not_succeed_are_not_drafts_and_not_rejects(world) -> None:
    (drafts, rejects, _), _ = derive(world, [candidate(10, "s")], {cid(10): llm.Result(cid(10), "errored", None, 0, 0, "overloaded_error", "b")})
    assert drafts == {} and rejects == {}


def test_draft_ids_are_stable_across_runs(world) -> None:
    rows, res = [candidate(10, "Revenue is expected to be $65.0 billion.")], {cid(10): result(cid(10), item())}
    first, _ = derive(world, rows, res)
    second, _ = derive(world, rows, res)
    assert first[0][COMPANY.cik][0]["draft_id"] == second[0][COMPANY.cik][0]["draft_id"]


# --- main, end to end ------------------------------------------------------


@pytest.fixture
def config_path(world):
    cfg = common.load_config()
    cfg["llm"]["sync_below"] = 0  # these tests are about the batch path; the synchronous rule has its own tests in test_llm.py
    cfg["companies"] = [{"name": COMPANY.name, "ticker": COMPANY.ticker, "cik": COMPANY.cik}]
    path = world / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


def respond(params: dict) -> str:
    user = params["messages"][0]["content"]
    if "Section lines:" in user:
        return json.dumps({"items": [item("revenue", "USD billions", "Q1 FY2027", 5.0, 6.0, lines=(1, 2))] if "$5.0 billion to $6.0 billion" in user else []})
    return json.dumps({"items": [item()] if "Line to assess: Revenue is expected" in user else []})


def use_client(monkeypatch, client):
    monkeypatch.setattr(llm, "make_client", lambda: client)
    return client


def seed(world):
    write_candidates(world, [
        candidate(10, "Revenue is expected to be $65.0 billion."),
        candidate(200, "Revenue was $60.0 billion."),
        candidate(400, "Gross margin was 74%."),
    ])


def test_a_dry_run_calls_the_api_for_nothing_but_counting(world, config_path, monkeypatch, capsys) -> None:
    seed(world)
    client = use_client(monkeypatch, fakes.FakeAnthropic(respond=respond))
    assert structure.main(["--config", str(config_path)]) == 0
    assert client.messages.create_calls == [] and client.messages.batches.created == []
    assert client.messages.count_calls >= 3  # exact counts, because credentials exist
    out = capsys.readouterr().out
    assert "counted by the API" in out and "dry run: nothing was submitted" in out and "3 sentence, 0 section" in out
    assert llm.Ledger().committed_usd() == 0


def test_submit_runs_the_batch_and_writes_drafts_rejects_and_skips(world, config_path, monkeypatch) -> None:
    seed(world)
    client = use_client(monkeypatch, fakes.FakeAnthropic(respond=respond))
    assert structure.main(["--config", str(config_path), "--submit"]) == 0
    assert len(client.messages.create_calls) == 1 and len(client.messages.batches.created) == 1  # canary + one batch
    drafts = [json.loads(l) for l in (world / "drafts" / f"{COMPANY.cik}.jsonl").read_text().splitlines()]
    assert [d["assumption"]["metric"] for d in drafts] == ["revenue"]
    assert not (world / "drafts" / f"{COMPANY.cik}.rejects.jsonl").exists()  # nothing was rejected, so no file
    assert llm.Ledger().committed_usd() > 0 and llm.RawArchive("03_structure").path.exists()


def test_a_section_request_goes_through_main_and_its_draft_carries_the_lines(world, config_path, monkeypatch) -> None:
    write_candidates(world, [candidate(10, "Revenue is expected to be $65.0 billion."),
                             candidate(300, "Revenue\n$5.0 billion to $6.0 billion", method="section", heading="Q1 FY27 Guidance")])
    use_client(monkeypatch, fakes.FakeAnthropic(respond=respond))
    assert structure.main(["--config", str(config_path), "--submit"]) == 0
    drafts = [json.loads(l) for l in (world / "drafts" / f"{COMPANY.cik}.jsonl").read_text().splitlines()]
    assert sorted(d["capture_method"] for d in drafts) == ["section", "sentence"]
    block = next(d for d in drafts if d["capture_method"] == "section")
    assert block["evidence_lines"] == [1, 2] and block["heading"] == "Q1 FY27 Guidance"
    assert block["assumption"]["evidence"]["excerpt"] == "Revenue\n$5.0 billion to $6.0 billion"


def test_editing_the_prompt_resends_and_does_not_reuse_answers_to_the_old_wording(world, config_path, monkeypatch) -> None:
    seed(world)
    use_client(monkeypatch, fakes.FakeAnthropic(respond=respond))
    structure.main(["--config", str(config_path), "--submit"])
    monkeypatch.setattr(structure, "_INTRO_SENTENCE", structure._INTRO_SENTENCE + " Be careful.")
    again = use_client(monkeypatch, fakes.FakeAnthropic(respond=respond))
    assert structure.main(["--config", str(config_path), "--submit"]) == 0
    assert len(again.messages.create_calls) == 1 and len(again.messages.batches.created[0]) == 2  # all three, canary + batch of two


def test_running_submit_again_sends_nothing_and_keeps_the_drafts(world, config_path, monkeypatch) -> None:
    seed(world)
    use_client(monkeypatch, fakes.FakeAnthropic(respond=respond))
    structure.main(["--config", str(config_path), "--submit"])
    second = use_client(monkeypatch, fakes.FakeAnthropic(respond=respond))
    assert structure.main(["--config", str(config_path), "--submit"]) == 0
    assert second.messages.create_calls == [] and second.messages.batches.created == []
    assert len((world / "drafts" / f"{COMPANY.cik}.jsonl").read_text().splitlines()) == 1


def test_submit_without_credentials_stops_and_says_so(world, config_path, monkeypatch, capsys) -> None:
    seed(world)
    client = use_client(monkeypatch, fakes.FakeAnthropic(authenticated=False))
    assert structure.main(["--config", str(config_path), "--submit"]) == 3
    captured = capsys.readouterr()
    assert "ESTIMATE" in captured.out and "no API credentials" in captured.err
    assert client.messages.create_calls == [] and client.messages.batches.created == []


def test_submit_over_the_budget_stops_before_any_call(world, config_path, monkeypatch, capsys) -> None:
    seed(world)
    cfg = yaml.safe_load(config_path.read_text())
    cfg["llm"]["budget_usd"] = 0.0001
    config_path.write_text(yaml.safe_dump(cfg))
    client = use_client(monkeypatch, fakes.FakeAnthropic(respond=respond, input_tokens=5000))
    assert structure.main(["--config", str(config_path), "--submit"]) == 4
    assert "EXCEEDS THE CAP" in capsys.readouterr().out
    assert client.messages.create_calls == [] and client.messages.batches.created == []


def test_the_command_line_default_is_8k_only_and_10k_and_10q_candidates_are_never_sent(world, config_path, monkeypatch) -> None:
    write_candidates(world, [
        candidate(10, "Revenue is expected to be $65.0 billion."),
        candidate(200, "We expect capex of $5 billion.", form="10-K", accession="0000000123-24-000009"),
        candidate(300, "We expect capex of $2 billion.", form="10-Q", accession="0000000123-24-000010"),
    ])
    client = use_client(monkeypatch, fakes.FakeAnthropic(respond=respond))
    assert structure.main(["--config", str(config_path), "--submit"]) == 0
    sent = [c["messages"][0]["content"] for c in client.messages.create_calls]
    sent += [item_["params"]["messages"][0]["content"] for batch in client.messages.batches.created for item_ in batch]
    assert len(sent) == 1 and "Revenue is expected to be $65.0 billion." in sent[0]
    assert not any("capex" in text for text in sent)


def test_a_dry_run_leaves_no_empty_output_files(world, config_path, monkeypatch) -> None:
    seed(world)
    use_client(monkeypatch, fakes.FakeAnthropic(respond=respond))
    assert structure.main(["--config", str(config_path)]) == 0
    drafts = world / "drafts"
    assert not drafts.exists() or all(p.stat().st_size > 0 for p in drafts.iterdir())


# --- third pilot review: dry runs write nothing, temperature 0, empty blocks --------------------------


def drafts_dir_snapshot(world) -> dict[str, bytes]:
    folder = world / "drafts"
    return {p.name: p.read_bytes() for p in sorted(folder.glob("*"))} if folder.exists() else {}


def test_a_dry_run_on_a_clean_directory_creates_nothing_in_data_drafts(world, config_path, monkeypatch, capsys) -> None:
    seed(world)
    use_client(monkeypatch, fakes.FakeAnthropic(respond=respond))
    assert structure.main(["--config", str(config_path)]) == 0
    assert not (world / "drafts").exists()
    assert "nothing was written to data/drafts/" in capsys.readouterr().out


def test_a_dry_run_leaves_existing_drafts_byte_for_byte_alone(world, config_path, monkeypatch) -> None:
    seed(world)
    use_client(monkeypatch, fakes.FakeAnthropic(respond=respond))
    structure.main(["--config", str(config_path), "--submit"])
    before = drafts_dir_snapshot(world)
    assert before[f"{COMPANY.cik}.jsonl"]
    assert structure.main(["--config", str(config_path)]) == 0  # answers are current: it could have re-derived them, and still writes nothing
    assert drafts_dir_snapshot(world) == before


def test_a_dry_run_after_a_prompt_change_no_longer_empties_the_drafts(world, config_path, monkeypatch, capsys) -> None:
    """The bug: a new prompt makes every archived answer stale, so a dry run derived nothing and wrote nothing over the drafts."""
    seed(world)
    use_client(monkeypatch, fakes.FakeAnthropic(respond=respond))
    structure.main(["--config", str(config_path), "--submit"])
    before = drafts_dir_snapshot(world)
    monkeypatch.setattr(structure, "_INTRO_SENTENCE", structure._INTRO_SENTENCE + " Be careful.")
    client = use_client(monkeypatch, fakes.FakeAnthropic(respond=respond))
    assert structure.main(["--config", str(config_path)]) == 0
    assert drafts_dir_snapshot(world) == before and client.messages.batches.created == []
    assert "0 already answered, 3 to send" in capsys.readouterr().out  # and it still says the answers are stale


def test_a_dry_run_still_reports_what_the_archived_answers_would_give(world, config_path, monkeypatch, capsys) -> None:
    seed(world)
    use_client(monkeypatch, fakes.FakeAnthropic(respond=respond))
    structure.main(["--config", str(config_path), "--submit"])
    capsys.readouterr()
    structure.main(["--config", str(config_path)])
    assert "EXMP: 1 drafts, 0 rejected" in capsys.readouterr().out


def test_submit_with_everything_answered_still_rewrites_the_drafts_from_the_archive(world, config_path, monkeypatch) -> None:
    seed(world)
    use_client(monkeypatch, fakes.FakeAnthropic(respond=respond))
    structure.main(["--config", str(config_path), "--submit"])
    path = world / "drafts" / f"{COMPANY.cik}.jsonl"
    path.write_text("")  # something went wrong with the file
    second = use_client(monkeypatch, fakes.FakeAnthropic(respond=respond))
    assert structure.main(["--config", str(config_path), "--submit"]) == 0 and second.messages.create_calls == []
    assert len(path.read_text().splitlines()) == 1  # re-derived, for free


def test_every_request_the_pipeline_sends_is_at_temperature_zero(world, config_path, monkeypatch) -> None:
    seed(world)
    client = use_client(monkeypatch, fakes.FakeAnthropic(respond=respond))
    structure.main(["--config", str(config_path), "--submit"])
    assert [c["extra_body"] for c in client.messages.create_calls] == [{"temperature": 0}]  # the canary, a direct call
    assert [item["params"]["temperature"] for item in client.messages.batches.created[0]] == [0, 0]


def test_a_section_that_produced_nothing_is_listed_only_when_the_run_is_a_submit(world, config_path, monkeypatch) -> None:
    write_candidates(world, [candidate(10, "Revenue is expected to be $65.0 billion."),
                             candidate(300, "Tax rate\n17.0%", method="section", heading="Q1 FY27 Guidance")])
    use_client(monkeypatch, fakes.FakeAnthropic(respond=respond))
    structure.main(["--config", str(config_path)])
    assert not (world / "drafts").exists()
    structure.main(["--config", str(config_path), "--submit"])
    rows = [json.loads(l) for l in (world / "drafts" / f"{COMPANY.cik}.empty_blocks.jsonl").read_text().splitlines()]
    assert [(r["heading"], r["reason"], r["evidence"]["excerpt"]) for r in rows] == [("Q1 FY27 Guidance", "the model returned no items", "Tax rate\n17.0%")]
    assert rows[0]["custom_id"] == structure.custom_id(candidate(300, "Tax rate\n17.0%", method="section", heading="Q1 FY27 Guidance"))


def test_no_empty_blocks_file_when_every_block_gave_a_draft(world, config_path, monkeypatch) -> None:
    write_candidates(world, [candidate(300, "Revenue\n$5.0 billion to $6.0 billion", method="section", heading="Q1 FY27 Guidance")])
    use_client(monkeypatch, fakes.FakeAnthropic(respond=respond))
    structure.main(["--config", str(config_path), "--submit"])
    assert not (world / "drafts" / f"{COMPANY.cik}.empty_blocks.jsonl").exists()
    assert (world / "drafts" / f"{COMPANY.cik}.jsonl").exists()


def test_the_dry_run_line_counts_the_empty_blocks(world, config_path, monkeypatch, capsys) -> None:
    write_candidates(world, [candidate(300, "Tax rate\n17.0%", method="section", heading="Q1 FY27 Guidance")])
    use_client(monkeypatch, fakes.FakeAnthropic(respond=respond))
    structure.main(["--config", str(config_path), "--submit"])
    capsys.readouterr()
    structure.main(["--config", str(config_path)])
    assert "1 outlook blocks with no draft" in capsys.readouterr().out


# --- a batch that has not ended writes nothing --------------------------------------------------------


def test_a_submit_whose_batch_has_not_ended_writes_nothing_to_data_drafts(world, config_path, monkeypatch, capsys) -> None:
    seed(world)
    client = use_client(monkeypatch, fakes.FakeAnthropic(respond=respond, batch_ready=False))
    assert structure.main(["--config", str(config_path), "--submit", "--wait-minutes", "0"]) == 5
    assert not (world / "drafts").exists()  # not the drafts, not the rejects, not the empty-block list
    err = capsys.readouterr().err
    assert "has not ended, so nothing was written to data/drafts/" in err and "msgbatch_1" in err
    assert len(client.messages.batches.created) == 1 and llm.RawArchive("03_structure").state_path.exists()  # and it is marked as pending


def test_a_batch_that_has_not_ended_leaves_complete_drafts_byte_for_byte_alone(world, config_path, monkeypatch) -> None:
    """The case that mattered: the canary is archived at once, so a pending run holds one answer, and deriving from it used to overwrite the rest."""
    seed(world)
    use_client(monkeypatch, fakes.FakeAnthropic(respond=respond))
    structure.main(["--config", str(config_path), "--submit"])
    before = drafts_dir_snapshot(world)
    assert before[f"{COMPANY.cik}.jsonl"]
    monkeypatch.setattr(structure, "_INTRO_SENTENCE", structure._INTRO_SENTENCE + " Be careful.")  # every answer is stale now
    use_client(monkeypatch, fakes.FakeAnthropic(respond=respond, batch_ready=False))
    assert structure.main(["--config", str(config_path), "--submit", "--wait-minutes", "0"]) == 5
    assert drafts_dir_snapshot(world) == before


def test_running_the_same_command_again_resumes_the_pending_batch_without_resubmitting_and_then_writes(world, config_path, monkeypatch) -> None:
    seed(world)
    client = use_client(monkeypatch, fakes.FakeAnthropic(respond=respond, batch_ready=False))
    assert structure.main(["--config", str(config_path), "--submit", "--wait-minutes", "0"]) == 5
    assert not (world / "drafts").exists()
    client.batch_ready = True  # the batch ends
    assert structure.main(["--config", str(config_path), "--submit", "--wait-minutes", "0"]) == 0
    assert len(client.messages.batches.created) == 1 and len(client.messages.create_calls) == 1  # nothing was sent a second time
    drafts = [json.loads(l) for l in (world / "drafts" / f"{COMPANY.cik}.jsonl").read_text().splitlines()]
    assert [d["assumption"]["metric"] for d in drafts] == ["revenue"]
    assert not llm.RawArchive("03_structure").state_path.exists()


def test_a_submit_that_ends_with_some_requests_failed_still_writes_what_was_answered(world, config_path, monkeypatch, capsys) -> None:
    """Only a batch that has not ended is held back. One that ended and lost a request writes the rest, and says to run again."""
    seed(world)
    use_client(monkeypatch, fakes.FakeAnthropic(respond=respond, fail_ids=(structure.custom_id(candidate(200, "Revenue was $60.0 billion.")),)))
    assert structure.main(["--config", str(config_path), "--submit"]) == 0
    assert (world / "drafts" / f"{COMPANY.cik}.jsonl").exists() and "did not succeed" in capsys.readouterr().out


def respond_by_number(params: dict) -> str:
    """A revenue draft whose figure is the one in the line to assess, in a quarter of its own (65 is Q1, 70 is Q2, 75 is Q3),
    so that each candidate gives a draft that dedupe keeps."""
    user = params["messages"][0]["content"]
    line = next((l for l in user.splitlines() if l.startswith("Line to assess:")), "")
    match = re.search(r"\$(\d+\.\d+) billion", line)
    if not match:
        return json.dumps({"items": []})
    value = float(match.group(1))
    return json.dumps({"items": [item(period=f"Q{int((value - 60) // 5)} FY2027", low=value, high=value)]})


def three_revenue_candidates(world):
    write_candidates(world, [candidate(10, "Revenue is expected to be $65.0 billion."), candidate(200, "Revenue is expected to be $70.0 billion."),
                             candidate(400, "Revenue is expected to be $75.0 billion.")])
    return [structure.custom_id(candidate(s, f"x")) for s in (10, 200, 400)]


def drafted_figures(world) -> list[float]:
    rows = [json.loads(l) for l in (world / "drafts" / f"{COMPANY.cik}.jsonl").read_text().splitlines()]
    return sorted(r["assumption"]["target_low"] for r in rows)


def test_rerunning_after_a_failed_request_keeps_the_drafts_the_first_run_made(world, config_path, monkeypatch) -> None:
    """The re-run sends only the request that failed. The drafts written afterwards must still hold the other two."""
    cids = three_revenue_candidates(world)
    use_client(monkeypatch, fakes.FakeAnthropic(respond=respond_by_number, fail_ids=(cids[1],)))
    assert structure.main(["--config", str(config_path), "--submit"]) == 0
    assert drafted_figures(world) == [65.0, 75.0]  # the failed one is missing
    again = use_client(monkeypatch, fakes.FakeAnthropic(respond=respond_by_number))
    assert structure.main(["--config", str(config_path), "--submit"]) == 0
    assert len(again.messages.create_calls) == 1 and again.messages.batches.created == []  # only the one that failed was sent
    assert drafted_figures(world) == [65.0, 70.0, 75.0]  # all three: it used to hold only the retried one


def test_resuming_a_pending_batch_writes_the_drafts_of_every_request_and_not_just_the_batch(world, config_path, monkeypatch) -> None:
    three_revenue_candidates(world)
    client = use_client(monkeypatch, fakes.FakeAnthropic(respond=respond_by_number, batch_ready=False))
    assert structure.main(["--config", str(config_path), "--submit", "--wait-minutes", "0"]) == 5
    client.batch_ready = True
    assert structure.main(["--config", str(config_path), "--submit", "--wait-minutes", "0"]) == 0
    assert drafted_figures(world) == [65.0, 70.0, 75.0]  # the canary's draft (65.0) is in the archive, not in the batch


# --- a request answers with a list of zero or more assumptions -------------------------------------------------


def test_a_request_answers_with_a_list_of_zero_or_more_assumptions(world) -> None:
    """The model's answer is {"items": [...]}: an empty list for a sentence that is not guidance (not a `null`), one item, or one per metric."""
    for block in (False, True):
        top = structure.item_schema(METRICS, block=block)
        assert top["required"] == ["items"] and top["properties"]["items"]["type"] == "array"  # a list, and it is always there
        assert "minItems" not in top["properties"]["items"]  # so it may be empty
    s = "Revenue is expected to be $65.0 billion. Gross margin is expected to be 74.0%. Tax rate is expected to be 17.0%."
    write_candidates(world, [candidate(10, s)])
    _, index, _ = structure.build_requests([COMPANY], {"8-K"})
    cid = structure.custom_id(candidate(10, s))
    gm = item("gross margin GAAP", "percent", "Q4 FY2026", 74.0, 74.0)
    tax = item("tax rate", "percent", "Q4 FY2026", 17.0, 17.0)
    for items, expected in (([], 0), ([item()], 1), ([item(), gm, tax], 3)):
        drafts, rejects, _ = structure.derive_drafts(index, {cid: result(cid, *items)}, METRICS)
        assert len(drafts[COMPANY.cik]) == expected and rejects[COMPANY.cik] == []


# --- second pilot review: bare value lines, half years, growth vs level, parentheses -------------------


@pytest.mark.parametrize(
    "excerpt, bare",
    [
        ("$915 million", True),
        ("$755 million", True),
        ("25 million", True),
        ("62.3%", True),
        ("8%, plus or minus 1%", False),  # "plus or minus" is not a unit word
        ("Operating expenses - GAAP\n$915 million", False),  # the label line is there
        ("Revenue is expected to be $65.0 billion.", False),
    ],
)
def test_is_bare_value(excerpt, bare) -> None:
    assert structure.is_bare_value(excerpt) is bare


def test_check_not_bare_value_rejects_a_number_with_nothing_saying_what_it_is() -> None:
    with pytest.raises(ValueError, match="bare value"):
        structure.check_not_bare_value("$915 million")
    structure.check_not_bare_value("Operating expenses - GAAP\n$915 million")  # does not raise


@pytest.mark.parametrize(
    "heading, names_a_metric",
    [
        ("Updated Q4 Fiscal 2019 Guidance", False),
        ("Full Year FY31 Guidance", False),
        ("Q4 FY26 Guidance", False),
        ("Guidance", False),
        ("GAAP diluted earnings per share guidance", True),
        ("Adjusted diluted earnings per share guidance", True),
        (None, False),
    ],
)
def test_heading_names_a_metric(heading, names_a_metric) -> None:
    assert structure.heading_names_a_metric(heading) is names_a_metric


def test_a_bare_value_row_is_not_rejected_when_the_heading_alone_names_the_metric() -> None:
    """From tests/fixtures/pilot_run2.json, candidate b-0000027419-000002741924000126-15105 (Target, EPS GAAP,
    FY2024): Target's EPS table has no label line of its own, only footnote rows below the numbers ("Estimated
    adjustments", "Other (a)") -- the section heading "GAAP diluted earnings per share guidance" is the only
    label, and it already names the metric completely. Rejecting this as bare would undo the dedupe fix that
    lets this exact row surface from its own table instead of a shadowing sentence draft."""
    excerpt = "$1.95 - $2.35 $8.60 - $9.60"
    with pytest.raises(ValueError, match="bare value"):
        structure.check_not_bare_value(excerpt)  # no heading given: still bare
    structure.check_not_bare_value(excerpt, "GAAP diluted earnings per share guidance")  # does not raise
    assert structure.is_bare_value(excerpt, "Updated Q4 Fiscal 2019 Guidance") is True  # a period-only heading is no label


def test_the_nvda_bare_value_pilot_finding_is_now_rejected(world) -> None:
    """From data/review/0001045810.csv, draft 01D28W2W00GA9TCEG6AGFXF34T (NVDA, operating expenses GAAP, Q4
    FY2019): the model pointed line_first=line_last at "$915 million" alone; its label sits four lines above in
    a previous/updated, GAAP/non-GAAP table too tangled to safely re-pair by grabbing the nearest line, so it
    is rejected rather than paired with the wrong label."""
    lines = ["Revenue", "$2.70 billion, plus or minus 2%", "$2.20 billion, plus or minus 2%", "Gross margin - GAAP",
             "Gross margin - non-GAAP", "62.3%, plus or minus 50 bps", "62.5%, plus or minus 50 bps",
             "55.0%, plus or minus 100 bps", "56.0%, plus or minus 100 bps", "Operating expenses - GAAP",
             "Operating expenses - non-GAAP", "$930 million", "$755 million", "$915 million", "$755 million"]
    block = candidate(10, "\n".join(lines), method="section", heading="Updated Q4 Fiscal 2019 Guidance")
    write_candidates(world, [block])
    _, index, _ = structure.build_requests([COMPANY], {"8-K"})
    bid = structure.custom_id(block)
    res = {bid: result(bid, item("operating expenses GAAP", "USD millions", "Q4 FY2019", 915.0, 915.0, lines=(14, 14)))}
    drafts, rejects, _ = structure.derive_drafts(index, res)
    assert drafts[COMPANY.cik] == []
    assert "bare value" in rejects[COMPANY.cik][0]["reason"]


@pytest.mark.parametrize(
    "sentence",
    ["in the back half of the year", "in the second half of the year", "in the first half", "for 1H", "for H2",
     "for the remainder of the year"],
)
def test_half_year_phrases_are_recognised(sentence) -> None:
    assert structure._HALF_YEAR.search(sentence)


def test_a_full_year_period_is_rejected_when_the_evidence_names_the_back_half_or_the_remainder_of_the_year() -> None:
    with pytest.raises(ValueError, match="half-year"):
        structure.check_period("FY2022", "an operating margin rate in a range around 6% in the back half of the year.")
    with pytest.raises(ValueError, match="half-year"):
        structure.check_period("FY2022", "guidance for the remainder of the year is unchanged.")


def test_a_half_year_qualifier_is_not_caught_when_the_same_excerpt_also_names_a_different_metrics_full_year() -> None:
    """From data/review/0000027419.csv, draft 01GAMHK300HZSHX7542M97T27R (Target, operating margin GAAP FY2022):
    the sentence conflates two metrics, "full-year revenue growth" and an operating margin "in the back half of
    the year". check_period's full-year override reads the whole sentence, so full-year language for revenue
    still lets the half-year-qualified margin claim through. Scoping the override to just the metric's own
    clause is a bigger change than the phrase list this fix adds; recorded here as the known edge it leaves
    open, not a regression."""
    evidence = ("While the Company is planning cautiously for the remainder of the year, current trends support the "
                "company's prior guidance for full-year revenue growth in the low- to mid-single digit range, and an "
                "operating margin rate in a range around 6% in the back half of the year.")
    structure.check_period("FY2022", evidence)  # does not raise


@pytest.mark.parametrize(
    "evidence",
    ["revenue growth of 9 to 10 percent", "an increase of $50 million", "a decline of $10 million",
     "revenue up 12 percent", "revenue down 5 percent", "operating income is expected to grow more than $1 billion"],
)
def test_change_words_are_recognised(evidence) -> None:
    assert structure._CHANGE_WORDS.search(evidence)


def test_growth_language_on_a_dollar_metric_is_rejected_as_a_change_not_a_level() -> None:
    """From data/review/0000027419.csv, draft 01GTAN380019H5W4JFCY1MJWH7 (Target, operating income FY2023):
    "expected to grow more than $1 billion" was captured as a $1 billion floor, but it states how much MORE
    operating income will be, not what it will BE."""
    evidence = ("Operating income is expected to grow more than $1 billion, and GAAP EPS and adjusted EPS are both "
                "expected to range from $7.75 to $8.75.")
    with pytest.raises(ValueError, match="change"):
        structure.check_not_a_change("operating income", (1.0, None), evidence, METRIC_KINDS)


def test_growth_language_on_a_percent_metric_is_unaffected() -> None:
    structure.check_not_a_change("comparable sales", (2.0, 3.0), "the Company expects comparable sales growth of 2 to 3 percent", METRIC_KINDS)  # does not raise


def test_growth_language_about_a_different_sentences_subject_does_not_disqualify_this_items_own_clean_figure() -> None:
    """From tests/fixtures/pilot_run2.json, candidate b-0001045810-000104581022000136-2785 (NVIDIA, revenue Q3
    FY2023): the outlook bullet states a clean absolute revenue figure, then runs on into colour about specific
    segments ("Gaming ... revenue are expected to decline sequentially ... offset by ... growth in Data Center").
    That colour is about a different subject and a different number, not this item's own $5.90 billion, so it
    must not disqualify it."""
    evidence = (
        "Revenue is expected to be $5.90 billion, plus or minus 2%. Gaming and Professional Visualization revenue "
        "are expected to decline sequentially, as OEMs and channel partners reduce inventory levels to align with "
        "current levels of demand and prepare for NVIDIA's new product generation. The company expects that "
        "decline to be partially offset by sequential growth in Data Center and Automotive."
    )
    structure.check_not_a_change("revenue", (5.90, 5.90), evidence, METRIC_KINDS)  # does not raise


def test_growth_language_in_the_same_sentence_as_a_different_number_does_not_disqualify_this_one() -> None:
    evidence = "Operating income is expected to be $5 billion. Free cash flow is expected to grow to $2 billion."
    structure.check_not_a_change("operating income", (5.0, 5.0), evidence, METRIC_KINDS)  # does not raise: "grow" is about the other sentence's number


def test_the_target_operating_income_growth_draft_is_now_rejected(world) -> None:
    s = ("Operating income is expected to grow more than $1 billion, and GAAP EPS and adjusted EPS are both "
         "expected to range from $7.75 to $8.75.")
    (drafts, rejects, _), _ = derive(world, [candidate(10, s)], {cid(10): result(cid(10), item("operating income", "USD billions", "FY2027", 1.0, None))})
    assert drafts[COMPANY.cik] == []
    assert "change" in rejects[COMPANY.cik][0]["reason"]


def test_parens_on_a_rate_metric_are_read_as_positive_without_negative_benefit_or_loss_wording() -> None:
    """From data/review/0001108524.csv, drafts 01EGHEM50045QREN430XTJJX0B (CRM, tax rate, Q3 FY2021) and
    01EGHEM500AFFS91Z45D9TTDXW (FY2021): the model read "(20%)" and "(146%)" as -20 and -146, but the excerpt
    never says negative, benefit or loss: Salesforce's tax provision was unusually large that year, not negative."""
    evidence = ("(1) The company's GAAP tax provision is expected to be approximately (20%) for the three months "
                "ended October 31, 2020, and approximately (146%) for the year ended January 31, 2021.")
    low, high, note = structure.fix_parens_sign("tax rate", -20.0, -20.0, evidence, METRIC_KINDS)
    assert (low, high) == (20.0, 20.0) and note is not None and "positive" in note
    low, high, note = structure.fix_parens_sign("tax rate", -146.0, -146.0, evidence, METRIC_KINDS)
    assert (low, high) == (146.0, 146.0) and note is not None


@pytest.mark.parametrize("word", ["negative", "benefit", "loss"])
def test_parens_stay_negative_on_a_rate_metric_when_the_evidence_says_so(word) -> None:
    evidence = f"The tax rate is expected to be approximately (20%), reflecting a discrete tax {word}."
    assert structure.fix_parens_sign("tax rate", -20.0, -20.0, evidence, METRIC_KINDS) == (-20.0, -20.0, None)


def test_parens_stay_negative_on_eps_and_other_income() -> None:
    for metric in ("EPS GAAP", "other income and expense"):
        result_ = structure.fix_parens_sign(metric, -0.44, -0.42, "GAAP earnings (loss) per share ($0.44) - ($0.42)", METRIC_KINDS)
        assert result_ == (-0.44, -0.42, None)


def test_fix_parens_sign_does_nothing_without_a_negative_value_or_without_parentheses() -> None:
    assert structure.fix_parens_sign("tax rate", 20.0, 20.0, "(approximately 20%)", METRIC_KINDS) == (20.0, 20.0, None)
    assert structure.fix_parens_sign("tax rate", -20.0, -20.0, "approximately -20%, no parens here", METRIC_KINDS) == (-20.0, -20.0, None)


def test_the_crm_tax_rate_parens_draft_is_corrected_and_the_rule_is_logged(world) -> None:
    s = ("(1) The company's GAAP tax provision is expected to be approximately (20%) for the three months ended "
         "October 31, 2020, and approximately (146%) for the year ended January 31, 2021.")
    (drafts, rejects, _), _ = derive(world, [candidate(10, s)], {cid(10): result(cid(10), item("tax rate", "percent", "Q3 FY2021", -20.0, -20.0))})
    (draft,) = drafts[COMPANY.cik]
    assert (draft["assumption"]["target_low"], draft["assumption"]["target_high"]) == (20.0, 20.0)
    assert draft["parens_note"] is not None and "positive" in draft["parens_note"]
    assert rejects[COMPANY.cik] == []


def test_a_draft_with_no_parens_correction_carries_no_note(world) -> None:
    (drafts, _, _), _ = derive(world, [candidate(10, "Revenue is expected to be $65.0 billion.")], {cid(10): result(cid(10), item(low=65.0, high=65.0))})
    assert drafts[COMPANY.cik][0]["parens_note"] is None


# --- "expense of" forces other income and expense negative ------------------


def test_expense_of_forces_other_income_and_expense_negative() -> None:
    """From data/drafts/0001045810.jsonl: the identical phrase "expected to be an expense of approximately
    $55 million" came back as +55 on draft 01EG207V00AKSRWF61WMN81KJG and -55 on 01EQCAD8008K9G9KHC9XQXV8MY.
    "an expense of $X" is a net expense whatever sign the model gave it."""
    evidence = "GAAP and non-GAAP other income and expense are both expected to be an expense of approximately $55 million."
    low, high, note = structure.fix_expense_of_sign("other income and expense", 55.0, 55.0, evidence)
    assert (low, high) == (-55.0, -55.0) and note is not None and "expense" in note


def test_expense_of_is_idempotent_when_the_model_already_gave_a_negative_value() -> None:
    evidence = "GAAP and non-GAAP other income and expense are expected to be an expense of approximately $60 million."
    assert structure.fix_expense_of_sign("other income and expense", -60.0, -60.0, evidence) == (-60.0, -60.0, None)


def test_expense_of_flips_a_range_keeping_the_more_negative_end_as_the_low() -> None:
    evidence = "Other income and expense is expected to be an expense of $50 million to $60 million."
    low, high, note = structure.fix_expense_of_sign("other income and expense", 50.0, 60.0, evidence)
    assert (low, high) == (-60.0, -50.0) and note is not None


def test_expense_of_does_nothing_for_another_metric_or_without_the_phrase() -> None:
    assert structure.fix_expense_of_sign("operating income", 55.0, 55.0, "an expense of $55 million") == (55.0, 55.0, None)
    assert structure.fix_expense_of_sign("other income and expense", 55.0, 55.0, "approximately $55 million of expense") == (55.0, 55.0, None)


def test_the_expense_of_draft_is_corrected_and_the_rule_is_logged(world) -> None:
    s = "GAAP and non-GAAP other income and expense are both expected to be an expense of approximately $55 million."
    (drafts, rejects, _), _ = derive(world, [candidate(10, s)], {
        cid(10): result(cid(10), item("other income and expense", "USD millions", "Q3 FY2021", 55.0, 55.0))
    })
    (draft,) = drafts[COMPANY.cik]
    assert (draft["assumption"]["target_low"], draft["assumption"]["target_high"]) == (-55.0, -55.0)
    assert draft["parens_note"] is not None and "expense" in draft["parens_note"]
    assert rejects[COMPANY.cik] == []


# --- third pilot review: percent-metric plus-or-minus, and relative-to-prior-period language ------------


def test_a_percent_metrics_own_spread_is_always_absolute() -> None:
    """From data/review/0001045810.csv, draft 01EG207V00Q7XTVDP923BJVZ04 (NVDA, tax rate, Q3 FY2021): the
    model read "8 percent, plus or minus 1 percent" as relative (7.92 to 8.08), though the identical phrasing
    elsewhere in the same filing correctly came back absolute (7 to 9)."""
    it = item("tax rate", "percent", "Q3 FY2021", 8.0, 8.0, pm=1.0, kind="percent")
    fixed = structure.with_absolute_percent_spread("tax rate", it, METRIC_KINDS)
    assert fixed["plus_minus_kind"] == "absolute"
    assert structure.resolve_range(fixed) == (7.0, 9.0)


def test_a_dollar_metrics_relative_spread_is_unaffected() -> None:
    it = item("revenue", "USD billions", "Q4 FY2026", 108.0, 108.0, pm=2.0, kind="percent")
    fixed = structure.with_absolute_percent_spread("revenue", it, METRIC_KINDS)
    assert fixed["plus_minus_kind"] == "percent"
    assert structure.resolve_range(fixed) == (105.84, 110.16)  # unchanged: 2% of 108, not fixed points


def test_a_percent_metrics_already_absolute_spread_is_left_alone() -> None:
    it = item("gross margin GAAP", "percent", "Q4 FY2026", 74.0, 74.0, pm=0.5, kind="absolute")
    assert structure.with_absolute_percent_spread("gross margin GAAP", it, METRIC_KINDS) is it


def test_the_nvda_tax_rate_pilot_finding_now_resolves_to_the_full_absolute_band(world) -> None:
    s = "GAAP and non-GAAP tax rates are both expected to be 8 percent, plus or minus 1 percent, excluding any discrete items."
    (drafts, rejects, _), _ = derive(world, [candidate(10, s)], {cid(10): result(cid(10), item("tax rate", "percent", "Q3 FY2021", 8.0, 8.0, pm=1.0, kind="percent"))})
    (draft,) = drafts[COMPANY.cik]
    assert (draft["assumption"]["target_low"], draft["assumption"]["target_high"]) == (7.0, 9.0)
    assert rejects[COMPANY.cik] == []


@pytest.mark.parametrize(
    "sentence",
    ["20 basis points higher than last year", "the rate was lower than the prior quarter",
     "well above the 2020 rate of 7.0 percent", "margin is expected to remain below last year's level",
     "operating margin compared to the prior year", "gross margin versus last year", "margin vs. last year"],
)
def test_relative_to_prior_period_phrases_are_recognised(sentence) -> None:
    assert structure._RELATIVE_TO_PRIOR.search(sentence)


def test_a_percent_metric_stated_only_relative_to_a_prior_period_is_rejected() -> None:
    """From data/review/0000027419.csv, drafts 01KJRFX500H24NN9APZ61TB1XC / 01KJRFX5000K2CZTGP0WVJYFZH /
    01KS1AX7009M235XX1TGTJ1C1A (Target, operating margin GAAP/non-GAAP, FY2026): the excerpt gives the size of
    the move (20 basis points) and last year's rate (4.6 percent), but never the new rate itself."""
    evidence = ("Full-year 2026 operating income margin rate approximately 20 basis points higher than the "
                "4.6 percent Adjusted operating income margin rate in 2025.")
    with pytest.raises(ValueError, match="relative to a prior period"):
        structure.check_not_relative_to_prior_period("operating margin GAAP", (20.0, 20.0), evidence, METRIC_KINDS)
    with pytest.raises(ValueError, match="relative to a prior period"):
        structure.check_not_relative_to_prior_period("operating margin GAAP", (20.0, None), "more than " + evidence, METRIC_KINDS)


def test_relative_language_does_not_disqualify_a_metric_that_also_states_its_own_absolute_figure() -> None:
    """From data/review/0000027419.csv, draft 01F60YR200NWN0T9F5S6S966DG (Target, operating margin GAAP,
    FY2021): "well above the 2020 rate of 7.0 percent" is relative wording, but 7.0 percent is also the item's
    own printed figure (its floor), so it is not rejected."""
    evidence = ("The Company expects positive single-digit comparable sales growth in the last two quarters of "
                "the year, and expects its full-year operating margin rate will be well above the 2020 rate of "
                "7.0 percent, with the potential to reach 8 percent or somewhat higher.")
    structure.check_not_relative_to_prior_period("operating margin GAAP", (7.0, None), evidence, METRIC_KINDS)  # does not raise

    synthetic = "Operating margin is expected to be approximately 21%, compared to 19% in the prior year."
    structure.check_not_relative_to_prior_period("operating margin GAAP", (21.0, 21.0), synthetic, METRIC_KINDS)  # does not raise


def test_relative_to_prior_period_check_is_scoped_to_the_sentence_with_this_items_own_number() -> None:
    evidence = "Tax rate is expected to be 21%. Gross margin was higher than last year."
    structure.check_not_relative_to_prior_period("tax rate", (21.0, 21.0), evidence, METRIC_KINDS)  # does not raise: different sentence


def test_relative_to_prior_period_check_is_unaffected_on_a_dollar_metric() -> None:
    evidence = "Revenue is expected to be $5 billion, compared to $4 billion last year."
    structure.check_not_relative_to_prior_period("revenue", (5.0, 5.0), evidence, METRIC_KINDS)  # does not raise: not a percent metric


def test_the_target_operating_margin_relative_pilot_finding_is_now_rejected(world) -> None:
    s = ("Full-year 2026 operating income margin rate approximately 20 basis points higher than the 4.6 percent "
         "Adjusted operating income margin rate in 2025.")
    (drafts, rejects, _), _ = derive(world, [candidate(10, s)], {cid(10): result(cid(10), item("operating margin GAAP", "basis points", "FY2026", 20.0, 20.0))})
    assert drafts[COMPANY.cik] == []
    assert "relative to a prior period" in rejects[COMPANY.cik][0]["reason"]
