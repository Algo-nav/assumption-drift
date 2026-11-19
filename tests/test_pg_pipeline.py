"""The real stages, 02 to 06, run twice on the same small company: once on files, once on Postgres. Same code, one
setting, and every list that comes out must be the same. Then the same stages inside a job, with its caps.

The model is the stand-in client from tests/fakes.py; nothing here reaches EDGAR or a model.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import re

import pytest
import yaml

import fakes
from pipeline import common, job, llm
from research_record import store

cand = importlib.import_module("pipeline.02_candidates")
structure = importlib.import_module("pipeline.03_structure")
verify = importlib.import_module("pipeline.03b_verify")
suggest = importlib.import_module("pipeline.03c_suggest")
outcomes = importlib.import_module("pipeline.04_outcomes")
review = importlib.import_module("pipeline.05_review")
publish = importlib.import_module("pipeline.06_publish")

pytestmark = pytest.mark.postgres

COMPANY = common.Company("Example Corp", "EXMP", "0000000123")
DOCS = [
    ("0010", "2024-02-01", "Example Corp Announces Financial Results for Third Quarter Fiscal 2024",
     ["Outlook", "Revenue is expected to be $5.0 billion to $6.0 billion for the fourth quarter of fiscal 2024."]),
    ("0012", "2024-05-01", "Example Corp Announces Financial Results for Fourth Quarter and Fiscal 2024",
     ["Fourth quarter revenue of $4.8 billion, up 10% from a year ago.", "Fiscal 2024 revenue was $19.5 billion."]),
    ("0013", "2024-08-01", "Example Corp Announces Financial Results for First Quarter Fiscal 2025",
     ["First quarter revenue of $5.3 billion.", "Fourth quarter revenue of $4.8 billion was below our guidance."]),
]
GUIDE = {"metric": "revenue", "unit": "USD billions", "target_period": "Q4 FY2024", "value_low": 5.0, "value_high": 6.0,
         "plus_minus": None, "plus_minus_kind": "none"}


def respond(params: dict) -> str:
    user, system = params["messages"][0]["content"], params["system"]
    if "verified" in params["output_config"]["format"]["schema"]["properties"]:
        return json.dumps({"verified": True, "reason": "the excerpt states the range and the quarter", "class": "other"})
    if system == outcomes.WITHDRAWAL_SYSTEM:
        return json.dumps({"indices": []})
    if system == outcomes.ACK_SYSTEM:
        hit = re.search(r"\[(\d+)\][^\[]*?LINE: [^\n]*below our guidance", user)
        return json.dumps({"indices": [int(hit.group(1))] if hit else []})
    if system == outcomes.OUTCOME_SYSTEM:
        hit = re.search(r"\[(\d+)\][^\[]*?LINE: Fourth quarter revenue of \$4\.8 billion", user)
        return json.dumps({"index": int(hit.group(1)), "value": 4.8, "unit": "USD billions"} if hit else {"index": None, "value": None, "unit": "none"})
    if "Section lines:" in user:
        return json.dumps({"items": []})
    return json.dumps({"items": [GUIDE] if "Line to assess: Revenue is expected to be $5.0 billion to $6.0 billion" in user else []})


@pytest.fixture
def world(tmp_path, monkeypatch):
    raw = tmp_path / "raw" / COMPANY.cik
    raw.mkdir(parents=True)
    for suffix, filed, title, lines in DOCS:
        acc = f"0000000123-24-00{suffix}"
        html = ("<html><body>" + f"<p>{title}</p>" + "".join(f"<p>{line}</p>" for line in lines) + "</body></html>").encode()
        (raw / f"{acc}.html").write_bytes(html)
        (raw / f"{acc}.meta.json").write_text(json.dumps({
            "cik": COMPANY.cik, "accession": acc, "filing_type": "8-K", "doc_type": "EX-99.1", "filed_at": filed, "http_status": 200,
            "final_url": f"https://www.sec.gov/Archives/edgar/data/123/{acc.replace('-', '')}/release.htm",
            "fetched_at": "2026-09-20T12:00:00+00:00", "raw_sha256": hashlib.sha256(html).hexdigest(),
            "content_sha256": hashlib.sha256(html).hexdigest()}))
    cfg = common.load_config()
    cfg["llm"]["sync_below"] = 0
    cfg["companies"] = [{"name": COMPANY.name, "ticker": COMPANY.ticker, "cik": COMPANY.cik}]
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))
    monkeypatch.setattr(llm, "make_client", lambda: fakes.FakeAnthropic(respond=respond))
    return tmp_path


def point_at(world, monkeypatch, name: str):
    """Every directory the stages know, under world/{name}/ (the cached filings are shared: they are never rows)."""
    root = world / name
    for module, attrs in [(cand, ["CANDIDATES_DIR"]), (structure, ["CANDIDATES_DIR", "DRAFTS_DIR"]), (outcomes, ["DRAFTS_DIR", "OUTCOMES_DIR", "REVIEW_DIR"]),
                          (review, ["CANDIDATES_DIR", "DRAFTS_DIR", "OUTCOMES_DIR", "REVIEW_DIR"]), (verify, ["REVIEW_DIR"]),
                          (suggest, ["REVIEW_DIR"]), (publish, ["REVIEW_DIR"]), (common, ["CANDIDATES_DIR", "DRAFTS_DIR", "OUTCOMES_DIR", "REVIEW_DIR"])]:
        for attr in attrs:
            monkeypatch.setattr(module, attr, root / attr.split("_")[0].lower())
    for module in (cand, review, common):
        monkeypatch.setattr(module, "RAW_DIR", world / "raw")
    monkeypatch.setattr(llm, "BATCH_DIR", root / "batches")
    return root


def run_the_stages(world, root) -> dict:
    config = ["--config", str(world / "config.yaml")]
    cfg = common.load_config(world / "config.yaml")
    cand.build_company(COMPANY, cfg["candidates"], out_dir=root / "candidates")
    assert structure.main(config + ["--submit"]) == 0
    assert review.main(config) == 0
    assert verify.main(config + ["--submit"]) == 0
    assert suggest.main(config) == 0
    assert outcomes.main(config + ["--submit"]) == 0
    assert review.main(config + ["--attach-outcomes"]) == 0
    cik = COMPANY.cik
    records, invalid = publish.build_records([COMPANY], root / "review")
    return {
        "candidates": store.read_jsonl(root / "candidates" / f"{cik}.jsonl"),
        "drafts": store.read_jsonl(root / "drafts" / f"{cik}.jsonl"),
        "rejects": store.read_jsonl(root / "drafts" / f"{cik}.rejects.jsonl"),
        "skipped": store.read_jsonl(root / "drafts" / f"{cik}.skipped.jsonl"),
        "empty_blocks": store.read_jsonl(root / "drafts" / f"{cik}.empty_blocks.jsonl"),
        "outcomes": store.read_jsonl(root / "outcomes" / f"{cik}.jsonl"),
        "outcome_rejects": store.read_jsonl(root / "outcomes" / f"{cik}.rejects.jsonl"),
        "review": review.read_csv(root / "review" / f"{cik}.csv"),
        "archive": {step: {cid: (r.status, r.text, r.fingerprint) for cid, r in llm.RawArchive(step).load().items()}
                    for step in ("03_structure", "03b_verify", "04_outcomes", "04_acknowledgements", "04_withdrawals")},
        "committed": round(llm.Ledger().committed_usd(), 9),
        "release": [r.model_dump(mode="json") for r in records], "invalid": invalid,
    }


def test_the_stages_give_the_same_rows_on_files_and_on_postgres(world, pg, monkeypatch) -> None:
    pg.upsert_company(COMPANY.cik, ticker=COMPANY.ticker, name=COMPANY.name)
    monkeypatch.setenv("DATABASE_URL", pg.url)
    monkeypatch.setenv("STORE_SCHEMA", pg.schema)
    seen = {}
    for setting in ("files", "postgres"):
        monkeypatch.setenv("STORE", setting)
        root = point_at(world, monkeypatch, setting)
        seen[setting] = run_the_stages(world, root)
    on_files, on_postgres = seen["files"], seen["postgres"]
    for name in on_files:
        assert on_postgres[name] == on_files[name], name
    # and the run was a real one: a draft, its outcome, the miss acknowledged, an outlook block flagged
    assert len(on_files["drafts"]) == 1 and on_files["outcomes"][0]["outcome"]["reported_value"] == 4.8
    assert on_files["outcomes"][0]["acknowledgement"]["acknowledged_at"] == "2024-08-01"
    assert sorted(r["empty_block"] for r in on_files["review"]) == ["false", "true"]  # 03b puts the section-captured flag first
    row = next(r for r in on_files["review"] if r["empty_block"] == "false")
    assert row["aid_verify"] == "yes" and row["outcome.reported_value"] == "4.8" and row["acknowledged_at"] == "2024-08-01"
    assert on_files["committed"] > 0
    # the postgres run left nothing on disk but the files run's files, and the files run left nothing in the database
    assert not (world / "postgres").exists()
    assert (world / "files" / "review" / f"{COMPANY.cik}.csv").exists() and (world / "files" / "batches" / "ledger.jsonl").exists()
    assert pg._one("SELECT count(*) FROM statements") == 2 and pg._one("SELECT count(*) FROM candidates") == len(on_files["candidates"])
    assert pg._one("SELECT count(*) FROM llm_calls WHERE status = 'pending'") == 0
    # excerpts in the database are the filing's words
    assert pg._one("SELECT assumption_excerpt FROM statements WHERE empty_block IS NOT TRUE") == DOCS[0][3][1]


def real_plan(world):
    config = ["--config", str(world / "config.yaml"), "--company", COMPANY.ticker]

    def make(kind, row, folder, config_path, *, sync, wait_minutes, guard, db):
        return job.Plan([job.Stage("03_structure", "pipeline.03_structure", config + ["--submit"]),
                         job.Stage("04_outcomes", "pipeline.04_outcomes", config + ["--submit"])])
    return make


def test_a_job_runs_the_real_stages_against_the_database_and_records_every_call(world, pg, monkeypatch) -> None:
    pg.upsert_company(COMPANY.cik, ticker=COMPANY.ticker, name=COMPANY.name, status="requested")
    monkeypatch.setenv("STORE", "files")  # a job is on the database whatever STORE says
    root = point_at(world, monkeypatch, "job")
    with store.use(pg):
        cand.build_company(COMPANY, common.load_config(world / "config.yaml")["candidates"], out_dir=root / "candidates")
    result = job.run_job(COMPANY.cik, "build", db=pg, caps=dict(job.CAP_DEFAULTS), stages=real_plan(world), jobs_dir=world / "jobs", echo=False)
    assert result.status == "succeeded", result.error
    calls = pg._all("SELECT step, count(*), bool_and(job_id = %s), bool_and(usd > 0), bool_and(response_text IS NOT NULL), bool_and(fingerprint IS NOT NULL) "
                    "FROM llm_calls WHERE kind <> 'count' GROUP BY step ORDER BY step", (result.job_id,))
    assert [c[0] for c in calls] == ["03_structure", "04_acknowledgements", "04_outcomes"] and all(all(c[2:]) for c in calls)
    assert result.usd_spent == pytest.approx(float(pg._one("SELECT sum(usd) FROM llm_calls"))) and result.usd_spent > 0
    assert pg._one("SELECT count(*) FROM statements WHERE draft IS NOT NULL AND outcome_found IS NOT NULL") == 1
    assert pg._one("SELECT outcome_found->'outcome'->>'reported_value' FROM statements WHERE outcome_found IS NOT NULL") == "4.8"
    assert not (world / "job").exists() and pg.company(COMPANY.cik)["status"] == "built"
    log = result.log_path.read_text()
    assert "== 03_structure" in log and "== 04_outcomes" in log and "exit 0" in log


@pytest.mark.parametrize("cap", ["JOB_CAP_USD", "DAILY_CAP_USD", "WORKSPACE_CAP_USD"])
def test_each_cap_stops_the_real_stages_before_anything_is_sent(world, pg, monkeypatch, cap) -> None:
    pg.upsert_company(COMPANY.cik, ticker=COMPANY.ticker, name=COMPANY.name, status="requested")
    root = point_at(world, monkeypatch, "job")
    client = fakes.FakeAnthropic(respond=respond)
    monkeypatch.setattr(llm, "make_client", lambda: client)
    with store.use(pg):
        cand.build_company(COMPANY, common.load_config(world / "config.yaml")["candidates"], out_dir=root / "candidates")
    caps = {**job.CAP_DEFAULTS, cap: 0.0001}  # less than one request could cost
    result = job.run_job(COMPANY.cik, "build", db=pg, caps=caps, stages=real_plan(world), jobs_dir=world / "jobs", echo=False)
    assert result.status == "parked" and result.error.startswith(f"{cap}: 03_structure: worst-case") and "Nothing was sent" in result.error
    assert client.messages.create_calls == [] and client.messages.batches.created == []
    assert result.usd_spent == 0 and pg._one("SELECT count(*) FROM llm_calls WHERE kind <> 'count'") == 0
    assert pg._one("SELECT count(*) FROM statements") == 0 and pg.company(COMPANY.cik)["status"] == "requested"


def test_a_build_job_with_its_real_stage_list_ends_with_the_review_rows_queued(world, pg, monkeypatch) -> None:
    pg.upsert_company(COMPANY.cik, ticker=COMPANY.ticker, name=COMPANY.name, status="requested")
    root = point_at(world, monkeypatch, "job")
    with store.use(pg):
        cand.build_company(COMPANY, common.load_config(world / "config.yaml")["candidates"], out_dir=root / "candidates")

    def all_but_the_fetch(kind, row, folder, config_path, *, sync, wait_minutes, guard, db):
        plan = job.plan_stages(kind, row, folder, config_path, sync=sync, wait_minutes=wait_minutes)
        assert [s.name for s in plan.stages] == ["01_fetch", "02_candidates", "03_structure", "05_review", "03b_verify", "04_outcomes", "05_review_attach"]
        return job.Plan(plan.stages[2:])  # 01 and 02 need EDGAR; the candidates are already in

    result = job.run_job(COMPANY.cik, "build", db=pg, caps=dict(job.CAP_DEFAULTS), stages=all_but_the_fetch, jobs_dir=world / "jobs", echo=False)
    assert result.status == "succeeded" and result.stage == "05_review_attach", result.error
    with store.use(pg):
        rows = review.read_csv(root / "review" / f"{COMPANY.cik}.csv")
    assert sorted(r["empty_block"] for r in rows) == ["false", "true"]  # the draft, and the flag for the outlook block
    row = next(r for r in rows if r["empty_block"] == "false")
    assert row["outcome.reported_value"] == "4.8" and row["acknowledged_at"] == "2024-08-01" and row["approved"] == "false"
    # 05 queued the draft before 03b ran, so the verifier saw it; 04 came after, and the second 05 put its findings on the row
    assert row["aid_verify"] == "yes" and row["aid_verify_reason"] and row["aid_proposed_status"] == "missed"
    assert pg._all("SELECT count(*), bool_and(job_id = %s AND usd > 0) FROM llm_calls WHERE step = '03b_verify'", (result.job_id,)) == [(1, True)]
    flag = next(r for r in rows if r["empty_block"] == "true")
    assert flag["aid_verify"] == ""  # an outlook-block flag is not a statement: nothing to verify
    assert pg._one("SELECT count(*) FROM statements WHERE queue_seq IS NOT NULL") == 2
    log = result.log_path.read_text()
    order = [log.index(f"== {name}\n") for name in ("03_structure", "05_review", "03b_verify", "04_outcomes", "05_review_attach")]
    assert order == sorted(order)  # in that order
