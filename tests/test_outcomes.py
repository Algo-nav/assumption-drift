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


# --- main, end to end ------------------------------------------------------


@pytest.fixture
def config_path(world):
    cfg = common.load_config()
    cfg["companies"] = [{"name": COMPANY.name, "ticker": COMPANY.ticker, "cik": COMPANY.cik}]
    path = world / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


def respond(params: dict) -> str:
    user = params["messages"][0]["content"]
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


def test_a_dry_run_only_counts_tokens_and_writes_reasons(world, config_path, monkeypatch, capsys) -> None:
    seed_drafts(world, draft(), draft("D2", period="Q4 FY2030"))
    client = fakes.FakeAnthropic(respond=respond)
    monkeypatch.setattr(llm, "make_client", lambda: client)
    assert outcomes.main(["--config", str(config_path)]) == 0
    assert client.messages.create_calls == [] and client.messages.batches.created == []
    rows = {r["draft_id"]: r for r in read_outcomes(world)}
    assert rows["D1"]["outcome"] is None and rows["D1"]["outcome_reason"] == "outcome search not completed"
    assert "no later 8-K release" in rows["D2"]["outcome_reason"]
    assert "dry run" in capsys.readouterr().out


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
