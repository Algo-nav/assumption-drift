"""Synthetic rows for the worker and notify tests: a filing, statements quoting it, and a stand-in for requests.post.

Nothing here names a real company, a real filing or a real address.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from pipeline import common, notify


def filing(db, cik: str, accession: str, filed: str) -> None:
    db.record_filing({"accession": accession, "cik": cik, "filing_type": "8-K", "filed_at": filed,
                      "final_url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/ex99.htm",
                      "fetched_at": "2026-11-01T12:00:00+00:00"})


def statement(db, cik: str, n: int, accession: str, *, proposed: str = "unresolved", tier: str | None = None,
              approved: bool | None = None, published: str | None = None, empty_block: bool | None = None, queued: bool = True) -> str:
    """One statements row in the company's review queue (or, with queued=False, a draft 05 has not queued)."""
    record_id = common.stable_ulid(f"{cik}-{n}", date(2026, 11, 1))
    db.conn.execute(
        "INSERT INTO statements (record_id, cik, assumption_accession, aid_proposed_status, review_tier, approved, published_status, "
        "empty_block, queue_seq, status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'open')",
        (record_id, cik, accession, proposed, tier, approved, published, empty_block, n if queued else None))
    return record_id


def built_company(db, cik: str, ticker: str, name: str, *, tier: str | None = "machine") -> None:
    """A built company with four statements from two filings: two resolved (met, missed), two not."""
    db.upsert_company(cik, ticker=ticker, name=name, status="built", fiscal_year_end_month=12)
    four_statements(db, cik, tier=tier)


def four_statements(db, cik: str, *, tier: str | None = "machine") -> None:
    first, second = f"{cik}-24-000001", f"{cik}-25-000007"
    filing(db, cik, first, "2024-02-01")
    filing(db, cik, second, "2025-08-05")
    hand = tier == "hand"
    for n, (accession, status) in enumerate([(first, "met"), (first, "missed"), (second, "unresolved"), (second, "unresolved")]):
        statement(db, cik, n, accession, proposed=status, tier=tier, approved=True if hand else None,
                  published=status if hand else None)
    statement(db, cik, 4, second, empty_block=True)          # an outlook-block flag: not a statement, never counted
    statement(db, cik, 5, second, queued=False)              # a draft that is not in the queue: not counted


class FakePost:
    """requests.post, for pipeline.notify: records every call, answers as Resend does."""

    def __init__(self, status: int = 200, fail: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.status, self.fail = status, fail

    def __call__(self, url: str, **kwargs) -> SimpleNamespace:
        self.calls.append({"url": url, **kwargs})
        if self.fail is not None:
            raise self.fail
        n = len(self.calls)
        return SimpleNamespace(status_code=self.status, json=lambda: {"id": f"resend-{n}"})


def no_network(*args, **kwargs):
    raise AssertionError("nothing may be sent here")


def settings(**overrides) -> notify.Settings:
    values = {"api_key": "re_test_key", "sender": notify.DEFAULT_FROM, "base_url": notify.DEFAULT_BASE_URL, "dry_run": False,
              "max_per_address_per_day": 5}
    values.update(overrides)
    return notify.Settings(**values)
