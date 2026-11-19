"""pipeline/job.py: the three caps, parking, serial execution and the dry run, with stand-in stages and a stand-in pricer.

No stage here touches EDGAR or a model. A stand-in stage "sends" requests the way pipeline.llm does: it asks the guard
before each one with the worst case, records the call in llm_calls, and tells the guard what it cost.
"""

from __future__ import annotations

import importlib
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import fakes
from pipeline import common, job, llm
from research_record import store

pytestmark = pytest.mark.postgres

CIK, OTHER = "0000000123", "0000000456"
CAPS = {"JOB_CAP_USD": 1.50, "DAILY_CAP_USD": 8.00, "WORKSPACE_CAP_USD": 70.00}


@pytest.fixture
def db(pg):
    pg.upsert_company(CIK, ticker="EXMP", name="Example Corporation", status="requested", fiscal_year_end_month=12)
    pg.upsert_company(OTHER, ticker="OTHR", name="Other Corp", status="requested")
    return pg


class Sends(job.Stage):
    """A stage that sends `calls` requests, each priced by `price(n)` -> (worst case, actual)."""

    def __init__(self, name, guard, db, calls=1, price=lambda n: (0.10, 0.05), log=None, code=0):
        super().__init__(name, "none", [])
        self.guard, self.db, self.calls, self.price, self.log, self.code = guard, db, calls, price, log if log is not None else [], code

    def run(self) -> int:
        for n in range(self.calls):
            worst, actual = self.price(n)
            try:
                self.guard.check(worst, self.name)
            except llm.BudgetExceeded as exc:
                print(f"STOPPED: {exc}")
                return 4  # what a real stage returns when its cost gate stops it
            self.db.record_call(step=self.name, custom_id=f"{self.name}-{n}", usd=actual)
            self.guard.add(actual)
            self.log.append((self.name, n))
        return self.code


def plan_of(*specs, sent=None):
    """A stand-in for job.plan_stages: one Sends stage per (name, calls, price)."""
    def make(kind, row, folder, config_path, *, sync, wait_minutes, guard, db):
        return job.Plan([Sends(name, guard, db, calls, price, sent) for name, calls, price in specs])
    return make


def run(db, tmp_path, stages, cik=CIK, caps=CAPS, **kwargs):
    return job.run_job(cik, "build", db=db, caps=caps, stages=stages, jobs_dir=tmp_path / "jobs", echo=False, **kwargs)


# --- caps -------------------------------------------------------------------------------------------------------------


def test_the_caps_come_from_the_environment_with_the_stated_defaults() -> None:
    assert job.caps_from_env({}) == {"JOB_CAP_USD": 1.50, "DAILY_CAP_USD": 8.00, "WORKSPACE_CAP_USD": 70.00}
    assert job.caps_from_env({"JOB_CAP_USD": "0.25", "DAILY_CAP_USD": "", "WORKSPACE_CAP_USD": "12"}) == {
        "JOB_CAP_USD": 0.25, "DAILY_CAP_USD": 8.00, "WORKSPACE_CAP_USD": 12.0}
    with pytest.raises(job.JobError, match="DAILY_CAP_USD"):
        job.caps_from_env({"DAILY_CAP_USD": "eight"})


def test_a_job_inside_every_cap_succeeds_and_every_call_is_recorded_against_it(db, tmp_path) -> None:
    sent = []
    result = run(db, tmp_path, plan_of(("03_structure", 3, lambda n: (0.10, 0.04)), ("04_outcomes", 2, lambda n: (0.05, 0.01)), sent=sent))
    assert result.status == "succeeded" and result.error is None and len(sent) == 5
    assert result.usd_spent == pytest.approx(0.14)
    row = db.job(result.job_id)
    assert (row["status"], row["kind"], row["stage"]) == ("succeeded", "build", "04_outcomes") and row["finished_at"] is not None
    assert db._all("SELECT count(*), sum(usd)::float8 FROM llm_calls WHERE job_id = %s AND cik = %s", (result.job_id, CIK)) == [(5, pytest.approx(0.14))]
    company = db.company(CIK)
    assert company["status"] == "built" and company["first_built_at"] is not None and company["last_refreshed_at"] is not None
    assert "succeeded" in result.log_path.read_text() and row["log_path"].endswith("job.log")


