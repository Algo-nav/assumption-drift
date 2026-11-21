"""Shared fixtures.

The record built here is synthetic. It is shaped like a real row but it names
no real company and points at no real document, so nothing in the test suite
can be mistaken for a published row.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import pytest

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64

_BASE_URL = "https://www.sec.gov/Archives/edgar/data/123/000000012324000001"


def _evidence(**overrides: Any) -> dict[str, Any]:
    evidence = {
        "source_url": f"{_BASE_URL}/example-8k.htm",
        "accession_number": "0000000123-24-000001",
        "filing_type": "8-K",
        "filed_at": date(2024, 2, 1),
        "fetched_at": datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc),
        "content_sha256": SHA_A,
        "excerpt": "The Company expects full year 2024 revenue of $5.0 billion to $6.0 billion.",
    }
    evidence.update(overrides)
    return evidence


@pytest.fixture
def evidence_data():
    return _evidence


@pytest.fixture
def record_data():
    """Returns a factory for a valid ResearchRecord payload, with overrides applied at the top level."""

    def _record(**overrides: Any) -> dict[str, Any]:
        record = {
            "record_id": "01HZY8Q9XMR3T7VBN2CDEFGHJK",
            "company": "Example Corporation",
            "ticker": "EXMP",
            "cik": "0000000123",
            "claim": "Example Corporation expects FY2024 revenue between 5.0 and 6.0 billion USD.",
            "assumption": {
                "text": "We expect full year 2024 revenue of $5.0 billion to $6.0 billion.",
                "metric": "revenue",
                "target_low": 5_000.0,
                "target_high": 6_000.0,
                "unit": "USD millions",
                "target_period": "FY2024",
                "stated_at": date(2024, 2, 1),
                "evidence": _evidence(),
            },
            "outcome": {
                "reported_value": 4_800.0,
                "reported_at": date(2025, 2, 3),
                "evidence": _evidence(
                    source_url=f"{_BASE_URL}/example-10k.htm",
                    accession_number="0000000123-25-000004",
                    filing_type="10-K",
                    filed_at=date(2025, 2, 3),
                    content_sha256=SHA_B,
                    excerpt="Full year 2024 revenue was $4.8 billion.",
                ),
            },
            "invalidation_condition": "reported value falls outside [5000.0, 6000.0]",
            "status": "missed",
            "acknowledged_at": None,
            "acknowledgement_evidence": None,
            "days_to_falsifiable": 368,
            "days_to_acknowledged": None,
            "last_reviewed_at": date(2026, 9, 20),
            "reviewer": "navneet",
        }
        record.update(overrides)
        return record

    return _record


@pytest.fixture(autouse=True)
def _spend_ledger_in_tmp(tmp_path, monkeypatch):
    """data/spend_ledger.jsonl is tracked. No test may append to the real one, however it reaches llm.run_batch."""
    from pipeline import common

    monkeypatch.setattr(common, "SPEND_LEDGER_PATH", tmp_path / "spend_ledger.jsonl")


@pytest.fixture(autouse=True)
def _manifest_in_tmp(tmp_path, monkeypatch):
    """data/manifest.json is tracked. Tests read a stand-in one (processed through 2026-09-20, no companies), written to tmp."""
    from pipeline import common

    path = tmp_path / "manifest.json"
    path.write_text('{"processed_through": "2026-09-20", "companies": {}}\n', encoding="utf-8")
    monkeypatch.setattr(common, "MANIFEST_PATH", path)


# --- which store a test may reach ---------------------------------------------------------------------------------------
#
# The suite decides where its rows go; the shell it was started from does not. STORE=postgres in the environment once
# sent every stage a test ran to the tables of the live `public` schema. So, at session start:
#
#   * STORE is set to files for the whole run, and DATABASE_URL and STORE_SCHEMA are taken out of the environment;
#   * a test not marked `postgres` cannot get a database at all: there is no DATABASE_URL in its environment,
#     store.database_url() refuses (it would otherwise read .env) and so does PostgresBackend, whatever the test sets;
#   * a test marked `postgres` gets DATABASE_URL back, and may open a PostgresBackend only on a schema named test_*.
#     The `pg` fixtures make such a schema; `public`, or whatever STORE_SCHEMA named, is refused before connecting;
#   * a test that asks for a `pg` fixture without the mark is an error at collection, not a skip.

_SESSION: dict[str, Any] = {"database_url": None, "store": None, "postgres_test": False, "installed": False}

TEST_SCHEMA_PREFIX = "test_"


def _install_store_guard() -> None:
    import os

    from research_record import store

    if _SESSION["installed"]:
        return
    _SESSION.update(installed=True, store=os.environ.get("STORE"), database_url=os.environ.pop("DATABASE_URL", None))
    os.environ["STORE"] = "files"
    os.environ.pop("STORE_SCHEMA", None)
    real_url, real_init = store.database_url, store.PostgresBackend.__init__

    def database_url() -> str:
        if not _SESSION["postgres_test"]:
            raise store.StoreError("this test is not marked postgres: it has no database (tests/conftest.py)")
        return real_url()

    def __init__(self, url: str | None = None, schema: str | None = None) -> None:
        if not _SESSION["postgres_test"]:
            raise store.StoreError("this test is not marked postgres: it may not open a database (tests/conftest.py)")
        target = schema or os.environ.get("STORE_SCHEMA") or "public"
        if not target.startswith(TEST_SCHEMA_PREFIX):
            raise store.StoreError(f"refusing schema {target!r}: the test suite only opens schemas named {TEST_SCHEMA_PREFIX}* (tests/conftest.py)")
        real_init(self, url, schema)

    store.database_url = database_url
    store.PostgresBackend.__init__ = __init__


def pytest_sessionstart(session) -> None:
    _install_store_guard()
    if (_SESSION["store"] or "").strip().lower() == "postgres":
        line = ("STORE=postgres is set in the environment and the suite ignores it: every test runs with STORE=files, and the tests "
                "marked postgres use a schema of their own named test_*, so no test can write to the live schema.")
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        reporter.write_line(line) if reporter is not None else print(line)


def pytest_collection_modifyitems(config, items) -> None:
    unmarked = [item.nodeid for item in items
                if {"pg", "pg_module"} & set(getattr(item, "fixturenames", ())) and item.get_closest_marker("postgres") is None]
    if unmarked:
        raise pytest.UsageError("these tests use a pg fixture and are not marked postgres (pytestmark = pytest.mark.postgres): "
                                + ", ".join(unmarked[:10]))


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item) -> None:
    """Before any fixture of the test is made: say whether it may have a database, and put the environment as it must be.
    A test's own monkeypatch.setenv still works inside the test, and is undone before the next one gets here."""
    import os

    _install_store_guard()
    marked = item.get_closest_marker("postgres") is not None
    _SESSION["postgres_test"] = marked
    os.environ["STORE"] = "files"
    os.environ.pop("STORE_SCHEMA", None)
    if marked and _SESSION["database_url"]:
        os.environ["DATABASE_URL"] = _SESSION["database_url"]
    else:
        os.environ.pop("DATABASE_URL", None)


