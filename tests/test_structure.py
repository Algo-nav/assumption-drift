"""03_structure.py, against a fake client. Nothing here reaches the network or the real data/ directory."""

from __future__ import annotations

import hashlib
import importlib
import json
import re

import pytest
import yaml

import fakes
from pipeline import llm

structure = importlib.import_module("pipeline.03_structure")
common = importlib.import_module("pipeline.common")

COMPANY = common.Company("Example Corp", "EXMP", "0000000123")
ACC = "0000000123-24-000001"
FILE_URL = "https://www.sec.gov/Archives/edgar/data/123/000000012324000001/release.htm"


def item(metric="revenue", unit="USD billions", period="Q4 FY2026", low=65.0, high=65.0, pm=None, kind="none"):
    return {"metric": metric, "unit": unit, "target_period": period, "value_low": low, "value_high": high,
            "plus_minus": pm, "plus_minus_kind": kind}


def candidate(char_start, sentence, *, method="sentence", heading=None, lead_in=None, before=(), after=(),
              form="8-K", accession=ACC, filed_at="2024-02-01"):
    return {"cik": COMPANY.cik, "accession": accession, "filing_type": form, "filed_at": filed_at,
            "char_start": char_start, "char_end": char_start + len(sentence), "sentence": sentence, "text_version": 1,
            "capture_method": method, "heading": heading, "lead_in": lead_in,
            "context_before": list(before), "context_after": list(after)}


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
    [
        item(low=None, high=None),  # no number at all
        item(low=6000, high=5000),  # reversed
        item(low=100, high=110, pm=2.0, kind="percent"),  # a spread needs one centre value
        item(low=100, high=100, pm=None, kind="percent"),  # a kind with no spread
    ],
)
def test_resolve_range_refuses_what_it_cannot_work_out(it) -> None:
    with pytest.raises(ValueError):
        structure.resolve_range(it)


# --- building requests -----------------------------------------------------


def test_custom_ids_fit_the_batch_api_limit() -> None:
    cid = structure.custom_id(candidate(1234567, "x"))
    assert cid == "c-0000000123-000000012324000001-1234567" and len(cid) <= 64


def test_only_8k_candidates_are_sent_by_default_and_10k_10q_never(world) -> None:
    write_candidates(world, [
        candidate(10, "Revenue is expected to be $65.0 billion."),
        candidate(200, "We expect capex of $5 billion.", form="10-K", accession="0000000123-24-000009"),
        candidate(300, "We expect capex of $2 billion.", form="10-Q", accession="0000000123-24-000010"),
    ])
    requests, index, _ = structure.build_requests([COMPANY], {"8-K"})
    assert [index[r.custom_id][1]["filing_type"] for r in requests] == ["8-K"]


def test_a_sentence_too_long_for_an_excerpt_is_skipped_and_logged(world) -> None:
    write_candidates(world, [candidate(10, "x" * 401), candidate(500, "y" * 400)])
    requests, _, skipped = structure.build_requests([COMPANY], {"8-K"})
    assert len(requests) == 1
    assert "401 chars" in skipped[COMPANY.cik][0]["reason"]


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


def test_the_prompt_shows_everything_the_model_needs(world) -> None:
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


def test_the_system_prompt_and_schema_agree_on_the_units() -> None:
    assert structure.SCHEMA["properties"]["items"]["items"]["properties"]["unit"]["enum"] == structure.UNITS
    assert all(u in structure.SYSTEM_PROMPT for u in structure.UNITS)


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
    assert a["evidence"]["content_sha256"] == json.loads((world / "raw" / COMPANY.cik / f"{ACC}.meta.json").read_text())["content_sha256"]
    assert re.fullmatch(r"[0-7][0-9A-HJKMNP-TV-Z]{25}", draft["draft_id"])
    assert rejects[COMPANY.cik] == [] and dupes[COMPANY.cik] == []


def test_one_sentence_can_yield_several_drafts(world) -> None:
    both = result(cid(10), item("gaap gross margin", "percent", low=74.8, high=74.8), item("non-gaap gross margin", "percent", low=75.0, high=75.0))
    (drafts, _, _), _ = derive(world, [candidate(10, "GAAP and non-GAAP gross margins are expected to be 74.8% and 75.0%.")], {cid(10): both})
    assert [d["assumption"]["metric"] for d in drafts[COMPANY.cik]] == ["gaap gross margin", "non-gaap gross margin"]
    assert len({d["draft_id"] for d in drafts[COMPANY.cik]}) == 2


def test_an_empty_answer_is_simply_not_guidance(world) -> None:
    (drafts, rejects, _), _ = derive(world, [candidate(10, "Revenue was $89.0 billion.")], {cid(10): result(cid(10))})
    assert drafts[COMPANY.cik] == [] and rejects[COMPANY.cik] == []


@pytest.mark.parametrize(
    "bad, why",
    [
        (item(period="third quarter"), "target_period"),
        (item(period="FY27"), "target_period"),
        (item(metric=""), "metric"),
        (item(low=None, high=None), "no number"),
        (item(low=9.0, high=1.0), "reversed"),
    ],
)
def test_output_that_breaks_the_rules_is_rejected_with_a_reason_not_dropped(world, bad, why) -> None:
    (drafts, rejects, _), _ = derive(world, [candidate(10, "s")], {cid(10): result(cid(10), bad)})
    assert drafts[COMPANY.cik] == []
    assert why in rejects[COMPANY.cik][0]["reason"]


def test_output_that_is_not_the_expected_json_is_rejected(world) -> None:
    junk = llm.Result(cid(10), "succeeded", "I think this is guidance.", 1, 1, None, "b")
    (drafts, rejects, _), _ = derive(world, [candidate(10, "s")], {cid(10): junk})
    assert drafts[COMPANY.cik] == [] and "did not parse" in rejects[COMPANY.cik][0]["reason"]


def test_the_same_metric_period_and_range_twice_in_one_filing_is_one_draft(world) -> None:
    rows = [candidate(10, "Raises full year revenue guidance to $46.1 billion."), candidate(500, "We are raising revenue guidance to $46.1 billion.")]
    same = item(period="FY2027", low=46.1, high=46.1)
    (drafts, _, dupes), _ = derive(world, rows, {cid(10): result(cid(10), same), cid(500): result(cid(500), same)})
    assert len(drafts[COMPANY.cik]) == 1 and drafts[COMPANY.cik][0]["char_start"] == 10  # the first one wins
    assert "same metric, period and range" in dupes[COMPANY.cik][0]["reason"]


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
    cfg["companies"] = [{"name": COMPANY.name, "ticker": COMPANY.ticker, "cik": COMPANY.cik}]
    path = world / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


def respond(params: dict) -> str:
    user = params["messages"][0]["content"]
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
    assert "counted by the API" in out and "dry run: nothing was submitted" in out
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
    sent += [item["params"]["messages"][0]["content"] for batch in client.messages.batches.created for item in batch]
    assert len(sent) == 1 and "Revenue is expected to be $65.0 billion." in sent[0]
    assert not any("capex" in text for text in sent)


def test_a_dry_run_leaves_no_empty_output_files(world, config_path, monkeypatch) -> None:
    seed(world)
    use_client(monkeypatch, fakes.FakeAnthropic(respond=respond))
    assert structure.main(["--config", str(config_path)]) == 0
    drafts = world / "drafts"
    assert not drafts.exists() or all(p.stat().st_size > 0 for p in drafts.iterdir())