def test_the_job_cap_parks_the_job_before_the_request_that_would_pass_it(db, tmp_path) -> None:
    sent = []
    # each request could cost 0.40 and does cost 0.40: three fit under 1.50 (1.20), the fourth would make 1.60
    result = run(db, tmp_path, plan_of(("03_structure", 10, lambda n: (0.40, 0.40)), ("04_outcomes", 1, lambda n: (0.01, 0.01)), sent=sent))
    assert result.status == "parked"
    assert result.error.startswith("JOB_CAP_USD:") and "$1.50" in result.error and "$1.2000 already spent by this job" in result.error
    assert "Nothing was sent" in result.error
    assert len(sent) == 3 and result.usd_spent == pytest.approx(1.20) and result.stage == "03_structure"  # 04 never ran
    assert db.job(result.job_id)["status"] == "parked"
    assert db.company(CIK)["status"] == "requested"  # the company is what it was: a cap parks the job, not the company


def test_the_daily_cap_counts_every_job_started_today(db, tmp_path) -> None:
    caps = {**CAPS, "DAILY_CAP_USD": 2.00}
    first = run(db, tmp_path, plan_of(("03_structure", 3, lambda n: (0.40, 0.40))), caps=caps)
    assert first.status == "succeeded" and first.usd_spent == pytest.approx(1.20)
    sent = []
    second = run(db, tmp_path, plan_of(("03_structure", 5, lambda n: (0.40, 0.40)), sent=sent), cik=OTHER, caps=caps)
    assert second.status == "parked" and second.error.startswith("DAILY_CAP_USD:")
    assert "$2.00" in second.error and "$2.0000 already spent by the jobs started today" in second.error
    assert len(sent) == 2 and second.usd_spent == pytest.approx(0.80)  # 1.20 + 0.80 is the cap; the third would pass it
    # a job that started yesterday is not today's spend
    db.conn.execute("UPDATE jobs SET started_at = started_at - interval '1 day' WHERE id = %s", (first.job_id,))
    third = run(db, tmp_path, plan_of(("03_structure", 2, lambda n: (0.40, 0.40))), cik=OTHER, caps=caps)
    assert third.status == "succeeded"


def test_the_workspace_cap_counts_every_model_call_of_the_calendar_month_job_or_not(db, tmp_path) -> None:
    caps = {**CAPS, "WORKSPACE_CAP_USD": 5.00}
    now = datetime.now(timezone.utc)
    db.record_call(step="03b_verify", custom_id="v-by-hand", usd=4.50)  # this month, outside any job
    db.record_call(step="03b_verify", custom_id="v-last-month", usd=60.0, created_at=now.replace(day=1) - timedelta(days=2))
    sent = []
    result = run(db, tmp_path, plan_of(("03_structure", 5, lambda n: (0.20, 0.20)), sent=sent), caps=caps)
    assert result.status == "parked" and result.error.startswith("WORKSPACE_CAP_USD:")
    assert "$5.00" in result.error and "$4.9000 already spent on model calls in" in result.error and now.strftime("%B %Y") in result.error
    assert len(sent) == 2 and result.usd_spent == pytest.approx(0.40)


def test_a_cap_already_used_up_parks_the_job_before_any_stage_runs(db, tmp_path) -> None:
    db.record_call(step="x", custom_id="x-1", usd=70.0)
    sent = []
    result = run(db, tmp_path, plan_of(("03_structure", 1, lambda n: (0.01, 0.01)), sent=sent))
    assert result.status == "parked" and result.error.startswith("WORKSPACE_CAP_USD:") and sent == [] and result.stage is None


def test_the_worst_case_is_what_is_checked_not_the_expected_cost(db, tmp_path) -> None:
    sent = []
    # it would only cost a cent, but it COULD cost 2.00: it is not sent
    result = run(db, tmp_path, plan_of(("03_structure", 1, lambda n: (2.00, 0.01)), sent=sent))
    assert result.status == "parked" and result.error.startswith("JOB_CAP_USD:") and "worst-case $2.0000" in result.error and sent == []


