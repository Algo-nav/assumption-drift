"""scripts/load_published.py and scripts/export_release.py, against the dataset in this checkout.

The published dataset is loaded once into a schema of its own. What the loader finds on disk decides how much there is
to load: a fresh checkout has the review queues and the outcomes (tracked), a working copy also has the candidates, the
drafts, the cached filings and the model archive (git-ignored). The release round trip needs only the review queues.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from pipeline import common
from research_record import store

pytestmark = pytest.mark.postgres

REPO = Path(__file__).resolve().parent.parent
PUBLISHED = REPO / "data" / "release" / "assumption_drift.jsonl"


def script(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


load_published = script("load_published")
export_release = script("export_release")


def digest(db) -> dict[str, tuple[int, str]]:
    """(row count, hash of every row) per table: equal digests mean an identical database."""
    out = {}
    for table, order in [("companies", "cik"), ("filings", "accession"), ("candidates", "candidate_id"), ("statements", "record_id"),
                         ("stage_diagnostics", "cik, source, seq"), ("llm_calls", "id"), ("jobs", "id"), ("flags", "id")]:
        rows = db._all(f"SELECT row_to_json(t)::text FROM {table} t ORDER BY {order}")
        out[table] = (len(rows), hashlib.sha256("\n".join(r[0] for r in rows).encode()).hexdigest())
    return out


@pytest.fixture(scope="module")
def loaded(pg_module):
    """The published companies, loaded once. Skips if this checkout has no review queues at all."""
    if not PUBLISHED.exists() or not any(common.REVIEW_DIR.glob("*.csv")):
        pytest.skip("no data/review/ and data/release/ in this checkout")
    patch = pytest.MonkeyPatch()
    patch.setenv("DATABASE_URL", pg_module.url)
    patch.setenv("STORE_SCHEMA", pg_module.schema)
    patch.delenv("STORE", raising=False)
    try:
        assert load_published.main([]) == 0  # 0 also means: everything read back is byte-identical to its file
        yield pg_module
    finally:
        patch.undo()


def test_the_fifteen_companies_and_every_statement_are_in(loaded) -> None:
    published_lines = PUBLISHED.read_text(encoding="utf-8").splitlines()
    assert loaded._one("SELECT count(*) FROM companies WHERE publish AND status = 'built'") == 15
    approved = loaded._one("SELECT count(*) FROM statements WHERE review_decision IN ('y', 'v')")
    assert approved == len(published_lines) == loaded._one("SELECT count(*) FROM statements WHERE published_status IS NOT NULL")
    # the rejected rows keep their notes, and the held rows are there with nothing decided
    assert loaded._one("SELECT count(*) FROM statements WHERE review_decision = 'n' AND btrim(COALESCE(reviewer_note, '')) = ''") == 0
    assert loaded._one("SELECT count(*) FROM statements WHERE review_decision = 'n'") > 200
    assert loaded._one("SELECT count(*) FROM statements WHERE review_decision = 's'") == 10
    assert loaded._one("SELECT count(*) FROM statements WHERE review_decision = 's' AND (approved OR reviewer_note IS NOT NULL)") == 0
    # every decision recorded so far was a person's; nothing says how a statement changed from the one before it
    assert loaded._one("SELECT count(*) FROM statements WHERE review_decision IS NOT NULL AND review_tier IS DISTINCT FROM 'hand'") == 0
    assert loaded._one("SELECT count(*) FROM statements WHERE change_direction IS NOT NULL") == 0


def test_every_review_csv_row_is_a_statement_with_the_same_cells(loaded) -> None:
    review = importlib.import_module("pipeline.05_review")
    files = store.FilesBackend()
    total = 0
    for c in loaded.companies():
        path = common.REVIEW_DIR / f"{c['cik']}.csv"
        if c["publish"] and path.exists():
            rows = files.read_csv(path)
            assert loaded.read_csv(path) == rows
            assert set(rows[0]) == set(review.COLUMNS)
            total += len(rows)
    assert total == loaded._one("SELECT count(*) FROM statements WHERE queue_seq IS NOT NULL")


def test_loading_again_changes_nothing(loaded) -> None:
    before = digest(loaded)
    assert load_published.main(["--no-verify"]) == 0
    assert load_published.main([]) == 0
    assert digest(loaded) == before
    assert before["statements"][0] > 1500 and before["companies"][0] >= 15


def test_the_release_exported_from_the_database_is_the_published_file_byte_for_byte(loaded, tmp_path, capsys) -> None:
    assert export_release.main(["--out", str(tmp_path / "release"), "--csv-dir", str(tmp_path / "csv")]) == 0
    exported = (tmp_path / "release" / "assumption_drift.jsonl").read_bytes()
    assert exported == PUBLISHED.read_bytes()
    assert hashlib.sha256(exported).hexdigest() == hashlib.sha256(PUBLISHED.read_bytes()).hexdigest()
    out = capsys.readouterr().out
    assert "byte-identical" in out
    for c in loaded.companies():
        if c["publish"]:
            assert (tmp_path / "csv" / f"{c['cik']}.csv").read_bytes() == (common.REVIEW_DIR / f"{c['cik']}.csv").read_bytes()


def test_each_published_row_round_trips_to_the_identical_row(loaded, tmp_path) -> None:
    assert export_release.main(["--out", str(tmp_path)]) == 0
    ours = [json.loads(line) for line in (tmp_path / "assumption_drift.jsonl").read_text(encoding="utf-8").splitlines()]
    theirs = [json.loads(line) for line in PUBLISHED.read_text(encoding="utf-8").splitlines()]
    assert len(ours) == len(theirs)
    for mine, published in zip(ours, theirs):
        assert mine == published, published["record_id"]
        assert list(mine) == list(published)  # and in the same field order


def test_a_changed_row_fails_the_export_and_the_difference_is_printed(loaded, tmp_path, capsys) -> None:
    rid, high, excerpt = loaded.conn.execute(
        "SELECT record_id, target_high, assumption_excerpt FROM statements WHERE published_status IS NOT NULL AND target_high IS NOT NULL "
        "ORDER BY record_id LIMIT 1").fetchone()
    try:
        loaded.conn.execute("UPDATE statements SET target_high = target_high + 1, assumption_excerpt = assumption_excerpt || ' ' WHERE record_id = %s", (rid,))
        assert export_release.main(["--out", str(tmp_path)]) == 1
        err = capsys.readouterr().err
        assert "not byte-identical" in err and rid in err
        assert "assumption.target_high" in err and "assumption.evidence.excerpt" in err  # a trailing space is a difference too
    finally:
        loaded.conn.execute("UPDATE statements SET target_high = %s, assumption_excerpt = %s WHERE record_id = %s", (high, excerpt, rid))
    assert export_release.main(["--out", str(tmp_path)]) == 0


def test_at_most_ten_differences_are_printed() -> None:
    published = "".join(json.dumps({"record_id": f"R{i}", "status": "met"}) + "\n" for i in range(30))
    ours = "".join(json.dumps({"record_id": f"R{i}", "status": "missed"}) + "\n" for i in range(25))
    found = export_release.differences(ours, published)
    assert len(found) == 10 and found[0] == "line 1: R0: status: database 'missed', published 'met'"
    assert export_release.differences(published, published) == []
    assert export_release.differences(published.replace("R29", "R29") + json.dumps({"record_id": "X"}) + "\n", published) == ["line 31: only in the database export: X"]


def test_the_archived_model_answers_are_in_with_their_fingerprints_and_costs(loaded) -> None:
    archives = sorted((REPO / "data" / "batches").glob("*.raw.jsonl"))
    if not archives:
        pytest.skip("data/batches/ is git-ignored and not in this checkout")
    lines = sum(sum(1 for line in p.read_text(encoding="utf-8").splitlines() if line.strip()) for p in archives)
    assert loaded._one("SELECT count(*) FROM llm_calls WHERE archive_seq IS NOT NULL") == lines
    assert loaded._one("SELECT count(*) FROM llm_calls WHERE kind <> 'count' AND response_text IS NOT NULL "
                       "AND response_sha256 IS DISTINCT FROM encode(sha256(convert_to(response_text, 'UTF8')), 'hex')") == 0
    assert loaded._one("SELECT count(*) FROM llm_calls WHERE kind <> 'count' AND fingerprint IS NOT NULL") > 0.99 * lines
    # the cost on the rows is the cost the local ledger recorded
    from pipeline import llm
    committed = llm.Ledger(REPO / "data" / "batches" / "ledger.jsonl").committed_usd()
    assert float(loaded._one("SELECT sum(usd) FROM llm_calls")) == pytest.approx(committed, abs=1e-6)
    with store.use(loaded):
        assert llm.Ledger().committed_usd() == pytest.approx(committed, abs=1e-6)
