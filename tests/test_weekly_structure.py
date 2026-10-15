"""02_candidates and 03_structure for the weekly refresh: --accessions, --sync, --max-usd and the tracked spend ledger."""

from __future__ import annotations

import hashlib
import importlib
import json

import yaml

import fakes
from pipeline import common, llm
from test_structure import (ACC, COMPANY, candidate, config_path, respond, seed, structure, use_client, world,  # noqa: F401  (fixtures)
                            write_candidates)

cand = importlib.import_module("pipeline.02_candidates")
OTHER_ACC = "0000000123-24-000002"


def new_filings(tmp_path, *entries):
    path = tmp_path / "new_filings.json"
    path.write_text(json.dumps({"run_date": "2026-09-27", "complete": True, "companies": sorted({e["cik"] for e in entries}),
                                "filings": list(entries)}))
    return path


def entry(accession=ACC, cik=COMPANY.cik, path="data/raw/x.html"):
    return {"cik": cik, "accession": accession, "form": "8-K", "filing_date": "2024-02-01", "status": "fetched" if path else "no_exhibit", "path": path}


def drafts(world):
    path = world / "drafts" / f"{COMPANY.cik}.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


def spend(world):
    path = world / "spend_ledger.jsonl"  # the conftest redirects data/spend_ledger.jsonl here
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


# --- the scope file ------------------------------------------------------------------------------------------------------


def test_the_scope_is_the_filings_that_have_a_document(tmp_path) -> None:
    path = new_filings(tmp_path, entry(ACC), entry(OTHER_ACC, path=None), entry("0000000456-24-000001", cik="0000000456"))
    assert common.load_accession_scope(path) == {COMPANY.cik: {ACC}, "0000000456": {"0000000456-24-000001"}}


# --- 02 ------------------------------------------------------------------------------------------------------------------


def write_filing(raw, cik, accession, filed, sentence):
    html = f"<html><body><p>Example Corp Announces Results</p><p>{sentence}</p></body></html>".encode()
    (raw / cik).mkdir(parents=True, exist_ok=True)
    (raw / cik / f"{accession}.html").write_bytes(html)
    (raw / cik / f"{accession}.meta.json").write_text(json.dumps({
        "cik": cik, "accession": accession, "filing_type": "8-K", "filed_at": filed, "http_status": 200,
        "raw_sha256": hashlib.sha256(html).hexdigest()}))


def test_02_with_accessions_reads_only_those_filings_and_merges_into_the_file(tmp_path, monkeypatch) -> None:
    raw, out = tmp_path / "raw", tmp_path / "cands"
    monkeypatch.setattr(cand, "RAW_DIR", raw)
    cfg = common.load_config()["candidates"]
    write_filing(raw, COMPANY.cik, ACC, "2024-02-01", "The Company expects revenue of $5.0 billion for the quarter.")
    write_filing(raw, COMPANY.cik, OTHER_ACC, "2024-05-01", "The Company expects revenue of $7.0 billion for the quarter.")
    cand.build_company(COMPANY, cfg, out)
    full = [json.loads(l) for l in (out / f"{COMPANY.cik}.jsonl").read_text().splitlines()]
    assert {r["accession"] for r in full} == {ACC, OTHER_ACC}

    write_filing(raw, COMPANY.cik, OTHER_ACC, "2024-05-01", "The Company expects revenue of $8.0 billion for the quarter.")  # the second filing changed
    stats = cand.build_company(COMPANY, cfg, out, accessions={OTHER_ACC})
    assert stats["documents"] == 1
    merged = [json.loads(l) for l in (out / f"{COMPANY.cik}.jsonl").read_text().splitlines()]
    assert [r for r in merged if r["accession"] == ACC] == [r for r in full if r["accession"] == ACC]  # the first filing's rows are untouched
    assert [r["sentence"] for r in merged if r["accession"] == OTHER_ACC] == ["The Company expects revenue of $8.0 billion for the quarter."]
    assert [r["accession"] for r in merged] == sorted(r["accession"] for r in merged)  # still in filing order


def test_02_with_accessions_on_a_fresh_checkout_writes_just_the_new_filings(tmp_path, monkeypatch) -> None:
    raw, out = tmp_path / "raw", tmp_path / "cands"
    monkeypatch.setattr(cand, "RAW_DIR", raw)
    write_filing(raw, COMPANY.cik, ACC, "2024-02-01", "The Company expects revenue of $5.0 billion for the quarter.")
    cand.build_company(COMPANY, common.load_config()["candidates"], out, accessions={ACC})
    assert {json.loads(l)["accession"] for l in (out / f"{COMPANY.cik}.jsonl").read_text().splitlines()} == {ACC}


def test_02_main_leaves_a_company_with_no_new_filing_alone(tmp_path, monkeypatch) -> None:
    cfg = common.load_config()
    cfg["companies"] = [{"name": "A", "ticker": "AAA", "cik": COMPANY.cik}, {"name": "B", "ticker": "BBB", "cik": "0000000456"}]
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(cfg))
    built = []
    monkeypatch.setattr(cand, "build_company", lambda company, c, out_dir=None, accessions=None: built.append((company.cik, accessions)) or {})
    assert cand.main(["--config", str(config), "--accessions", str(new_filings(tmp_path, entry(ACC)))]) == 0
    assert built == [(COMPANY.cik, {ACC})]


