"""pipeline/worker.py: the queue, the caps, parking, the lock and shutdown.

The build is pipeline.job.run_job with stand-in stages (as in test_pg_job.py), so the job rows, the cost accounting and
the two-failures rule are the real ones. No stage here touches EDGAR or a model, the SEC lookup is a stand-in, and the
email goes to a stand-in for requests.post.
"""

from __future__ import annotations

import logging
import os
import signal
from datetime import datetime, timedelta, timezone

import pytest

import pilot_rows
from pipeline import common, job, worker

pytestmark = pytest.mark.postgres

CIK, OTHER, THIRD = "0000000123", "0000000456", "0000000789"
CAPS = {"JOB_CAP_USD": 1.50, "DAILY_CAP_USD": 8.00, "WORKSPACE_CAP_USD": 70.00}
T0 = datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc)


class Step(job.Stage):
    """A stand-in stage: records that it ran, optionally spends, writes the company's statements, or exits non-zero."""

    def __init__(self, name, ran, *, guard=None, db=None, cik=None, usd=0.0, code=0, rows=False, then=None):
        super().__init__(name, "none", [])
        self.ran, self.guard, self.db, self.cik, self.usd, self.code, self.rows, self.then = ran, guard, db, cik, usd, code, rows, then

    def run(self) -> int:
        self.ran.append((self.cik, self.name))
        if self.usd:
            self.guard.check(self.usd, self.name)
            self.db.record_call(step=self.name, custom_id=f"{self.name}-{self.cik}", usd=self.usd)
            self.guard.add(self.usd)
        if self.rows and not self.db.email_counts(self.cik)["n_statements"]:
            pilot_rows.four_statements(self.db, self.cik, tier=None)  # as a build leaves them: queued, no tier yet
        if self.then:
            self.then()
        return self.code


class Builds:
    """The worker's `build`: run_job with stand-in stages. `codes[cik]` is the exit code of each successive attempt's
    last stage (default 0); `usd` is what the first stage spends."""

    def __init__(self, db, tmp_path, *, codes=None, usd=0.05, between=None, rows=True):
        self.db, self.tmp_path, self.codes, self.usd, self.between, self.rows = db, tmp_path, codes or {}, usd, between, rows
        self.ran: list[tuple[str, str]] = []
        self.calls: list[str] = []

    def __call__(self, cik, job_id, should_stop):
        self.calls.append(cik)
        attempt = self.calls.count(cik) - 1
        todo = self.codes.get(cik, [])
        code = todo[attempt] if attempt < len(todo) else 0

        def stages(kind, row, folder, config_path, *, sync, wait_minutes, guard, db):
            return job.Plan([Step("03_structure", self.ran, guard=guard, db=db, cik=cik, usd=self.usd, then=self.between),
                             Step("05_review", self.ran, db=db, cik=cik, code=code, rows=self.rows and code == 0)])

        return job.run_job(cik, "build", db=self.db, caps=CAPS, stages=stages, jobs_dir=self.tmp_path / "jobs", echo=False,
                           job_id=job_id, should_stop=should_stop)


def derive_ok(cik, wanted):
    return worker.Derived(name=f"Company {cik[-3:]}", ticker=(wanted or f"T{cik[-3:]}").upper(), fiscal_year_end_month=12, fiscal_year_named_for="end")


@pytest.fixture
def db(pg, tmp_path, monkeypatch):
    monkeypatch.setattr(common, "RAW_DIR", tmp_path / "raw")  # a finished build looks here for the filings it cached
    return pg


def make(db, tmp_path, *, build=None, post=None, **kwargs):
    build = build if build is not None else Builds(db, tmp_path)
    post = post if post is not None else pilot_rows.FakePost()
    kwargs.setdefault("derive", derive_ok)
    kwargs.setdefault("settings", pilot_rows.settings())
    kwargs.setdefault("caps", CAPS)
    return worker.Worker(db, build=build, post=post, echo=False, poll_seconds=0.01, **kwargs), build, post


