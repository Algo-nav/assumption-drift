"""research_record/store.py against Postgres: the schema, and every list a stage reads or writes, there and back."""

from __future__ import annotations

import copy
import csv
import hashlib
import importlib
import io
import json
import re
from datetime import date
from pathlib import Path

import pytest

import fakes
from pipeline import common, llm
from research_record import store
from research_record.schema import ResearchRecord

review = importlib.import_module("pipeline.05_review")
publish = importlib.import_module("pipeline.06_publish")

pytestmark = pytest.mark.postgres

REPO = Path(__file__).resolve().parent.parent
COMPANY = common.Company("Example Corporation", "EXMP", "0000000123")
TODAY = date(2026, 9, 20)


@pytest.fixture
def db(pg):
    pg.upsert_company(COMPANY.cik, ticker=COMPANY.ticker, name=COMPANY.name)
    with store.use(pg):
        yield pg


@pytest.fixture
def parts(record_data):
    record = ResearchRecord(**record_data())
    draft = {"draft_id": record.record_id, "cik": COMPANY.cik, "ticker": "EXMP", "company": COMPANY.name,
             "capture_method": "section", "heading": "Q4 FY26 Guidance", "lead_in": "Our outlook is as follows:",
             "assumption": record.assumption.model_dump(mode="json")}
    outcome = {"draft_id": record.record_id, "outcome": record.outcome.model_dump(mode="json"), "acknowledgement": None, "outcome_reason": None}
    return draft, outcome


def another(draft, suffix):
    d = copy.deepcopy(draft)
    d["draft_id"] = draft["draft_id"][:-1] + suffix
    return d


def queue_row(draft, outcome=None, **changes) -> dict[str, str]:
    return {**review.build_row(draft, outcome, TODAY), **changes}


def path(folder: str, name: str) -> Path:
    return Path("/nowhere") / folder / name


REVIEW = path("review", f"{COMPANY.cik}.csv")


# --- the schema -------------------------------------------------------------------------------------------------------


def test_the_schema_applies_clean_and_a_second_time_changes_nothing(pg) -> None:
    def shape():
        return pg._all("SELECT table_name, column_name, data_type FROM information_schema.columns WHERE table_schema = %s ORDER BY 1, 2", (pg.schema,))

    first = shape()
    pg.apply_schema()
    assert shape() == first
    tables = {t for t, _, _ in first}
    assert tables == {"companies", "filings", "candidates", "statements", "llm_calls", "jobs", "flags", "stage_diagnostics", "company_requests"}


def test_the_indexes_asked_for_are_there(pg) -> None:
    defs = " ".join(d for (d,) in pg._all("SELECT indexdef FROM pg_indexes WHERE schemaname = %s", (pg.schema,)))
    for table, columns in [("statements", "cik"), ("statements", "cik, target_period"), ("statements", "record_id"),
                           ("statements", "assumption_accession"), ("filings", "accession"), ("candidates", "accession"),
                           ("candidates", "cik"), ("filings", "cik"), ("llm_calls", "fingerprint"), ("llm_calls", "cik"),
                           ("flags", "record_id"), ("jobs", "cik")]:
        assert re.search(rf"ON {pg.schema}\.{table} USING btree \({re.escape(columns)}\b", defs), (table, columns)