def test_the_real_cost_gate_in_pipeline_llm_parks_a_job_and_records_what_was_spent(db, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(llm, "BATCH_DIR", tmp_path / "batches")
    schema = {"type": "object", "properties": {"items": {"type": "array"}}, "required": ["items"], "additionalProperties": False}
    cfg = llm.LlmConfig(model="claude-haiku-4-5", budget_usd=100.0, price_input=1.0, price_output=5.0, batch_discount=0.5)
    client = fakes.FakeAnthropic(input_tokens=100_000, output_tokens=1_000)  # 0.105 a request, one by one

    class Real(job.Stage):
        def __init__(self, name, n):
            super().__init__(name, "none", [])
            self.n = n

        def run(self) -> int:
            requests = [llm.LlmRequest(f"{self.name}-{i}", "s", f"u {i}", 1000, schema) for i in range(self.n)]
            projection = llm.project(requests, [100_000] * self.n, exact=True, llm=cfg, expected_output_per_request=1000)
            try:
                llm.run_batch(client, cfg, self.name, requests, projection=projection, canary_check=lambda t: None, force_sync=True, log=lambda s: None)
            except llm.BudgetExceeded as exc:
                print(f"STOPPED: {exc}")
                return 4
            return 0

    def stages(kind, row, folder, config_path, *, sync, wait_minutes, guard, db):
        return job.Plan([Real("03_structure", 8), Real("04_outcomes", 8)])

    result = run(db, tmp_path, stages)
    # 03: 8 requests, worst case 8 x 0.105 = 0.84, spent 0.84. 04: another 0.84 on top would be 1.68 > 1.50: parked, unsent.
    assert result.status == "parked" and result.error.startswith("JOB_CAP_USD: 04_outcomes:") and result.stage == "04_outcomes"
    assert result.usd_spent == pytest.approx(0.84) and len(client.messages.create_calls) == 8
    assert db._all("SELECT step, count(*), sum(usd)::float8 FROM llm_calls WHERE job_id = %s GROUP BY step", (result.job_id,)) == [("03_structure", 8, pytest.approx(0.84))]
    assert llm.GUARD is None  # the guard does not outlive the job


def test_the_lifetime_budget_in_config_still_stops_a_job_and_says_so(db, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(llm, "BATCH_DIR", tmp_path / "batches")
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    cfg = llm.LlmConfig(model="m", budget_usd=0.05, price_input=1.0, price_output=5.0, batch_discount=0.5)

    class Real(job.Stage):
        def run(self) -> int:
            requests = [llm.LlmRequest("r-0", "s", "u", 1000, schema)]
            projection = llm.project(requests, [100_000], exact=True, llm=cfg, expected_output_per_request=1000)
            try:
                llm.run_batch(fakes.FakeAnthropic(), cfg, "03_structure", requests, projection=projection, canary_check=lambda t: None, force_sync=True)
            except llm.BudgetExceeded:
                return 4
            return 0

    result = run(db, tmp_path, lambda *a, **k: job.Plan([Real("03_structure", "none", [])]))
    assert result.status == "parked" and result.error.startswith("llm.budget_usd in config.yaml:") and result.usd_spent == 0


# --- failing, parking, waiting ------------------------------------------------------------------------------------------


def failing(code=1):
    def make(kind, row, folder, config_path, *, sync, wait_minutes, guard, db):
        return job.Plan([Sends("01_fetch", guard, db, calls=0, code=code), Sends("02_candidates", guard, db, calls=0)])
    return make


def test_a_stage_that_exits_non_zero_fails_the_job_and_stops_it_there(db, tmp_path) -> None:
    result = run(db, tmp_path, failing())
    assert result.status == "failed" and result.stage == "01_fetch" and "01_fetch exited 1" in result.error
    assert db.company(CIK)["status"] == "requested"  # one failure does not park it


def test_a_company_that_fails_twice_is_parked_and_is_not_run_again_unasked(db, tmp_path) -> None:
    assert run(db, tmp_path, failing()).status == "failed"
    second = run(db, tmp_path, failing(code=3))
    assert second.status == "failed" and "second failed job in a row" in second.error
    assert db.company(CIK)["status"] == "parked"
    with pytest.raises(job.JobError, match="parked"):
        run(db, tmp_path, plan_of(("03_structure", 1, lambda n: (0.01, 0.01))))
    assert len(db.jobs(CIK)) == 2  # the refused run made no job
    again = run(db, tmp_path, plan_of(("03_structure", 1, lambda n: (0.01, 0.01))), retry_parked=True)
    assert again.status == "succeeded" and db.company(CIK)["status"] == "built"


def test_a_success_between_two_failures_does_not_park(db, tmp_path) -> None:
    assert run(db, tmp_path, failing()).status == "failed"
    assert run(db, tmp_path, plan_of(("03_structure", 1, lambda n: (0.01, 0.01)))).status == "succeeded"
    assert run(db, tmp_path, failing(), force=True).status == "failed"
    assert db.company(CIK)["status"] == "built"


def test_a_stage_that_raises_fails_the_job_with_the_error_on_record(db, tmp_path) -> None:
    class Boom(job.Stage):
        def run(self) -> int:
            raise RuntimeError("EDGAR said no")

    result = run(db, tmp_path, lambda *a, **k: job.Plan([Boom("01_fetch", "none", [])]))
    assert result.status == "failed" and "RuntimeError: EDGAR said no" in result.error
    assert "Traceback" in result.log_path.read_text()


def test_a_batch_still_processing_leaves_the_job_waiting_and_the_next_run_resumes_it(db, tmp_path) -> None:
    def waiting(kind, row, folder, config_path, *, sync, wait_minutes, guard, db):
        return job.Plan([Sends("03_structure", guard, db, calls=1, price=lambda n: (0.30, 0.30), code=job.EXIT_BATCH_PENDING)])

    first = run(db, tmp_path, waiting)
    assert first.status == "waiting" and db.job(first.job_id)["finished_at"] is None and db.company(CIK)["status"] == "building"
    second = run(db, tmp_path, plan_of(("03_structure", 1, lambda n: (0.30, 0.30))))
    assert second.job_id == first.job_id and second.status == "succeeded"
    assert second.usd_spent == pytest.approx(0.60)  # the cap counts what the job spent before it waited
    assert len(db.jobs(CIK)) == 1


def test_an_unknown_company_or_kind_is_refused_before_anything_is_recorded(db, tmp_path) -> None:
    with pytest.raises(job.JobError, match="not in the companies table"):
        run(db, tmp_path, failing(), cik="0000009999")
    with pytest.raises(job.JobError, match="build or refresh"):
        job.run_job(CIK, "rebuild", db=db, caps=CAPS, stages=failing(), jobs_dir=tmp_path)
    with pytest.raises(job.JobError, match="has not been built"):
        job.run_job(CIK, "refresh", db=db, caps=CAPS, jobs_dir=tmp_path)
    assert db._one("SELECT count(*) FROM jobs") == 0


# --- one at a time ------------------------------------------------------------------------------------------------------


def test_jobs_for_different_companies_run_one_after_the_other(db, tmp_path) -> None:
    spans: dict[str, tuple[float, float]] = {}
    errors: list[BaseException] = []

    class Slow(job.Stage):
        def run(self) -> int:
            started = time.monotonic()
            time.sleep(0.4)
            spans[self.name] = (started, time.monotonic())
            return 0

    def worker(cik: str) -> None:
        mine = store.PostgresBackend(db.url, db.schema)  # a process of its own would have a connection of its own
        try:
            result = job.run_job(cik, "build", db=mine, caps=CAPS, jobs_dir=tmp_path / "jobs", echo=False,
                                 stages=lambda *a, **k: job.Plan([Slow(cik, "none", [])]))
            assert result.status == "succeeded"
        except BaseException as exc:  # noqa: BLE001 - reported on the main thread
            errors.append(exc)
        finally:
            mine.close()

    threads = [threading.Thread(target=worker, args=(cik,)) for cik in (CIK, OTHER)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors and set(spans) == {CIK, OTHER}
    (a_start, a_end), (b_start, b_end) = sorted(spans.values())
    assert b_start >= a_end  # the second did not start until the first had finished
    rows = db._all("SELECT started_at, finished_at FROM jobs ORDER BY started_at")
    assert rows[1][0] >= rows[0][1]  # and the jobs table says the same


# --- the job's config and stage list ------------------------------------------------------------------------------------


def test_the_job_config_is_config_yaml_with_only_the_company_replaced(db, tmp_path) -> None:
    row = db.company(CIK)
    path = job.write_job_config(row, tmp_path)
    mine, base = common.load_config(path), common.load_config()
    assert [c.cik for c in common.companies(mine)] == [CIK] and common.companies(mine)[0].fiscal_year_end_month == 12
    assert {k: v for k, v in mine.items() if k != "companies"} == {k: v for k, v in base.items() if k != "companies"}
    assert mine["edgar"]["user_agent"] == common.EXPECTED_USER_AGENT and mine["edgar"]["max_rps"] == base["edgar"]["max_rps"] <= 10


def test_a_build_runs_01_to_04_and_a_refresh_runs_them_on_the_new_filings_only(db, tmp_path) -> None:
    row = db.company(CIK)
    build = job.plan_stages("build", row, tmp_path, tmp_path / "config.yaml", sync=False, wait_minutes=60)
    assert [(s.name, s.module) for s in build.stages] == [
        ("01_fetch", "pipeline.01_fetch"), ("02_candidates", "pipeline.02_candidates"), ("03_structure", "pipeline.03_structure"),
        ("05_review", "pipeline.05_review"), ("03b_verify", "pipeline.03b_verify"), ("04_outcomes", "pipeline.04_outcomes"),
        ("05_review_attach", "pipeline.05_review")]
    base = ["--config", str(tmp_path / "config.yaml"), "--company", "EXMP"]
    assert all(s.argv[:4] == base for s in build.stages)
    assert all("--submit" in build.stages[i].argv and "--sync" not in build.stages[i].argv for i in (2, 4, 5))
    assert build.stages[3].argv == base and build.stages[6].argv == base + ["--attach-outcomes"]  # 05 sends nothing: no --submit
    one_by_one = job.plan_stages("build", row, tmp_path, tmp_path / "config.yaml", sync=True, wait_minutes=60)
    assert all("--sync" in one_by_one.stages[i].argv for i in (2, 4, 5)) and not any("--sync" in one_by_one.stages[i].argv for i in (3, 6))

    db.upsert_company(CIK, last_filing_date=date(2026, 9, 28), accessions_on_last_date=["0000000123-26-000182"],
                      last_refreshed_at=datetime(2026, 10, 28, tzinfo=timezone.utc))
    refresh = job.plan_stages("refresh", db.company(CIK), tmp_path, tmp_path / "config.yaml", sync=False, wait_minutes=60)
    manifest = importlib.import_module("pipeline.manifest").load_manifest(tmp_path / "manifest.json")
    assert manifest.companies[CIK].last_filing_date == date(2026, 9, 28) and manifest.companies[CIK].accessions_on_last_date == ("0000000123-26-000182",)
    assert [s.name for s in refresh.stages] == ["01_fetch", "02_candidates", "03_structure", "04_outcomes"]  # a refresh is as it was
    assert "--since-manifest" in refresh.stages[0].argv and str(tmp_path / "new_filings.json") in refresh.stages[0].argv
    assert all("--accessions" in s.argv for s in refresh.stages[1:]) and "--from-review" in refresh.stages[3].argv
    (tmp_path / "new_filings.json").write_text('{"filings": [{"cik": "0000000123", "accession": "a", "path": null}]}')
    assert refresh.after_fetch() is False  # an 8-K with no exhibit is nothing to draft from


# --- the dry run --------------------------------------------------------------------------------------------------------


def candidate(char_start: int, sentence: str) -> dict:
    return {"cik": CIK, "accession": "0000000123-24-000001", "filing_type": "8-K", "filed_at": "2024-02-01", "char_start": char_start,
            "char_end": char_start + len(sentence), "sentence": sentence, "text_version": 1, "capture_method": "sentence", "heading": None,
            "lead_in": None, "context_before": [], "context_after": [], "block_lines": None, "block_end": None, "table_header": None}


def test_a_dry_run_prices_from_the_fingerprints_and_calls_nothing(db, tmp_path, monkeypatch) -> None:
    structure = importlib.import_module("pipeline.03_structure")
    monkeypatch.setattr(common, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(llm, "make_client", lambda: (_ for _ in ()).throw(AssertionError("a dry run must not make a client")))
    with store.use(db):
        store.write_jsonl(common.CANDIDATES_DIR / f"{CIK}.jsonl", [
            candidate(10, "For fiscal 2025 the Company expects revenue of $5.0 billion to $6.0 billion."),
            candidate(200, "We expect first quarter 2025 revenue of approximately $1.2 billion.")])
    before = db._one("SELECT count(*) FROM llm_calls"), db._one("SELECT count(*) FROM jobs")

    prices, lines = job.dry_run(CIK, "build", db=db, caps=CAPS, jobs_dir=tmp_path / "jobs")
    first = prices[0]
    assert (first.step, first.requests, first.answered, first.counted, first.estimated) == ("03_structure", 2, 0, 0, 2)
    assert 0 < first.usd_expected < first.usd_worst
    assert [p.step for p in prices[1:]] == ["03b_verify", "04_outcomes", "04_acknowledgements", "04_withdrawals"]
    assert prices[1].note.startswith("depends") and all(p.note.startswith("cannot be priced") for p in prices[2:])
    assert any("within the cap" in line for line in lines) and any("estimated" in line for line in lines)

    # a counted request is priced at its count; an answered one is free
    config = common.load_config(tmp_path / "jobs" / f"dry-{CIK}" / "config.yaml")
    cfg = llm.LlmConfig.from_config(config["llm"])
    with store.use(db):
        requests, _, _ = structure.build_requests(common.companies(config), {"8-K"}, metrics=config["metrics"])
    db.token_cache_write(Path("token_cache.json"), {requests[0].fingerprint(cfg.model): 4000}, model=cfg.model)
    db.conn.execute("DELETE FROM llm_calls WHERE kind <> 'count'")
    db.record_call(step="03_structure", custom_id=requests[1].custom_id, usd=0.0123, fingerprint=requests[1].fingerprint(cfg.model),
                   text='{"items": []}', kind="batch", input_tokens=3000, output_tokens=10)
    prices, lines = job.dry_run(CIK, "build", db=db, caps=CAPS, jobs_dir=tmp_path / "jobs")
    first = prices[0]
    assert (first.requests, first.answered, first.counted, first.estimated, first.input_tokens) == (2, 1, 1, 0, 4000)
    assert first.usd_expected == pytest.approx(cfg.usd(4000, structure.EXPECTED_OUTPUT_TOKENS, batch=False))  # one request: sent one by one
    assert first.usd_worst == pytest.approx(cfg.usd(4000, requests[0].max_tokens, batch=False))
    assert first.usd_already_paid == pytest.approx(0.0123)
    assert db._one("SELECT count(*) FROM jobs") == before[1] == 0  # a dry run is not a job

    # and a worst case over a cap is called out
    _, lines = job.dry_run(CIK, "build", db=db, caps={**CAPS, "JOB_CAP_USD": 0.001}, jobs_dir=tmp_path / "jobs")
    assert any(line.strip().startswith("JOB_CAP_USD") and "WOULD PARK" in line for line in lines)


def test_a_dry_run_with_nothing_to_price_says_how_to_get_there(db, tmp_path) -> None:
    with pytest.raises(job.JobError, match="no candidates in the database"):
        job.dry_run(CIK, "build", db=db, caps=CAPS, jobs_dir=tmp_path)
    with pytest.raises(job.JobError, match="--fetch"):
        job.dry_run(CIK, "refresh", db=db, caps=CAPS, jobs_dir=tmp_path)


# --- where the next refresh starts ----------------------------------------------------------------------------------------


def refresh_plan(tmp_path, filings, complete=True):
    """A stand-in refresh whose 01 "found" these filings."""
    import json

    def make(kind, row, folder, config_path, *, sync, wait_minutes, guard, db):
        new_filings = tmp_path / "new_filings.json"
        new_filings.write_text(json.dumps({"run_date": "2026-11-19", "complete": complete, "companies": [CIK], "filings": filings}))
        return job.Plan([Sends("01_fetch", guard, db, calls=0)], after_fetch=lambda: False, new_filings=new_filings)
    return make


def filing(accession, filed, status="fetched"):
    return {"cik": CIK, "accession": accession, "form": "8-K", "filing_date": filed, "path": f"data/raw/{CIK}/{accession}.html", "status": status}


def test_a_refresh_that_succeeds_moves_the_watermark_by_the_manifests_own_rules(db, tmp_path) -> None:
    db.upsert_company(CIK, status="built", last_filing_date=date(2026, 9, 28), accessions_on_last_date=["0000000123-26-000182"])
    plan = refresh_plan(tmp_path, [filing("0000000123-26-000190", "2026-11-02"), filing("0000000123-26-000191", "2026-11-02"),
                                   filing("0000000123-26-000185", "2026-10-10")])
    result = job.run_job(CIK, "refresh", db=db, caps=CAPS, stages=plan, jobs_dir=tmp_path / "jobs", echo=False)
    company = db.company(CIK)
    assert result.status == "succeeded" and company["status"] == "built"
    assert company["last_filing_date"] == date(2026, 11, 2)
    assert company["accessions_on_last_date"] == ["0000000123-26-000190", "0000000123-26-000191"]
    assert company["last_refreshed_at"] is not None


def test_a_filing_that_did_not_fetch_cleanly_holds_the_watermark_where_it_was(db, tmp_path) -> None:
    db.upsert_company(CIK, status="built", last_filing_date=date(2026, 9, 28), accessions_on_last_date=["0000000123-26-000182"])
    plan = refresh_plan(tmp_path, [filing("0000000123-26-000190", "2026-11-02", status="failed")])
    result = job.run_job(CIK, "refresh", db=db, caps=CAPS, stages=plan, jobs_dir=tmp_path / "jobs", echo=False)
    company = db.company(CIK)
    assert result.status == "succeeded" and company["status"] == "built"
    assert company["last_filing_date"] == date(2026, 9, 28) and company["accessions_on_last_date"] == ["0000000123-26-000182"]
    assert company["last_refreshed_at"] is None  # so the filing is looked for again next time


def test_a_build_that_succeeds_starts_the_watermark_from_the_cached_filings(db, tmp_path, monkeypatch) -> None:
    import json

    raw = tmp_path / "raw" / CIK
    raw.mkdir(parents=True)
    for accession, filed in [("0000000123-24-000001", "2024-02-01"), ("0000000123-24-000009", "2024-05-01"), ("0000000123-24-000010", "2024-05-01")]:
        (raw / f"{accession}.meta.json").write_text(json.dumps({"accession": accession, "filed_at": filed, "http_status": 200}))
    monkeypatch.setattr(common, "RAW_DIR", tmp_path / "raw")
    result = run(db, tmp_path, plan_of(("03_structure", 1, lambda n: (0.01, 0.01))))
    company = db.company(CIK)
    assert result.status == "succeeded" and company["last_filing_date"] == date(2024, 5, 1)
    assert company["accessions_on_last_date"] == ["0000000123-24-000009", "0000000123-24-000010"]


# --- a company that is already built ------------------------------------------------------------------------------------


def test_a_build_of_a_built_company_is_refused_unless_forced(db, tmp_path) -> None:
    db.upsert_company(CIK, status="built")
    sent = []
    with pytest.raises(job.JobError, match="already built") as refused:
        run(db, tmp_path, plan_of(("03_structure", 1, lambda n: (0.01, 0.01)), sent=sent))
    assert "--force" in str(refused.value) and "--kind refresh" in str(refused.value)
    assert sent == [] and db._one("SELECT count(*) FROM jobs") == 0 and db.company(CIK)["status"] == "built"
    forced = run(db, tmp_path, plan_of(("03_structure", 1, lambda n: (0.01, 0.01)), sent=sent), force=True)
    assert forced.status == "succeeded" and len(sent) == 1


def test_a_refresh_of_a_built_company_needs_no_force(db, tmp_path) -> None:
    db.upsert_company(CIK, status="built", last_filing_date=date(2026, 9, 28), accessions_on_last_date=[])
    result = job.run_job(CIK, "refresh", db=db, caps=CAPS, stages=refresh_plan(tmp_path, []), jobs_dir=tmp_path / "jobs", echo=False)
    assert result.status == "succeeded"


def test_the_command_line_prints_the_reason_and_exits_2(db, tmp_path, monkeypatch, capsys) -> None:
    db.upsert_company(CIK, status="built")
    monkeypatch.setenv("DATABASE_URL", db.url)
    monkeypatch.setenv("STORE_SCHEMA", db.schema)
    monkeypatch.setattr(job, "JOBS_DIR", tmp_path / "jobs")
    assert job.main(["run", "--cik", CIK, "--kind", "build"]) == 2
    err = capsys.readouterr().err
    assert "ERROR: EXMP is already built" in err and "--force" in err
    assert db._one("SELECT count(*) FROM jobs") == 0