def ask(db, cik, email="reader@example.com", minutes=0, **fields):
    return db.create_request(cik, email, created_at=T0 + timedelta(minutes=minutes), **fields)


def status(db, rid):
    row = db.request(rid)
    return row["status"], row["reason"]


# --- the schema ---------------------------------------------------------------------------------------------------------


def test_one_address_has_one_request_in_flight_per_company_whatever_its_case(db) -> None:
    import psycopg

    first = ask(db, CIK)
    with pytest.raises(psycopg.errors.UniqueViolation):
        ask(db, CIK, email="Reader@Example.com")
    ask(db, OTHER)                      # another company: fine
    ask(db, CIK, email="b@example.com")  # another address: fine
    db.update_request(first, status="live")
    ask(db, CIK)                        # the first is no longer in flight
    row = db.request(first)
    assert (row["source"], row["notified_at"], row["job_id"], row["resend_id"]) == ("pilot", None, None, None)
    with pytest.raises(psycopg.errors.CheckViolation):
        ask(db, "123")                  # a cik is ten digits, as in companies
    indexes = " ".join(d for (d,) in db._all("SELECT indexdef FROM pg_indexes WHERE schemaname = %s AND tablename = 'company_requests'", (db.schema,)))
    assert "(cik, status)" in indexes


# --- the queue ----------------------------------------------------------------------------------------------------------


def test_the_oldest_open_request_is_built_first(db, tmp_path) -> None:
    newer = ask(db, CIK, minutes=10)
    older = ask(db, OTHER, minutes=0)  # asked first, inserted second
    w, build, post = make(db, tmp_path)
    w.tick()
    assert build.calls == [OTHER, CIK]
    assert status(db, older) == ("live", None) and status(db, newer) == ("live", None)
    for cik in (CIK, OTHER):
        company = db.company(cik)
        assert company["status"] == "built" and company["fiscal_year_end_month"] == 12 and company["tax_rate_parens_negative"] is False
        tiers = db._all("SELECT review_tier, count(*) FROM statements WHERE cik = %s AND queue_seq IS NOT NULL AND NOT COALESCE(empty_block, false) GROUP BY 1", (cik,))
        assert tiers == [("machine", 4)]  # the rows the build produced are machine tier; there is no tier column on companies
    row = db.request(older)
    assert db.job(row["job_id"])["status"] == "succeeded" and db.job(row["job_id"])["kind"] == "build"
    assert row["notified_at"] is not None and row["resend_id"] == "resend-1" and len(post.calls) == 2


def test_a_company_that_is_already_built_goes_live_at_once_and_is_emailed(db, tmp_path) -> None:
    pilot_rows.built_company(db, CIK, "EXMP", "Example Corporation")
    rid = ask(db, CIK, ticker="typed", name="Typed By The Visitor")
    w, _, post = make(db, tmp_path, build=pilot_rows.no_network, derive=pilot_rows.no_network)
    w.tick()
    row = db.request(rid)
    assert (row["status"], row["job_id"], row["resend_id"]) == ("live", None, "resend-1") and row["notified_at"] is not None
    assert db._one("SELECT count(*) FROM jobs") == 0
    (call,) = post.calls
    assert call["json"]["to"] == ["reader@example.com"] and call["json"]["subject"] == "EXMP is live on assumption-drift"
    assert call["json"]["text"].startswith("Example Corporation (EXMP) is built.\n\n4 forward-looking statements from 2 filings, 2024-02-01 to 2025-08-05.\n"
                                           "2 have a resolution; 2 are still open.\n\nhttps://pilot.scndorder.com/company/exmp\n")
    assert "You asked for this company on 2026-11-20." in call["json"]["text"] and "Typed" not in call["json"]["text"]
    w.tick()
    assert len(post.calls) == 1  # once