# --- 03 ------------------------------------------------------------------------------------------------------------------


def test_03_with_accessions_sends_only_those_candidates_and_merges_the_drafts(world, config_path, monkeypatch, tmp_path) -> None:
    write_candidates(world, [candidate(10, "Revenue is expected to be $65.0 billion."),
                             candidate(10, "Revenue is expected to be $66.0 billion.", accession=OTHER_ACC)])
    (world / "drafts").mkdir()
    keep = {"custom_id": f"c-{COMPANY.cik}-{OTHER_ACC.replace('-', '')}-5", "draft_id": "KEEP"}
    stale = {"custom_id": f"c-{COMPANY.cik}-{ACC.replace('-', '')}-5", "draft_id": "STALE"}
    (world / "drafts" / f"{COMPANY.cik}.jsonl").write_text(json.dumps(keep) + "\n" + json.dumps(stale) + "\n")
    client = use_client(monkeypatch, fakes.FakeAnthropic(respond=respond))
    assert structure.main(["--config", str(config_path), "--submit", "--accessions", str(new_filings(tmp_path, entry(ACC)))]) == 0
    sent = {cid for cid in llm.RawArchive("03_structure").load()}
    assert sent and all(cid.split("-")[2] == ACC.replace("-", "") for cid in sent)  # nothing from the other filing went out
    ids = [d["draft_id"] for d in drafts(world)]
    assert ids[0] == "KEEP" and "STALE" not in ids and len(ids) == 2  # the other filing's row stays; this filing's old row is replaced
    assert drafts(world)[1]["assumption"]["metric"] == "revenue"


def test_03_without_accessions_still_replaces_the_files(world, config_path, monkeypatch) -> None:
    seed(world)
    (world / "drafts").mkdir()
    (world / "drafts" / f"{COMPANY.cik}.jsonl").write_text(json.dumps({"custom_id": "c-x-1-5", "draft_id": "OLD"}) + "\n")
    use_client(monkeypatch, fakes.FakeAnthropic(respond=respond))
    assert structure.main(["--config", str(config_path), "--submit"]) == 0
    assert [d["draft_id"] for d in drafts(world)] != ["OLD"] and "OLD" not in [d["draft_id"] for d in drafts(world)]


def test_03_sync_sends_one_by_one_whatever_sync_below_says_and_logs_the_spend(world, config_path, monkeypatch) -> None:
    seed(world)
    client = use_client(monkeypatch, fakes.FakeAnthropic(respond=respond))
    assert structure.main(["--config", str(config_path), "--submit", "--sync"]) == 0  # the config says sync_below 0: always batch
    assert len(client.messages.create_calls) == 3 and client.messages.batches.created == []
    (line,) = spend(world)
    assert (line["stage"], line["requests"]) == ("03_structure", 3) and line["usd"] > 0 and set(line) == {"date", "stage", "requests", "usd"}
    assert [d["assumption"]["metric"] for d in drafts(world)] == ["revenue"]


def test_03_a_batch_run_logs_its_spend_too(world, config_path, monkeypatch) -> None:
    seed(world)
    use_client(monkeypatch, fakes.FakeAnthropic(respond=respond))
    assert structure.main(["--config", str(config_path), "--submit"]) == 0
    (line,) = spend(world)
    assert (line["stage"], line["requests"]) == ("03_structure", 3) and line["usd"] > 0


def test_03_max_usd_stops_before_anything_is_sent(world, config_path, monkeypatch, capsys) -> None:
    seed(world)
    client = use_client(monkeypatch, fakes.FakeAnthropic(respond=respond))
    assert structure.main(["--config", str(config_path), "--submit", "--sync", "--max-usd", "0.0000001"]) == 4
    assert client.messages.create_calls == [] and client.messages.batches.created == []
    assert drafts(world) == [] and spend(world) == []
    assert "--max-usd cap" in capsys.readouterr().err


def test_03_max_usd_is_priced_at_the_price_the_run_will_pay(world, config_path, monkeypatch) -> None:
    """Synchronous requests cost twice what the same batch does. A cap that fits the batch worst case but not the sync one stops a --sync run."""
    seed(world)
    cfg = llm.LlmConfig.from_config(common.load_config()["llm"])
    requests, _, _ = structure.build_requests([COMPANY], {"8-K"}, metrics=common.load_config()["metrics"])
    batch = llm.project(requests, [1000] * len(requests), exact=True, llm=cfg, expected_output_per_request=120)  # the fake counts 1000 a request
    between = batch.usd_worst * 1.5  # above the batch worst case, below the synchronous one (2x)
    client = use_client(monkeypatch, fakes.FakeAnthropic(respond=respond))
    assert structure.main(["--config", str(config_path), "--submit", "--sync", "--max-usd", str(between)]) == 4
    assert client.messages.create_calls == []
    assert structure.main(["--config", str(config_path), "--submit", "--max-usd", str(between)]) == 0  # as a batch it fits
    assert len(client.messages.batches.created) == 1


def test_03_max_usd_is_a_ceiling_across_the_stages_of_one_run() -> None:
    cap = llm.RunCap(0.01)
    cap.check(0.006)
    cap.add(0.006)
    try:
        cap.check(0.006)
    except llm.BudgetExceeded as exc:
        assert "0.0060 already spent this run" in str(exc)
    else:
        raise AssertionError("the second check should have been refused")