def test_row_level_security_is_on_for_every_table(pg) -> None:
    rls = dict(pg._all("SELECT c.relname, c.relrowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                       "WHERE n.nspname = %s AND c.relkind = 'r'", (pg.schema,)))
    tables = ["companies", "filings", "candidates", "statements", "llm_calls", "jobs", "flags", "stage_diagnostics", "company_requests"]
    assert len(tables) == 9 and rls == {t: True for t in tables}
    assert pg._all("SELECT tablename, policyname FROM pg_policies WHERE schemaname = %s", (pg.schema,)) == []


def test_every_review_csv_column_has_a_column_in_statements(pg) -> None:
    assert [c for c, _, _ in store.REVIEW_COLUMNS] == review.COLUMNS  # the same columns, in the CSV's order
    in_db = {c for (c,) in pg._all("SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = 'statements'", (pg.schema,))}
    assert {db_column for _, db_column, _ in store.REVIEW_COLUMNS} <= in_db


def test_the_column_map_names_every_column_of_every_file() -> None:
    readme = (REPO / "db" / "README.md").read_text(encoding="utf-8")
    for column in review.COLUMNS:
        assert f"`{column}`" in readme, column
    for key in ["sentence", "char_start", "context_before", "table_header", "draft_id", "custom_id", "parens_note", "evidence_lines",
                "outcome_reason", "withdrawal", "block_id", "raw_sha256", "final_url", "input_tokens", "batch_id", "fingerprint",
                "processed_through", "accessions_on_last_date"]:
        assert f"`{key}`" in readme, key


def test_the_public_schema_is_never_dropped(pg) -> None:
    public = store.PostgresBackend(pg.url, "public")
    try:
        with pytest.raises(store.StoreError, match="public"):
            public.drop_schema()
    finally:
        public.close()


def test_a_path_with_no_table_is_refused_not_written_to_disk(db, tmp_path) -> None:
    with pytest.raises(store.StoreError, match="no table"):
        store.write_jsonl(tmp_path / "somewhere" / "else.jsonl", [{"a": 1}])
    assert not (tmp_path / "somewhere").exists()


# --- the review queue -------------------------------------------------------------------------------------------------


def test_review_rows_come_back_exactly_as_written_in_the_same_order(db, parts) -> None:
    draft, outcome = parts
    rows = [queue_row(draft, outcome), queue_row(another(draft, "A")), queue_row(another(draft, "B"), outcome)]
    store.write_csv(REVIEW, rows, review.COLUMNS)
    assert store.read_csv(REVIEW) == rows
    store.write_csv(REVIEW, rows[::-1], review.COLUMNS)
    assert store.read_csv(REVIEW) == rows[::-1]


def test_excerpts_are_stored_verbatim_never_normalised(db, parts) -> None:
    draft, _ = parts
    # decomposed accents, a non-breaking space, a zero-width space, curly quotes, a tab, a CRLF, leading and trailing spaces
    odd = "  Café résumé — “about” $5.0​billion\tto\r\n$6.0 billion  "
    row = queue_row(draft, **{"assumption.evidence.excerpt": odd, "reviewer_note": " kept as typed \n", "aid_context": odd})
    store.write_csv(REVIEW, [row], review.COLUMNS)
    back = store.read_csv(REVIEW)[0]
    assert back["assumption.evidence.excerpt"] == odd and back["aid_context"] == odd and back["reviewer_note"] == " kept as typed \n"
    assert back["assumption.evidence.excerpt"].encode() == odd.encode()
    stored = db._one("SELECT assumption_excerpt FROM statements WHERE record_id = %s", (row["record_id"],))
    assert stored == odd


def test_empty_cells_three_way_flags_and_number_text_survive(db, parts) -> None:
    draft, _ = parts
    a = queue_row(draft, **{"ack_pending_review": "", "change_pending_review": "true", "assumption.target_low": "46", "assumption.target_high": "46.50"})
    b = queue_row(another(draft, "A"), **{"ack_pending_review": "false", "assumption.target_low": "-0.5", "assumption.target_high": ""})
    store.write_csv(REVIEW, [a, b], review.COLUMNS)
    assert store.read_csv(REVIEW) == [a, b]


@pytest.mark.parametrize("column, value", [
    ("assumption.target_low", "1e3"), ("assumption.target_low", "about 5"), ("assumption.stated_at", "2024-2-1"),
    ("assumption.evidence.fetched_at", "2026-09-20 12:00:00"), ("approved", "yes"), ("days_to_falsifiable", "3.0"),
])
def test_a_cell_the_column_could_not_give_back_unchanged_is_refused_with_the_row_named(db, parts, column, value) -> None:
    draft, outcome = parts
    good = queue_row(another(draft, "A"), outcome)
    store.write_csv(REVIEW, [good], review.COLUMNS)
    bad = queue_row(draft, outcome, **{column: value})
    with pytest.raises(store.StoreError, match=re.escape(column)) as err:
        store.write_csv(REVIEW, [good, bad], review.COLUMNS)
    assert bad["record_id"] in str(err.value)
    assert store.read_csv(REVIEW) == [good]  # nothing of the refused write went in


def test_a_csv_column_with_no_column_in_the_table_is_refused_not_dropped(db, parts) -> None:
    draft, _ = parts
    row = {**queue_row(draft), "aid_something_new": "x"}
    with pytest.raises(store.StoreError, match="aid_something_new"):
        store.write_csv(REVIEW, [row], review.COLUMNS + ["aid_something_new"])
    with pytest.raises(store.StoreError, match="aid_something_new"):
        store.write_csv(REVIEW, [row], review.COLUMNS)


def test_the_decision_the_tier_and_the_published_status_are_worked_out_from_the_row(db, parts) -> None:
    draft, outcome = parts
    rows = [
        queue_row(draft, outcome, approved="true", reviewer_note="ok"),
        queue_row(another(draft, "A"), outcome, approved="true", hand_verified="true"),
        queue_row(another(draft, "B"), outcome, reviewer_note="not guidance: component figure"),
        queue_row(another(draft, "C"), outcome),
        {**review.build_empty_block_row({"block_id": draft["draft_id"][:-1] + "D", "custom_id": "b-x", "cik": COMPANY.cik, "ticker": "EXMP",
                                         "company": COMPANY.name, "accession": "0000000123-24-000001", "filed_at": "2024-02-01", "char_start": 5,
                                         "heading": "Outlook", "lead_in": None, "table_header": None, "block_lines": 3,
                                         "reason": "the model returned no items", "evidence": None}), "reviewer_note": "looked: nothing there"},
    ]
    store.write_csv(REVIEW, rows, review.COLUMNS)
    got = db._all("SELECT review_decision, review_tier, published_status, approved, hand_verified FROM statements ORDER BY queue_seq")
    assert got == [("y", "hand", "missed", True, False), ("v", "hand", "missed", True, True), ("n", "hand", None, False, False),
                   (None, None, None, False, False), (None, None, None, False, False)]
    # the status the database stores is the one 06_publish gives the record
    records, invalid = publish.build_records([COMPANY], REVIEW.parent)
    assert invalid == 0 and {r.record_id: r.status for r in records} == dict(db._all("SELECT record_id, published_status FROM statements WHERE published_status IS NOT NULL"))


def test_a_held_row_stays_held_until_it_is_decided(db, parts) -> None:
    draft, outcome = parts
    row = queue_row(draft, outcome)
    store.write_csv(REVIEW, [row], review.COLUMNS)
    db.conn.execute("UPDATE statements SET review_decision = 's', review_tier = 'hand'")  # what the loader does for a held row
    store.write_csv(REVIEW, [{**row, "aid_verify": "yes"}], review.COLUMNS)
    assert db._all("SELECT review_decision, review_tier FROM statements") == [("s", "hand")]
    store.write_csv(REVIEW, [{**row, "approved": "true"}], review.COLUMNS)
    assert db._all("SELECT review_decision, review_tier FROM statements") == [("y", "hand")]


def test_a_row_that_leaves_the_queue_keeps_its_draft_and_loses_its_review_columns(db, parts) -> None:
    draft, outcome = parts
    drafts = path("drafts", f"{COMPANY.cik}.jsonl")
    store.write_jsonl(drafts, [draft, another(draft, "A")])
    store.write_csv(REVIEW, [queue_row(draft, outcome, approved="true"), queue_row(another(draft, "A"))], review.COLUMNS)
    store.write_csv(REVIEW, [queue_row(another(draft, "A"))], review.COLUMNS)
    assert [r["record_id"] for r in store.read_csv(REVIEW)] == [draft["draft_id"][:-1] + "A"]
    assert db._all("SELECT approved, review_decision, claim, draft IS NOT NULL FROM statements WHERE record_id = %s", (draft["draft_id"],)) == [(None, None, None, True)]
    store.write_jsonl(drafts, [another(draft, "A")])  # and once it is no draft either, it is gone
    assert db._one("SELECT count(*) FROM statements") == 1


def test_one_company_cannot_write_into_another_companys_queue(db, parts) -> None:
    draft, _ = parts
    db.upsert_company("0000000456", ticker="OTHR", name="Other Corp")
    store.write_csv(REVIEW, [queue_row(draft)], review.COLUMNS)
    with pytest.raises(store.StoreError, match="cik"):
        store.write_csv(path("review", "0000000456.csv"), [queue_row(draft)], review.COLUMNS)
    with pytest.raises(store.StoreError, match="another company"):
        store.write_csv(path("review", "0000000456.csv"), [queue_row(draft, cik="0000000456")], review.COLUMNS)


# --- the jsonl lists --------------------------------------------------------------------------------------------------


def candidate(char_start, sentence, method="sentence"):
    return {"cik": COMPANY.cik, "accession": "0000000123-24-000001", "filing_type": "8-K", "filed_at": "2024-02-01",
            "char_start": char_start, "char_end": char_start + len(sentence), "sentence": sentence, "text_version": 1,
            "capture_method": method, "heading": None, "lead_in": "Outlook:", "context_before": ["Before — it."], "context_after": [],
            "block_lines": 2 if method == "section" else None, "block_end": "gap" if method == "section" else None, "table_header": None}


def as_file(rows) -> str:
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)


def test_every_jsonl_list_reads_back_byte_for_byte(db, parts) -> None:
    draft, outcome = parts
    lists = {
        path("candidates", f"{COMPANY.cik}.jsonl"): [candidate(10, "Revenue is expected to be $65.0 billion."), candidate(90, "Line one\nLine  two ", "section")],
        path("drafts", f"{COMPANY.cik}.jsonl"): [{**draft, "parens_note": None, "item_index": 0, "evidence_lines": [1, 2]}, another(draft, "A")],
        path("outcomes", f"{COMPANY.cik}.jsonl"): [outcome, {"draft_id": draft["draft_id"][:-1] + "Z", "outcome": None, "outcome_reason": "period not closed", "acknowledgement": None, "withdrawal": None}],
        path("drafts", f"{COMPANY.cik}.rejects.jsonl"): [{"custom_id": "c-1", "item": {"value_low": 43.0, "plus_minus": None}, "reason": "value_low 43 is not in the evidence text"}],
        path("drafts", f"{COMPANY.cik}.skipped.jsonl"): [{"custom_id": "c-2", "reason": "sentence is 663 chars"}, {"custom_id": "c-3", "reason": "dup", "kept": {"text": "café"}}],
        path("drafts", f"{COMPANY.cik}.empty_blocks.jsonl"): [{"block_id": draft["draft_id"][:-1] + "E", "custom_id": "b-9", "reason": "the model returned no items"}],
        path("outcomes", f"{COMPANY.cik}.rejects.jsonl"): [{"draft_id": draft["draft_id"], "output": {"index": 2, "value": 3.8}, "reason": "cannot convert"}],
    }
    for p, rows in lists.items():
        assert not store.exists(p) and store.read_jsonl(p) == []
        store.write_jsonl(p, rows)
    for p, rows in lists.items():
        assert store.exists(p)
        assert as_file(store.read_jsonl(p)) == as_file(rows), p  # same rows, same key order, same number text
    store.write_jsonl(path("candidates", f"{COMPANY.cik}.jsonl"), [])
    assert not store.exists(path("candidates", f"{COMPANY.cik}.jsonl"))


def test_a_candidate_is_keyed_by_the_id_03_gives_it(db) -> None:
    store.write_jsonl(path("candidates", f"{COMPANY.cik}.jsonl"), [candidate(10, "A sentence."), candidate(10, "A block.", "section")])
    assert [r[0] for r in db._all("SELECT candidate_id FROM candidates ORDER BY seq")] == [
        "c-0000000123-000000012324000001-10", "b-0000000123-000000012324000001-10"]
    assert db._one("SELECT text FROM candidates WHERE seq = 0") == "A sentence."


def test_a_filing_sidecar_comes_back_as_it_went_in(db) -> None:
    meta = {"accession": "0000000123-24-000001", "bytes": 562832, "cik": COMPANY.cik, "content_sha256": "a" * 64, "content_type": "text/html",
            "doc_type": "EX-99.1", "fetched_at": "2026-10-01T12:38:47+00:00", "filed_at": "2024-02-01", "filing_type": "8-K",
            "final_url": "https://www.sec.gov/Archives/edgar/data/123/000000012324000001/release.htm", "http_status": 200,
            "items": ["2.02", "9.01"], "raw_sha256": "b" * 64, "requested_url": "https://www.sec.gov/x"}
    db.record_filing(meta)
    db.record_filing(meta)
    assert db.filings(COMPANY.cik) == [meta]
    with pytest.raises(store.StoreError, match="surprise"):
        db.record_filing({**meta, "surprise": 1})


# --- one setting ------------------------------------------------------------------------------------------------------


def test_a_stage_gives_the_same_rows_pointed_at_files_or_at_the_database(pg, parts, tmp_path, monkeypatch) -> None:
    draft, outcome = parts
    for name in ("DRAFTS_DIR", "OUTCOMES_DIR", "REVIEW_DIR", "RAW_DIR", "CANDIDATES_DIR"):
        monkeypatch.setattr(review, name, tmp_path / name.split("_")[0].lower())
    pg.upsert_company(COMPANY.cik, ticker=COMPANY.ticker, name=COMPANY.name)
    monkeypatch.setenv("DATABASE_URL", pg.url)
    monkeypatch.setenv("STORE_SCHEMA", pg.schema)
    seen = {}
    for setting in ("files", "postgres"):
        monkeypatch.setenv("STORE", setting)
        assert store.backend().name == setting
        store.write_jsonl(review.DRAFTS_DIR / f"{COMPANY.cik}.jsonl", [draft, another(draft, "A")])
        store.write_jsonl(review.OUTCOMES_DIR / f"{COMPANY.cik}.jsonl", [outcome])
        rows, stats = review.review_company(COMPANY, TODAY)
        again, stats_again = review.review_company(COMPANY, TODAY)
        assert stats["added"] == 2 and stats_again["added"] == 0 and again == rows
        attached, _ = review.attach_outcomes(COMPANY, TODAY)
        seen[setting] = (rows, attached, review.read_csv(review.REVIEW_DIR / f"{COMPANY.cik}.csv"))
    assert seen["files"] == seen["postgres"]
    assert (tmp_path / "review" / f"{COMPANY.cik}.csv").exists()            # the files run wrote a file
    assert pg._one("SELECT count(*) FROM statements WHERE queue_seq IS NOT NULL") == 2  # the postgres run wrote rows
    monkeypatch.setenv("STORE", "sqlite")
    with pytest.raises(store.StoreError, match="files or postgres"):
        store.backend()


# --- the model archive ------------------------------------------------------------------------------------------------

SCHEMA = {"type": "object", "properties": {"items": {"type": "array"}}, "required": ["items"], "additionalProperties": False}
CFG = llm.LlmConfig(model="claude-haiku-4-5", budget_usd=10.0, price_input=1.0, price_output=5.0, batch_discount=0.5)


def reqs(n: int) -> list[llm.LlmRequest]:
    return [llm.LlmRequest(f"r-{i}", "system text", f"user text {i}", 700, SCHEMA) for i in range(n)]


def run(client, requests, **kwargs):
    projection = llm.project(requests, [1000] * len(requests), exact=True, llm=CFG, expected_output_per_request=100)
    return llm.run_batch(client, CFG, "step", requests, projection=projection, canary_check=lambda text: None, sleep=lambda s: None,
                         log=lambda line: None, **kwargs)


@pytest.fixture
def batches(db, tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "BATCH_DIR", tmp_path / "batches")
    return db


def test_every_model_call_is_a_row_with_its_cost_and_its_answer(batches) -> None:
    client = fakes.FakeAnthropic()
    outcome = run(client, reqs(3), force_sync=True)
    assert len(outcome.results) == 3 and not (llm.BATCH_DIR).exists()  # nothing went to disk
    rows = batches._all("SELECT custom_id, kind, status, fingerprint, model, input_tokens, output_tokens, usd, response_text, response_sha256 FROM llm_calls ORDER BY id")
    assert [r[0] for r in rows] == ["r-0", "r-1", "r-2"]
    for (cid, kind, status, fp, model, tin, tout, usd, text, sha), req in zip(rows, reqs(3)):
        assert (kind, status, model, tin, tout) == ("sync", "succeeded", "claude-haiku-4-5", 1000, 100)
        assert fp == req.fingerprint(CFG.model) and float(usd) == pytest.approx(CFG.usd(1000, 100, batch=False))
        assert text == '{"items": []}' and sha == hashlib.sha256(text.encode()).hexdigest()
    assert llm.Ledger().committed_usd() == pytest.approx(3 * CFG.usd(1000, 100, batch=False))
    again = run(client, reqs(3), force_sync=True)  # answered already: nothing is sent, nothing is paid twice
    assert len(client.messages.create_calls) == 3 and len(again.results) == 3


def test_a_batch_is_held_at_its_worst_case_until_it_ends_and_is_resumed_not_resubmitted(batches) -> None:
    requests = reqs(5)
    slow = fakes.FakeAnthropic(batch_ready=False)
    clock = iter([0, 0, 10, 20, 9999, 9999, 9999])
    first = run(slow, requests, wait_seconds=30, clock=lambda: next(clock))
    worst = llm.project(requests, [1000] * 5, exact=True, llm=CFG, expected_output_per_request=100).usd_worst
    assert first.pending_batch == "msgbatch_1" and llm.RawArchive("step").pending()["requests"] == 4
    assert batches._one("SELECT count(*) FROM llm_calls WHERE status = 'pending'") == 4
    assert llm.Ledger().committed_usd() == pytest.approx(CFG.usd(1000, 100, batch=False) + worst)
    assert set(llm.RawArchive("step").load()) == {"r-0"}  # a pending request is not an answer

    slow.batch_ready = True
    second = run(slow, requests, wait_seconds=30)
    assert len(slow.messages.batches.created) == 1 and all(r.ok for r in second.results.values()) and len(second.results) == 5
    assert llm.RawArchive("step").pending() is None and batches._one("SELECT count(*) FROM llm_calls WHERE status = 'pending'") == 0
    assert llm.Ledger().committed_usd() == pytest.approx(CFG.usd(1000, 100, batch=False) + 4 * CFG.usd(1000, 100))
    assert batches._one("SELECT count(*) FROM llm_calls WHERE kind = 'batch' AND batch_id = 'msgbatch_1'") == 4


def test_token_counts_are_kept_by_fingerprint_and_asked_for_once(batches) -> None:
    client = fakes.FakeAnthropic(input_tokens=321)
    assert llm.count_input_tokens(client, CFG, reqs(4)) == [321] * 4
    assert llm.count_input_tokens(client, CFG, reqs(4)) == [321] * 4
    assert client.messages.count_calls == 4
    known = batches.priced([r.fingerprint(CFG.model) for r in reqs(4)] + ["f" * 64])
    assert len(known) == 4 and all(v == {"answered": False, "input_tokens": 321, "usd": 0.0} for v in known.values())


def test_the_review_csv_written_from_the_database_is_the_file(db, parts, tmp_path) -> None:
    draft, outcome = parts
    rows = [queue_row(draft, outcome, reviewer_note='said "about" $5, twice\nsecond line'), queue_row(another(draft, "A"))]
    review.write_csv(REVIEW, rows)
    with store.use(store.FilesBackend()):
        review.write_csv(tmp_path / "review" / REVIEW.name, rows)
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=review.COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(review.read_csv(REVIEW))
    assert out.getvalue() == (tmp_path / "review" / REVIEW.name).read_text(encoding="utf-8")