def test_a_second_request_for_a_company_that_is_building_is_a_duplicate_and_is_emailed_when_it_is_live(db, tmp_path) -> None:
    first = ask(db, CIK)
    build = Builds(db, tmp_path, codes={CIK: [job.EXIT_BATCH_PENDING, job.EXIT_BATCH_PENDING, 0]})
    w, _, post = make(db, tmp_path, build=build)
    w.tick()
    assert status(db, first) == ("building", None) and db.job(db.request(first)["job_id"])["status"] == "waiting"
    second = ask(db, CIK, email="second@example.com", minutes=5)
    elsewhere = ask(db, OTHER, email="third@example.com", minutes=6)
    w.tick()
    assert status(db, second) == ("duplicate", "already requested") and db.request(second)["job_id"] is None
    assert status(db, elsewhere) == ("open", None)  # one build at a time: it waits its turn, no second build is started
    assert post.calls == [] and len(db.jobs(CIK)) == 1 and db.jobs(OTHER) == []
    w.tick()
    assert status(db, first) == ("live", None) and status(db, second) == ("duplicate", "already requested")
    assert {c["json"]["to"][0] for c in post.calls} == {"reader@example.com", "second@example.com", "third@example.com"}
    assert all(db.request(r)["notified_at"] is not None and db.request(r)["resend_id"] for r in (first, second, elsewhere))
    assert len(db.jobs(CIK)) == 1 and status(db, elsewhere) == ("live", None)  # the same job, resumed; then the next company
    w.tick()
    assert len(post.calls) == 3


# --- caps ---------------------------------------------------------------------------------------------------------------


def test_a_daily_cap_with_no_room_queues_the_request_and_the_next_day_builds_it(db, tmp_path) -> None:
    day = [datetime.now(timezone.utc).date()]
    db.record_call(step="03_structure", custom_id="earlier-today", usd=7.00)  # 7.00 + a build's 1.50 is over 8.00
    rid = ask(db, CIK)
    w, build, post = make(db, tmp_path, today=lambda: day[0])
    w.tick()
    assert status(db, rid) == ("queued", "daily spending limit reached, resumes tomorrow")
    assert db._one("SELECT count(*) FROM jobs") == 0 and db.request(rid)["job_id"] is None and build.calls == [] and post.calls == []
    assert db.company(CIK) is None  # nothing was set up, and the SEC was not asked
    late = ask(db, CIK, email="late@example.com", minutes=3)
    w.tick()
    assert status(db, rid)[0] == "queued" and status(db, late) == ("duplicate", "already requested")
    day[0] += timedelta(days=1)
    w.tick()
    assert status(db, rid) == ("live", None) and build.calls == [CIK] and len(post.calls) == 2
    assert db.job(db.request(rid)["job_id"])["status"] == "succeeded"


def test_a_monthly_cap_with_no_room_queues_with_its_own_reason(db, tmp_path) -> None:
    db.record_call(step="03_structure", custom_id="this-month", usd=69.00)
    rid = ask(db, CIK)
    w, build, _ = make(db, tmp_path)
    w.tick()
    assert status(db, rid) == ("queued", "monthly spending limit reached") and db._one("SELECT count(*) FROM jobs") == 0
    db.conn.execute("UPDATE llm_calls SET created_at = created_at - interval '40 days'")
    w.tick()
    assert status(db, rid) == ("live", None)


def test_a_build_the_daily_cap_stops_part_way_is_queued_not_parked(db, tmp_path) -> None:
    rid = ask(db, CIK)
    caps = {**CAPS, "DAILY_CAP_USD": 2.00}

    def build(cik, job_id, should_stop):
        def stages(kind, row, folder, config_path, *, sync, wait_minutes, guard, db):
            db.update_job(job_id, usd_spent=1.90)  # as if the job's own earlier requests had cost this much today
            return job.Plan([Step("03_structure", [], guard=guard, db=db, cik=cik, usd=0.40)])
        return job.run_job(cik, "build", db=db, caps=caps, stages=stages, jobs_dir=tmp_path / "jobs", echo=False, job_id=job_id)

    w, _, post = make(db, tmp_path, build=build, caps=caps)
    w.tick()
    assert db.job(db.request(rid)["job_id"])["status"] == "parked"
    assert status(db, rid) == ("queued", "daily spending limit reached, resumes tomorrow") and post.calls == []


