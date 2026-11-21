"""pipeline/notify.py: the one email, built from rows, and the Resend call. No test here reaches a network."""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

import pytest
import requests

import pilot_rows
from pipeline import notify, worker

CIK = "0000000123"

COMPANY = {"cik": CIK, "ticker": "EXMP", "name": "Example Corporation"}
COUNTS = {"n_statements": 4, "n_filings": 2, "first_filing_date": date(2024, 2, 1), "last_filing_date": date(2025, 8, 5),
          "n_resolved": 2, "n_open": 2, "n_machine": 4}
REQUEST = {"id": 7, "cik": CIK, "email": "reader@example.com", "created_at": datetime(2026, 11, 20, 23, 30, tzinfo=timezone.utc),
           "ticker": "typed by the visitor", "name": "typed by the visitor"}

BODY = """Example Corporation (EXMP) is built.

4 forward-looking statements from 2 filings, 2024-02-01 to 2025-08-05.
2 have a resolution; 2 are still open.

https://pilot.scndorder.com/company/exmp

These rows were produced by the pipeline without a human review pass. Each row links to the filing it came from; check the filing before relying on a row.

You asked for this company on 2026-11-20. Reply to this email if something looks wrong.
"""


def test_the_email_is_exactly_the_agreed_shape_filled_from_the_rows() -> None:
    subject, body = notify.build_email(COMPANY, COUNTS, REQUEST, "https://pilot.scndorder.com")
    assert subject == "EXMP is live on assumption-drift"
    assert body == BODY
    assert "typed by the visitor" not in subject + body  # the request's own ticker and name are never used
    assert chr(0x2014) not in subject + body and "<" not in body  # no long dash, no HTML


def test_the_no_review_sentence_is_only_there_when_rows_are_machine_tier() -> None:
    _, body = notify.build_email(COMPANY, {**COUNTS, "n_machine": 0}, REQUEST, "https://pilot.scndorder.com/")
    assert notify.MACHINE_TIER_NOTE not in body
    assert body == BODY.replace(notify.MACHINE_TIER_NOTE + "\n\n", "")


def test_there_is_no_email_to_build_for_a_company_with_no_statement() -> None:
    empty = {"n_statements": 0, "n_filings": 0, "first_filing_date": None, "last_filing_date": None, "n_resolved": 0, "n_open": 0, "n_machine": 0}
    with pytest.raises(ValueError, match="no statement"):
        notify.build_email(COMPANY, empty, REQUEST, "https://pilot.scndorder.com")


def test_settings_come_from_the_environment_and_no_key_means_a_dry_run() -> None:
    bare = notify.Settings.from_env({})
    assert (bare.sender, bare.base_url, bare.dry_run, bare.api_key) == ("SCND Order <pilot@scndorder.com>", "https://pilot.scndorder.com", True, None)
    assert bare.why_dry == "RESEND_API_KEY is not set" and bare.max_per_address_per_day == 5
    live = notify.Settings.from_env({"RESEND_API_KEY": "re_x", "NOTIFY_FROM": "A <a@example.com>", "PILOT_BASE_URL": "https://example.com/"})
    assert (live.sender, live.base_url, live.dry_run) == ("A <a@example.com>", "https://example.com", False)
    dry = notify.Settings.from_env({"RESEND_API_KEY": "re_x", "NOTIFY_DRY_RUN": "1"})
    assert dry.dry_run and dry.why_dry == "NOTIFY_DRY_RUN=1"


def test_send_posts_plain_text_to_resend_and_returns_the_message_id() -> None:
    post = pilot_rows.FakePost()
    assert notify.send("reader@example.com", "s", "b", settings=pilot_rows.settings(), idempotency_key="company-request-7", post=post) == "resend-1"
    (call,) = post.calls
    assert call["url"] == "https://api.resend.com/emails"
    assert call["json"] == {"from": "SCND Order <pilot@scndorder.com>", "to": ["reader@example.com"], "subject": "s", "text": "b"}
    assert "html" not in call["json"]
    assert call["headers"]["Authorization"] == "Bearer re_test_key" and call["headers"]["Idempotency-Key"] == "company-request-7"


def test_send_says_whether_a_failure_is_worth_another_try() -> None:
    for status, permanent in [(422, True), (403, True), (429, False), (500, False)]:
        with pytest.raises(notify.NotifyError) as failed:
            notify.send("reader@example.com", "s", "b", settings=pilot_rows.settings(), post=pilot_rows.FakePost(status=status))
        assert failed.value.permanent is permanent and str(status) in str(failed.value)
    with pytest.raises(notify.NotifyError) as failed:
        notify.send("reader@example.com", "s", "b", settings=pilot_rows.settings(), post=pilot_rows.FakePost(fail=requests.ConnectionError("down")))
    assert failed.value.permanent is False


@pytest.mark.parametrize("address", ["a@example.com, b@example.com", "Reader <a@example.com>", "a@example.com\nBcc: b@example.com", "nobody", ""])
def test_an_address_that_is_not_one_plain_address_is_never_sent_to(address) -> None:
    assert not notify.valid_address(address)
    with pytest.raises(notify.NotifyError):
        notify.send(address, "s", "b", settings=pilot_rows.settings(), post=pilot_rows.no_network)


def test_the_log_shows_an_address_masked() -> None:
    assert notify.masked("reader@example.com") == "r***@example.com"


# --- against the database -----------------------------------------------------------------------------------------------


@pytest.mark.postgres
def test_the_counts_come_from_statements_and_filings(pg) -> None:
    pilot_rows.built_company(pg, CIK, "EXMP", "Example Corporation")
    assert pg.email_counts(CIK) == COUNTS  # four statements; the flag row and the unqueued draft are not counted
    # a company reviewed by hand: only the rows a person approved are counted, and none is machine tier
    other = "0000000456"
    pilot_rows.built_company(pg, other, "OTHR", "Other Corp", tier="hand")
    pilot_rows.statement(pg, other, 9, f"{other}-25-000007", proposed="met")  # in the queue, undecided: not shown, not counted
    assert pg.email_counts(other) == {**COUNTS, "n_machine": 0}
    assert pg.email_counts("0000000789")["n_statements"] == 0


@pytest.mark.postgres
def test_a_dry_run_writes_the_email_to_the_log_and_sets_notified_at_without_a_network_call(pg, caplog) -> None:
    pilot_rows.built_company(pg, CIK, "EXMP", "Example Corporation")
    rid = pg.create_request(CIK, "reader@example.com", created_at=REQUEST["created_at"], status="live")
    w = worker.Worker(pg, caps={"JOB_CAP_USD": 1.5, "DAILY_CAP_USD": 8.0, "WORKSPACE_CAP_USD": 70.0}, build=pilot_rows.no_network,
                      derive=pilot_rows.no_network, post=pilot_rows.no_network,
                      settings=notify.Settings.from_env({"RESEND_API_KEY": "re_x", "NOTIFY_DRY_RUN": "1"}))
    with caplog.at_level(logging.INFO, logger="pipeline.worker"):
        w.notify_cik(CIK)
    row = pg.request(rid)
    assert row["notified_at"] is not None and row["resend_id"] is None and row["status"] == "live"
    (line,) = [r.getMessage() for r in caplog.records if "NOT sent" in r.getMessage()]
    assert "NOTIFY_DRY_RUN=1" in line and "notified_at set" in line and f"request={rid} cik={CIK}" in line
    assert "Subject: EXMP is live on assumption-drift" in line and BODY in line
    assert "To: r***@example.com" in line and "reader@example.com" not in line  # the address itself is not logged
