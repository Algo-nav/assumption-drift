"""tests/conftest.py: the suite decides which store a test reaches, whatever the shell it was started from says.

The first tests here are deliberately not marked postgres: they are the proof that such a test has no database.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import conftest
from research_record import store

REPO = Path(__file__).resolve().parent.parent
NOWHERE = "postgresql://nobody:nothing@127.0.0.1:1/nowhere"  # nothing listens on port 1; it is never dialled anyway


def test_an_unmarked_test_cannot_see_database_url() -> None:
    assert "DATABASE_URL" not in os.environ and "STORE_SCHEMA" not in os.environ
    assert os.environ["STORE"] == "files" and store.kind() == "files" and store.backend().name == "files"
    with pytest.raises(store.StoreError, match="not marked postgres"):
        store.database_url()  # not from .env either


def test_an_unmarked_test_cannot_open_a_database_whatever_it_sets(monkeypatch) -> None:
    import psycopg

    def no_connection(*args, **kwargs):
        raise AssertionError("an unmarked test reached psycopg.connect")

    monkeypatch.setattr(psycopg, "connect", no_connection)
    monkeypatch.setenv("STORE", "postgres")
    monkeypatch.setenv("DATABASE_URL", NOWHERE)
    monkeypatch.setenv("STORE_SCHEMA", "test_looks_allowed")
    for attempt in (store.backend, store.PostgresBackend, lambda: store.PostgresBackend(NOWHERE, "test_looks_allowed"),
                    lambda: store.PostgresBackend(NOWHERE, "public")):
        with pytest.raises(store.StoreError, match="not marked postgres"):
            attempt()


def test_what_one_test_set_is_gone_for_the_next() -> None:
    # a test that wrote to os.environ directly and did not clean up: the next test's setup puts it back
    os.environ["STORE"], os.environ["DATABASE_URL"] = "postgres", NOWHERE
    try:
        item = type("Item", (), {"get_closest_marker": lambda self, name: None})()
        conftest.pytest_runtest_setup(item)
        assert os.environ["STORE"] == "files" and "DATABASE_URL" not in os.environ
    finally:
        os.environ["STORE"] = "files"
        os.environ.pop("DATABASE_URL", None)


@pytest.mark.postgres
def test_a_postgres_test_may_only_open_a_schema_named_test(monkeypatch) -> None:
    import psycopg

    def no_connection(*args, **kwargs):
        raise AssertionError("a refused schema reached psycopg.connect")

    monkeypatch.setattr(psycopg, "connect", no_connection)
    assert os.environ["STORE"] == "files" and "STORE_SCHEMA" not in os.environ
    with pytest.raises(store.StoreError, match="refusing schema 'public'"):
        store.PostgresBackend(NOWHERE)                      # no schema named: that is public
    with pytest.raises(store.StoreError, match="refusing schema 'public'"):
        store.PostgresBackend(NOWHERE, "public")
    monkeypatch.setenv("STORE", "postgres")
    monkeypatch.setenv("DATABASE_URL", NOWHERE)
    with pytest.raises(store.StoreError, match="refusing schema 'public'"):
        store.backend()                                     # STORE=postgres with no STORE_SCHEMA is public too
    monkeypatch.setenv("STORE_SCHEMA", "live")
    with pytest.raises(store.StoreError, match="refusing schema 'live'"):
        store.backend()
    monkeypatch.setenv("STORE_SCHEMA", "test_guard_probe")
    with pytest.raises(AssertionError, match="reached psycopg.connect"):
        store.PostgresBackend()                             # a test_ schema is let through to the driver


PROBE = '''
import os
import pytest

def test_unmarked():
    assert os.environ["STORE"] == "files" and "DATABASE_URL" not in os.environ and "STORE_SCHEMA" not in os.environ

@pytest.mark.postgres
def test_marked():
    assert os.environ["STORE"] == "files" and os.environ["DATABASE_URL"] == "%s" and "STORE_SCHEMA" not in os.environ
''' % NOWHERE


@pytest.mark.parametrize("exported, said", [("postgres", 1), ("files", 0), (None, 0)])
def test_store_postgres_in_the_environment_is_ignored_and_the_suite_says_so_once(tmp_path, exported, said) -> None:
    probe = tmp_path / "test_probe.py"
    probe.write_text(PROBE, encoding="utf-8")
    (tmp_path / "conftest.py").write_text((REPO / "tests" / "conftest.py").read_text(encoding="utf-8"), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("STORE", "STORE_SCHEMA", "DATABASE_URL", "PYTEST_CURRENT_TEST")}
    env.update({"DATABASE_URL": NOWHERE, "STORE_SCHEMA": "public", "PYTHONPATH": str(REPO)})
    if exported:
        env["STORE"] = exported
    done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(tmp_path), "-c", os.devnull,
                           "-W", "ignore::pytest.PytestUnknownMarkWarning", str(probe)],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    out = done.stdout + done.stderr
    assert done.returncode == 0 and "2 passed" in out, out
    assert out.count("STORE=postgres is set in the environment and the suite ignores it") == said, out


def test_a_pg_fixture_without_the_mark_is_an_error_at_collection(tmp_path) -> None:
    (tmp_path / "test_probe.py").write_text("def test_unmarked(pg):\n    pass\n", encoding="utf-8")
    (tmp_path / "conftest.py").write_text((REPO / "tests" / "conftest.py").read_text(encoding="utf-8"), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("STORE", "STORE_SCHEMA", "DATABASE_URL", "PYTEST_CURRENT_TEST")}
    env["PYTHONPATH"] = str(REPO)
    done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(tmp_path), "-c", os.devnull,
                           str(tmp_path / "test_probe.py")], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode != 0 and "not marked postgres" in done.stdout + done.stderr, done.stdout + done.stderr