# --- failing ------------------------------------------------------------------------------------------------------------


def test_two_failed_builds_park_the_request_and_the_company_is_not_tried_again(db, tmp_path, caplog) -> None:
    rid = ask(db, CIK)
    also = None
    build = Builds(db, tmp_path, codes={CIK: [1, 1, 0]})
    w, _, post = make(db, tmp_path, build=build)
    with caplog.at_level(logging.INFO, logger="pipeline.worker"):
        w.tick()
        assert status(db, rid) == ("open", None) and db.company(CIK)["status"] == "requested"  # one failure: one more try
        w.tick()
        assert status(db, rid) == ("parked", "build failed, we are looking at it") and db.company(CIK)["status"] == "parked"
        also = ask(db, CIK, email="second@example.com", minutes=9)
        w.tick()
        w.tick()
    assert status(db, also) == ("parked", "build failed, we are looking at it") and db.request(also)["job_id"] is None
    assert build.calls == [CIK, CIK] and len(db.jobs(CIK)) == 2 and post.calls == []
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith(f"request={rid} cik={CIK} status=")]
    # one line per state change: request, cik, status, and what the request's job has spent so far
    assert [line.split()[2:4] for line in lines] == [["status=building", "usd=0.0000"], ["status=open", "usd=0.0500"],
                                                     ["status=building", "usd=0.0000"], ["status=parked", "usd=0.0500"]]


def test_a_company_the_sec_cannot_describe_is_parked_for_manual_setup(db, tmp_path) -> None:
    def derive(cik, wanted):
        raise worker.NeedsSetup("the fiscal year ends in month 1")

    rid = ask(db, CIK)
    waiting_on_it = ask(db, CIK, email="second@example.com", minutes=1, status="duplicate", reason="already requested")
    w, build, post = make(db, tmp_path, derive=derive)
    w.tick()
    assert status(db, rid) == ("parked", "needs manual setup") == status(db, waiting_on_it)  # a duplicate shares its fate
    assert db.company(CIK) is None and db._one("SELECT count(*) FROM jobs") == 0 and build.calls == [] and post.calls == []


def test_a_ticker_another_company_holds_is_not_taken(db, tmp_path) -> None:
    db.upsert_company(OTHER, ticker="EXMP", name="Other Corp", status="config")
    rid = ask(db, CIK, ticker="EXMP")
    w, build, _ = make(db, tmp_path)
    w.tick()
    assert status(db, rid) == ("parked", "needs manual setup") and db.company(CIK) is None and build.calls == []


def test_a_company_row_written_by_hand_is_built_as_it_is(db, tmp_path) -> None:
    db.upsert_company(CIK, ticker="EXMP", name="Example Corporation", status="requested", fiscal_year_end_month=1,
                      fiscal_year_named_for="start", tax_rate_parens_negative=True)
    rid = ask(db, CIK)
    w, build, _ = make(db, tmp_path, derive=pilot_rows.no_network)
    w.tick()
    company = db.company(CIK)
    assert status(db, rid) == ("live", None)
    assert (company["status"], company["fiscal_year_end_month"], company["fiscal_year_named_for"], company["tax_rate_parens_negative"]) == ("built", 1, "start", True)


EMPTY_BODY = """Company 123 (T123) is built. The pipeline found no forward-looking statements with a number in its filings, so there is no company page yet.

You asked for this company on 2026-11-20. Reply to this email if you think that is wrong.
"""