# --- Postgres -------------------------------------------------------------------------------------------------------
#
# The Postgres tests run against whatever DATABASE_URL names (the environment, or .env at the repo root), local or
# remote, and always inside a schema of their own named test_<timestamp>, which is dropped afterwards. They never
# create, write or drop anything in `public`. With no database they are skipped, unless REQUIRE_POSTGRES=1 (CI), which
# makes a missing database a failure.

_pg_counter = iter(range(1, 1_000_000))


def _pg_backend():
    import os
    import time

    from research_record import store

    if not _SESSION["postgres_test"]:
        pytest.fail("a pg fixture was asked for by a test that is not marked postgres")
    try:
        url = store.database_url()
        schema = f"{TEST_SCHEMA_PREFIX}{time.time_ns() // 1000}{next(_pg_counter):03d}"
        db = store.PostgresBackend(url, schema)
    except Exception as exc:  # noqa: BLE001 - no URL, no driver, or nothing listening: all the same to a test
        if os.environ.get("REQUIRE_POSTGRES") == "1":
            raise
        pytest.skip(f"no Postgres to test against ({type(exc).__name__}); set DATABASE_URL")
    assert db.schema != "public" and db.schema.startswith(TEST_SCHEMA_PREFIX)
    db.apply_schema()
    return db


@pytest.fixture
def pg():
    """A PostgresBackend on a fresh schema test_<timestamp> with db/schema.sql applied. The schema is dropped afterwards."""
    db = _pg_backend()
    try:
        yield db
    finally:
        db.drop_schema()
        db.close()


@pytest.fixture(scope="module")
def pg_module():
    """The same, shared by one test module (for the tests that load the real dataset once)."""
    db = _pg_backend()
    try:
        yield db
    finally:
        db.drop_schema()
        db.close()