def test_a_build_with_nothing_to_show_is_parked_and_emailed_that_there_is_no_page(db, tmp_path) -> None:
    rid = ask(db, CIK)
    also = ask(db, CIK, email="second@example.com", minutes=1)
    w, build, post = make(db, tmp_path, build=Builds(db, tmp_path, rows=False))
    w.tick()
    for request in (rid, also):
        assert status(db, request) == ("parked", "built, but no forward-looking statements were found")
        assert db.request(request)["notified_at"] is not None and db.request(request)["resend_id"]
    assert db.company(CIK)["status"] == "built" and build.calls == [CIK]
    assert [c["json"]["to"] for c in post.calls] == [["reader@example.com"], ["second@example.com"]]
    for call in post.calls:
        assert call["json"]["subject"] == "T123 is built, with nothing to show" and call["json"]["text"] == EMPTY_BODY
        assert "http" not in call["json"]["text"]  # there is no page to link to
    w.tick()
    w.tick()
    assert len(post.calls) == 2 and build.calls == [CIK]  # once each, and the company is not built again


def test_a_nothing_to_show_email_that_did_not_go_out_is_sent_on_a_later_pass(db, tmp_path) -> None:
    class DownAtFirst(pilot_rows.FakePost):
        def __call__(self, url, **kwargs):
            self.status = 500 if len(self.calls) < 2 else 200
            return super().__call__(url, **kwargs)

    rid = ask(db, CIK)
    w, _, post = make(db, tmp_path, build=Builds(db, tmp_path, rows=False), post=DownAtFirst())
    w.tick()  # tried when the build settles, and again at the end of the pass: both fail
    assert status(db, rid) == ("parked", "built, but no forward-looking statements were found")
    assert len(post.calls) == 2 and db.request(rid)["notified_at"] is None
    w.tick()  # a parked request is still owed its email
    assert len(post.calls) == 3 and db.request(rid)["notified_at"] is not None and db.request(rid)["resend_id"] == "resend-3"
    assert status(db, rid) == ("parked", "built, but no forward-looking statements were found")
    w.tick()
    assert len(post.calls) == 3


def test_a_request_parked_for_another_reason_is_not_picked_up_for_the_nothing_to_show_email(db, tmp_path) -> None:
    db.upsert_company(CIK, ticker="EXMP", name="Example Corporation", status="built", fiscal_year_end_month=12)
    rid = ask(db, CIK, status="parked", reason="build failed, we are looking at it")
    assert db.ciks_owed_an_email(parked_reason=worker.EMPTY_REASON) == [] and db.ciks_owed_an_email() == []
    db.update_request(rid, reason=worker.EMPTY_REASON)
    assert db.ciks_owed_an_email(parked_reason=worker.EMPTY_REASON) == [CIK] and db.ciks_owed_an_email() == []


# --- the address is not verified ------------------------------------------------------------------------------------------


def test_one_address_is_emailed_once_about_a_company_and_only_so_often_in_a_day(db, tmp_path) -> None:
    for cik, ticker in [(CIK, "EXMP"), (OTHER, "OTHR"), (THIRD, "THRD")]:
        pilot_rows.built_company(db, cik, ticker, f"Company {ticker}")
    w, _, post = make(db, tmp_path, build=pilot_rows.no_network, settings=pilot_rows.settings(max_per_address_per_day=2))
    first = ask(db, CIK)
    w.tick()
    again = ask(db, CIK, email="READER@example.com", minutes=1)  # the same address asks for the same company again
    w.tick()
    assert len(post.calls) == 1 and status(db, again) == ("live", None)
    assert db.request(again)["notified_at"] is not None and db.request(again)["resend_id"] is None
    second, third = ask(db, OTHER, minutes=2), ask(db, THIRD, minutes=3)
    w.tick()
    w.tick()
    assert len(post.calls) == 2 and db.request(second)["notified_at"] is not None  # the day's second, and last
    assert status(db, third) == ("live", None) and db.request(third)["notified_at"] is None  # held, not dropped
    db.conn.execute("UPDATE company_requests SET notified_at = notified_at - interval '1 day' WHERE id IN (%s, %s)", (first, second))
    w.tick()
    assert len(post.calls) == 3 and db.request(third)["resend_id"] == "resend-3"


def test_an_email_resend_refuses_is_not_sent_over_and_over(db, tmp_path) -> None:
    pilot_rows.built_company(db, CIK, "EXMP", "Example Corporation")
    rid, bad = ask(db, CIK), ask(db, CIK, email="not an address", minutes=1)
    w, _, post = make(db, tmp_path, build=pilot_rows.no_network, post=pilot_rows.FakePost(status=422))
    for _ in range(4):
        w.tick()
    assert len(post.calls) == 1 and [c["json"]["to"] for c in post.calls] == [["reader@example.com"]]
    assert status(db, rid) == ("live", None) and db.request(rid)["notified_at"] is None and db.request(bad)["notified_at"] is None


# --- one worker, and stopping -----------------------------------------------------------------------------------------------


def test_a_second_worker_finds_the_lock_held_and_exits_0(db, tmp_path, caplog) -> None:
    rid = ask(db, CIK)
    held = db.worker_lock()
    assert held is not None
    try:
        w, build, post = make(db, tmp_path)
        with caplog.at_level(logging.INFO, logger="pipeline.worker"):
            assert w.run(once=True) == 0
        assert build.calls == [] and post.calls == [] and status(db, rid) == ("open", None)
        assert any("lock is held by another worker" in r.getMessage() for r in caplog.records)
    finally:
        held.close()
    with caplog.at_level(logging.INFO, logger="pipeline.worker"):
        assert w.run(once=True) == 0  # the lock went with its connection
    assert status(db, rid) == ("live", None)
    assert any("worker lock taken" in r.getMessage() for r in caplog.records)


def test_sigterm_between_stages_stops_at_the_boundary_and_the_next_worker_resumes(db, tmp_path) -> None:
    rid = ask(db, CIK)
    waiting_too = ask(db, OTHER, email="second@example.com", minutes=1)
    before = signal.getsignal(signal.SIGTERM)
    build = Builds(db, tmp_path, between=lambda: os.kill(os.getpid(), signal.SIGTERM))  # arrives while the first stage runs
    w, _, post = make(db, tmp_path, build=build)
    assert w.run() == 0  # not --once: it is the signal that ends the loop
    assert build.ran == [(CIK, "03_structure")]  # the stage in hand finished; the next one was not started
    row = db.request(rid)
    paused = db.job(row["job_id"])
    assert row["status"] == "building" and paused["status"] == "waiting" and "shut down" in paused["error"]
    assert float(paused["usd_spent"]) == pytest.approx(0.05)  # what the finished stage spent is on the job
    assert status(db, waiting_too) == ("open", None) and post.calls == []
    assert signal.getsignal(signal.SIGTERM) is before  # the handler is the worker's only while it runs

    held = db.worker_lock()  # the lock was released on the way out
    assert held is not None
    held.close()

    build.between = None
    again, _, _ = make(db, tmp_path, build=build, post=post)
    assert again.run(once=True) == 0
    assert status(db, rid) == ("live", None) and db.request(rid)["job_id"] == row["job_id"] and db.job(row["job_id"])["status"] == "succeeded"
    assert status(db, waiting_too) == ("live", None) and len(post.calls) == 2 and len(db.jobs(CIK)) == 1


def test_a_request_left_building_by_a_killed_worker_is_carried_on(db, tmp_path) -> None:
    db.upsert_company(CIK, ticker="EXMP", name="Example Corporation", status="building", fiscal_year_end_month=12)
    job_id = db.create_job(CIK, "build")  # running, and nothing is running it
    rid = ask(db, CIK, status="building", job_id=job_id)
    w, build, post = make(db, tmp_path)
    w.tick()
    assert status(db, rid) == ("live", None) and db.job(job_id)["status"] == "succeeded" and len(db.jobs(CIK)) == 1 and len(post.calls) == 1
