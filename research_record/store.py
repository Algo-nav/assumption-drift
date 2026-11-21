"""Where the pipeline's rows live: per-CIK files, or Postgres.

    STORE=files      (the default) data/candidates/{cik}.jsonl, data/drafts/, data/outcomes/, data/review/{cik}.csv,
                     data/batches/: exactly the files the stages have always read and written.
    STORE=postgres   the same rows in the tables of db/schema.sql. DATABASE_URL names the database (the environment,
                     or a DATABASE_URL= line in the git-ignored .env at the repo root); STORE_SCHEMA names the schema
                     (default: public).

The stages keep calling the functions they always called (`read_csv`, `write_csv`, `read_jsonl`, `write_jsonl`,
`RawArchive.load`, ...), with the same path arguments. Those functions now end here, and the path says which rows are
meant: `.../review/0000796343.csv` is that company's review queue whichever store is in use. A path this module has no
table for is an error under STORE=postgres, never a silent write to disk.

Every value goes in and comes out unchanged. A review-queue cell that a typed column could not give back exactly as
it was written ("1e3" in a number column, a date that is not a date) is refused with the row and the column named:
nothing is rounded, trimmed or normalised to make it fit.

What stays on disk under either setting: the cached filings themselves (data/raw/{cik}/{accession}.html and its
sidecar). They are a re-fetchable cache of documents, not rows; the `filings` table records each one.

db/README.md maps every CSV and JSONL column to its table and column.
"""

from __future__ import annotations

import contextlib
import contextvars
import csv
import hashlib
import json
import os
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator

__all__ = [
    "StoreError", "backend", "use", "kind", "FilesBackend", "PostgresBackend", "REVIEW_COLUMNS", "review_decision",
    "read_jsonl", "write_jsonl", "read_csv", "write_csv", "exists", "apply_schema", "SCHEMA_PATH",
]

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "db" / "schema.sql"


class StoreError(RuntimeError):
    """The store was asked for something it cannot do without losing or changing data."""


# --- the review queue's columns ------------------------------------------------------------------------------------

#: (CSV column, statements column, kind) for every column of data/review/{cik}.csv, in the CSV's own order. A CSV
#: column that is not here has nowhere to go, and writing it is refused (see `PostgresBackend.write_csv`).
REVIEW_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("record_id", "record_id", "text"),
    ("company", "company", "text"),
    ("ticker", "ticker", "text"),
    ("cik", "cik", "text"),
    ("claim", "claim", "text"),
    ("assumption.text", "assumption_text", "text"),
    ("assumption.metric", "metric", "text"),
    ("assumption.target_low", "target_low", "number"),
    ("assumption.target_high", "target_high", "number"),
    ("assumption.unit", "unit", "text"),
    ("assumption.target_period", "target_period", "text"),
    ("assumption.stated_at", "stated_at", "date"),
    ("assumption.evidence.source_url", "assumption_source_url", "text"),
    ("assumption.evidence.accession_number", "assumption_accession", "text"),
    ("assumption.evidence.filing_type", "assumption_filing_type", "text"),
    ("assumption.evidence.filed_at", "assumption_filed_at", "date"),
    ("assumption.evidence.fetched_at", "assumption_fetched_at", "timestamp"),
    ("assumption.evidence.content_sha256", "assumption_content_sha256", "text"),
    ("assumption.evidence.excerpt", "assumption_excerpt", "text"),
    ("outcome.reported_value", "outcome_reported_value", "number"),
    ("outcome.reported_at", "outcome_reported_at", "date"),
    ("outcome.evidence.source_url", "outcome_source_url", "text"),
    ("outcome.evidence.accession_number", "outcome_accession", "text"),
    ("outcome.evidence.filing_type", "outcome_filing_type", "text"),
    ("outcome.evidence.filed_at", "outcome_filed_at", "date"),
    ("outcome.evidence.fetched_at", "outcome_fetched_at", "timestamp"),
    ("outcome.evidence.content_sha256", "outcome_content_sha256", "text"),
    ("outcome.evidence.excerpt", "outcome_excerpt", "text"),
    ("invalidation_condition", "invalidation_condition", "text"),
    ("status", "status", "text"),
    ("acknowledged_at", "acknowledged_at", "date"),
    ("acknowledgement_evidence.source_url", "ack_source_url", "text"),
    ("acknowledgement_evidence.accession_number", "ack_accession", "text"),
    ("acknowledgement_evidence.filing_type", "ack_filing_type", "text"),
    ("acknowledgement_evidence.filed_at", "ack_filed_at", "date"),
    ("acknowledgement_evidence.fetched_at", "ack_fetched_at", "timestamp"),
    ("acknowledgement_evidence.content_sha256", "ack_content_sha256", "text"),
    ("acknowledgement_evidence.excerpt", "ack_excerpt", "text"),
    ("days_to_falsifiable", "days_to_falsifiable", "int"),
    ("days_to_acknowledged", "days_to_acknowledged", "int"),
    ("last_reviewed_at", "last_reviewed_at", "date"),
    ("reviewer", "reviewer", "text"),
    ("approved", "approved", "bool"),
    ("hand_verified", "hand_verified", "bool"),
    ("reviewer_note", "reviewer_note", "text"),
    ("conflict", "conflict", "bool"),
    ("empty_block", "empty_block", "bool"),
    ("ack_pending_review", "ack_pending_review", "bool"),
    ("change_pending_review", "change_pending_review", "bool"),
    ("aid_proposed_status", "aid_proposed_status", "text"),
    ("aid_capture_method", "aid_capture_method", "text"),
    ("aid_heading", "aid_heading", "text"),
    ("aid_lead_in", "aid_lead_in", "text"),
    ("aid_table_header", "aid_table_header", "text"),
    ("aid_value_column", "aid_value_column", "text"),
    ("aid_outcome_note", "aid_outcome_note", "text"),
    ("aid_flag_note", "aid_flag_note", "text"),
    ("aid_withdrawal_note", "aid_withdrawal_note", "text"),
    ("aid_verify", "aid_verify", "text"),
    ("aid_verify_reason", "aid_verify_reason", "text"),
    ("aid_verify_class", "aid_verify_class", "text"),
    ("aid_suggested_note", "aid_suggested_note", "text"),
    ("aid_ack_proposed_at", "aid_ack_proposed_at", "text"),
    ("aid_ack_proposed_excerpt", "aid_ack_proposed_excerpt", "text"),
    ("aid_ack_proposed_url", "aid_ack_proposed_url", "text"),
    ("aid_ack_proposed_evidence", "aid_ack_proposed_evidence", "text"),
    ("aid_proposed_change", "aid_proposed_change", "text"),
    ("aid_derived_snapshot", "aid_derived_snapshot", "text"),
    ("aid_context", "aid_context", "text"),
)
_REVIEW_BY_CSV = {c: (db, k) for c, db, k in REVIEW_COLUMNS}

_TIMESTAMP = re.compile(r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)Z$")
_PLAIN_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")
_BOOLS = {"true": True, "false": False}


def _to_db(text: str, kind: str) -> Any:
    """A CSV cell as the value its column holds. Raises ValueError if the column could not give the cell back exactly."""
    if text == "":
        return None
    if kind == "text":
        return text
    if kind == "number":
        if not _PLAIN_NUMBER.match(text):
            raise ValueError("not a plain decimal number")
        try:
            return Decimal(text)
        except InvalidOperation as exc:
            raise ValueError("not a number") from exc
    if kind == "int":
        if str(int(text)) != text:
            raise ValueError("not a plain integer")
        return int(text)
    if kind == "date":
        value = date.fromisoformat(text)
        if value.isoformat() != text:
            raise ValueError("not a YYYY-MM-DD date")
        return value
    if kind == "timestamp":
        m = _TIMESTAMP.match(text)
        if not m:
            raise ValueError("not a YYYY-MM-DDTHH:MM:SSZ timestamp")
        return datetime(*(int(g) for g in m.groups()), tzinfo=timezone.utc)
    if kind == "bool":
        if text not in _BOOLS:
            raise ValueError("not true, false or empty")
        return _BOOLS[text]
    raise AssertionError(kind)


def _to_csv(value: Any, kind: str) -> str:
    if value is None:
        return ""
    if kind == "number":
        return format(value, "f")
    if kind == "timestamp":
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if kind == "date":
        return value.isoformat()
    if kind == "bool":
        return "true" if value else "false"
    return str(value)


def review_decision(row: dict[str, str]) -> str | None:
    """The reviewer's decision on a review-queue row, read off the row's own columns:

        v  approved, and hand-verified against EDGAR
        y  approved
        n  not approved, with a reviewer note saying why (a rejection)
        None  nothing decided

    An outlook-block flag (`empty_block=true`) is not a statement and has no decision. `s`, a row looked at and held,
    leaves no trace in the CSV: the loader sets it (see scripts/load_published.py) and a later write keeps it for as
    long as the row stays undecided."""
    if row.get("empty_block") == "true":
        return None
    if row.get("approved") == "true":
        return "v" if row.get("hand_verified") == "true" else "y"
    if (row.get("reviewer_note") or "").strip():
        return "n"
    return None


def _published_status(row: dict[str, str]) -> str | None:
    """The status a release row built from this queue row carries: the rubric's answer for an approved row, the same
    call pipeline/06_publish.py makes. None for a row that is not approved, is a flag, or does not parse as a record."""
    if row.get("approved") != "true" or row.get("empty_block") == "true":
        return None
    from pydantic import ValidationError

    from research_record import rubric
    from research_record.schema import ResearchRecord
    from research_record.validate import unflatten_csv_row

    try:
        record = ResearchRecord.model_validate(unflatten_csv_row(row))
    except (ValidationError, KeyError, ValueError):
        return None
    return rubric.resolve_record(record, reviewed=True, withdrawn=bool((row.get("aid_withdrawal_note") or "").strip()))


# --- which rows a path means ----------------------------------------------------------------------------------------

_CIK_FILE = re.compile(r"^(\d{10})(?:\.([a-z_]+))?\.(jsonl|csv)$")
#: The first keys of a candidate row, in the order 02_candidates writes them; each has its own column.
_CANDIDATE_KEYS = ("cik", "accession", "filing_type", "filed_at", "char_start", "char_end", "sentence", "text_version", "capture_method")
_DIAGNOSTICS = {("drafts", "rejects"), ("drafts", "skipped"), ("drafts", "empty_blocks"), ("outcomes", "rejects")}
_CANDIDATE_CIK = re.compile(r"^[cb]-(\d{10})-")
_SIDECAR_KEYS = frozenset({"accession", "bytes", "cik", "content_sha256", "content_type", "doc_type", "fetched_at", "filed_at",
                           "filing_type", "final_url", "http_status", "items", "raw_sha256", "requested_url"})


def _classify(path: Path) -> tuple[str, str]:
    """(dataset, key) for a path the stages use. dataset is one of candidates, drafts, outcomes, review,
    diagnostics:<source>, archive, state, ledger, token_cache, spend; key is the cik or the step."""
    path = Path(path)
    folder, name = path.parent.name, path.name
    m = _CIK_FILE.match(name)
    if m:
        cik, sub, ext = m.groups()
        if folder == "review" and ext == "csv" and sub is None:
            return "review", cik
        if ext == "jsonl" and folder in ("candidates", "drafts", "outcomes"):
            if sub is None:
                return folder, cik
            if (folder, sub) in _DIAGNOSTICS:
                return f"diagnostics:{folder}.{sub}", cik
    if name.endswith(".raw.jsonl"):
        return "archive", name[: -len(".raw.jsonl")]
    if name.endswith(".state.json"):
        return "state", name[: -len(".state.json")]
    if name == "ledger.jsonl":
        return "ledger", ""
    if name == "token_cache.json":
        return "token_cache", ""
    if name == "spend_ledger.jsonl":
        return "spend", ""
    raise StoreError(f"{path}: STORE=postgres has no table for this path")


def _json_param(value: Any) -> Any:
    from psycopg.types.json import Json

    return Json(value, dumps=lambda o: json.dumps(o, ensure_ascii=False))


def _call_kind(batch_id: str | None) -> str:
    return batch_id if batch_id in ("sync", "canary") else "batch"


# --- files ------------------------------------------------------------------------------------------------------------


class FilesBackend:
    """The files, read and written exactly as the stages did before there was a second store."""

    name = "files"

    def exists(self, path: Path) -> bool:
        return Path(path).exists()

    def read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        path = Path(path)
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def write_jsonl(self, path: Path, rows: list[dict[str, Any]], *, atomic: bool = False) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        if not atomic:
            path.write_text(text, encoding="utf-8")
            return
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    def read_csv(self, path: Path) -> list[dict[str, str]]:
        with Path(path).open(newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def write_csv(self, path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp, path)

    # The model archive, its pending-batch state, the ledgers and the token cache (pipeline/llm.py).

    def archive_read(self, path: Path) -> list[dict[str, Any]]:
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        return [json.loads(line) for line in lines if line.strip()]

    def archive_append(self, path: Path, rows: list[dict[str, Any]], *, model: str | None = None,
                       costs: dict[str, float] | None = None) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def state_read(self, path: Path) -> dict[str, Any] | None:
        path = Path(path)
        return json.loads(path.read_text()) if path.exists() else None

    def state_write(self, path: Path, payload: dict[str, Any], *, model: str | None = None) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))

    def state_clear(self, path: Path) -> None:
        Path(path).unlink(missing_ok=True)

    def ledger_append(self, path: Path, entry: dict[str, Any]) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    def ledger_entries(self, path: Path) -> list[dict[str, Any]]:
        try:
            return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
        except FileNotFoundError:
            return []

    def token_cache_read(self, path: Path) -> dict[str, int]:
        try:
            return json.loads(Path(path).read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def token_cache_write(self, path: Path, cache: dict[str, int], *, new: set[str] | None = None, model: str | None = None) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache))

    def spend_append(self, path: Path, entry: dict[str, Any]) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    def record_filing(self, meta: dict[str, Any]) -> None:
        """The sidecar beside the cached document is the record; 01_fetch has already written it."""


# --- postgres ---------------------------------------------------------------------------------------------------------


def database_url() -> str:
    """DATABASE_URL from the environment, or from a DATABASE_URL= line in the git-ignored .env at the repo root. The
    value is never printed."""
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    env = REPO_ROOT / ".env"
    if env.exists():
        for raw in env.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("DATABASE_URL="):
                value = line.split("=", 1)[1].strip().strip("'\"")
                if value:
                    return value
    raise StoreError("STORE=postgres needs DATABASE_URL (in the environment, or in .env at the repo root)")


_SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class PostgresBackend:
    """The tables of db/schema.sql, in one schema of one database."""

    name = "postgres"

    def __init__(self, url: str | None = None, schema: str | None = None) -> None:
        import psycopg

        self.schema = schema or os.environ.get("STORE_SCHEMA") or "public"
        if not _SCHEMA_NAME.match(self.schema):
            raise StoreError(f"STORE_SCHEMA {self.schema!r} is not a plain lower-case schema name")
        self.url = url or database_url()
        self.conn = psycopg.connect(self.url, autocommit=True)
        self.conn.execute(f'SET search_path TO "{self.schema}"')
        #: Set by pipeline.job for the length of a job: every model call recorded meanwhile carries them.
        self.job_id: int | None = None
        self.job_cik: str | None = None

    def close(self) -> None:
        self.conn.close()

    # -- schema --

    def apply_schema(self, sql_path: Path | None = None) -> None:
        sql = (sql_path or SCHEMA_PATH).read_text(encoding="utf-8")
        with self.conn.transaction():
            self.conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')
            self.conn.execute(f'SET LOCAL search_path TO "{self.schema}"')
            self.conn.execute(sql)

    def drop_schema(self) -> None:
        """Drop this backend's schema and everything in it. Refused for `public`: that one is never dropped from here."""
        if self.schema == "public":
            raise StoreError("refusing to drop the public schema")
        self.conn.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')

    def _all(self, sql: str, params: tuple = ()) -> list[tuple]:
        return self.conn.execute(sql, params).fetchall()

    def _one(self, sql: str, params: tuple = ()) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        return row[0] if row else None

    # -- rows the stages read and write --

    def exists(self, path: Path) -> bool:
        dataset, key = _classify(Path(path))
        if dataset == "review":
            return bool(self._one("SELECT EXISTS (SELECT 1 FROM statements WHERE cik = %s AND queue_seq IS NOT NULL)", (key,)))
        if dataset == "candidates":
            return bool(self._one("SELECT EXISTS (SELECT 1 FROM candidates WHERE cik = %s)", (key,)))
        if dataset == "drafts":
            return bool(self._one("SELECT EXISTS (SELECT 1 FROM statements WHERE cik = %s AND draft_seq IS NOT NULL)", (key,)))
        if dataset == "outcomes":
            return bool(self._one("SELECT EXISTS (SELECT 1 FROM statements WHERE cik = %s AND outcome_seq IS NOT NULL)", (key,)))
        if dataset.startswith("diagnostics:"):
            return bool(self._one("SELECT EXISTS (SELECT 1 FROM stage_diagnostics WHERE cik = %s AND source = %s)",
                                  (key, dataset.split(":", 1)[1])))
        if dataset == "state":
            return self.state_read(path) is not None
        raise StoreError(f"{path}: exists() is not defined for {dataset} rows")

    def read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        dataset, cik = _classify(Path(path))
        if dataset == "candidates":
            rows = self._all(
                "SELECT cik, accession, filing_type, filed_at, char_start, char_end, text, text_version, capture_method, meta "
                "FROM candidates WHERE cik = %s ORDER BY seq", (cik,))
            return [{**dict(zip(_CANDIDATE_KEYS, (r[0], r[1], r[2], r[3].isoformat(), *r[4:9]))), **r[9]} for r in rows]
        if dataset == "drafts":
            return [r[0] for r in self._all("SELECT draft FROM statements WHERE cik = %s AND draft_seq IS NOT NULL ORDER BY draft_seq", (cik,))]
        if dataset == "outcomes":
            return [r[0] for r in self._all("SELECT outcome_found FROM statements WHERE cik = %s AND outcome_seq IS NOT NULL ORDER BY outcome_seq", (cik,))]
        if dataset.startswith("diagnostics:"):
            return [r[0] for r in self._all("SELECT body FROM stage_diagnostics WHERE cik = %s AND source = %s ORDER BY seq",
                                            (cik, dataset.split(":", 1)[1]))]
        raise StoreError(f"{path}: read_jsonl() is not defined for {dataset} rows")

    def write_jsonl(self, path: Path, rows: list[dict[str, Any]], *, atomic: bool = False) -> None:
        """Replace the whole list the path names, as writing the file did. One transaction: all of it or none."""
        dataset, cik = _classify(Path(path))
        with self.conn.transaction():
            if dataset == "candidates":
                self._write_candidates(cik, rows)
            elif dataset in ("drafts", "outcomes"):
                self._write_statement_json(cik, rows, *(("draft", "draft_seq") if dataset == "drafts" else ("outcome_found", "outcome_seq")))
            elif dataset.startswith("diagnostics:"):
                self._write_diagnostics(cik, dataset.split(":", 1)[1], rows)
            else:
                raise StoreError(f"{path}: write_jsonl() is not defined for {dataset} rows")

    def _write_candidates(self, cik: str, rows: list[dict[str, Any]]) -> None:
        params = []
        seen: set[str] = set()
        for seq, row in enumerate(rows):
            if tuple(row)[: len(_CANDIDATE_KEYS)] != _CANDIDATE_KEYS:
                raise StoreError(f"candidate {seq} of {cik}: its keys are not in the layout the candidates table holds: {list(row)}")
            if row["cik"] != cik:
                raise StoreError(f"candidate {seq}: cik {row['cik']!r} written to {cik}'s list")
            if date.fromisoformat(row["filed_at"]).isoformat() != row["filed_at"]:
                raise StoreError(f"candidate {seq} of {cik}: filed_at {row['filed_at']!r} is not a YYYY-MM-DD date")
            cid = f"{'b' if row['capture_method'] == 'section' else 'c'}-{cik}-{row['accession'].replace('-', '')}-{row['char_start']}"
            if cid in seen:
                raise StoreError(f"candidate {cid} appears twice in {cik}'s list")
            seen.add(cid)
            meta = {k: v for k, v in row.items() if k not in _CANDIDATE_KEYS}
            params.append((cid, cik, row["accession"], seq, row["filing_type"], row["filed_at"], row["capture_method"],
                           row["char_start"], row["char_end"], row["sentence"], row["text_version"], _json_param(meta)))
        self.conn.execute("DELETE FROM candidates WHERE cik = %s", (cik,))
        if params:
            with self.conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO candidates (candidate_id, cik, accession, seq, filing_type, filed_at, capture_method, char_start, "
                    "char_end, text, text_version, meta) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", params)

    def _write_statement_json(self, cik: str, rows: list[dict[str, Any]], column: str, seq_column: str) -> None:
        ids = [r["draft_id"] for r in rows]
        if len(set(ids)) != len(ids):
            raise StoreError(f"{cik}: a draft_id appears twice in the {column} rows; statements holds one row per record")
        self.conn.execute(f"UPDATE statements SET {column} = NULL, {seq_column} = NULL WHERE cik = %s AND {seq_column} IS NOT NULL", (cik,))
        if rows:
            with self.conn.cursor() as cur:
                cur.executemany(
                    f"INSERT INTO statements (record_id, cik, {column}, {seq_column}) VALUES (%s, %s, %s, %s) "
                    f"ON CONFLICT (record_id) DO UPDATE SET {column} = EXCLUDED.{column}, {seq_column} = EXCLUDED.{seq_column} "
                    "WHERE statements.cik = EXCLUDED.cik",
                    [(r["draft_id"], cik, _json_param(r), seq) for seq, r in enumerate(rows)])
            written = self._one(f"SELECT count(*) FROM statements WHERE cik = %s AND {seq_column} IS NOT NULL", (cik,))
            if written != len(rows):
                raise StoreError(f"{cik}: {len(rows) - written} of the {column} rows carry a record_id that belongs to another company")
        self._drop_empty_statements(cik)

    def _drop_empty_statements(self, cik: str) -> None:
        """A statement that is no longer a draft, has nothing from 04 and is not in the queue is nothing at all."""
        self.conn.execute("DELETE FROM statements WHERE cik = %s AND queue_seq IS NULL AND draft_seq IS NULL AND outcome_seq IS NULL", (cik,))

    def _write_diagnostics(self, cik: str, source: str, rows: list[dict[str, Any]]) -> None:
        self.conn.execute("DELETE FROM stage_diagnostics WHERE cik = %s AND source = %s", (cik, source))
        if rows:
            with self.conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO stage_diagnostics (cik, source, seq, custom_id, record_id, reason, body) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    [(cik, source, seq, r.get("custom_id"), r.get("draft_id") or r.get("block_id"), r.get("reason"), _json_param(r))
                     for seq, r in enumerate(rows)])

    def read_csv(self, path: Path) -> list[dict[str, str]]:
        dataset, cik = _classify(Path(path))
        if dataset != "review":
            raise StoreError(f"{path}: read_csv() is only defined for a review queue")
        columns = ", ".join(db for _, db, _ in REVIEW_COLUMNS)
        rows = self._all(f"SELECT {columns} FROM statements WHERE cik = %s AND queue_seq IS NOT NULL ORDER BY queue_seq", (cik,))
        if not rows:
            raise FileNotFoundError(f"{path}: no review rows for {cik} in the database")
        return [{c: _to_csv(v, k) for (c, _, k), v in zip(REVIEW_COLUMNS, r)} for r in rows]

    def write_csv(self, path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
        """Replace the company's review queue with `rows`, in that order. A statement that leaves the queue keeps its
        draft and what 04 found, and loses its review columns."""
        dataset, cik = _classify(Path(path))
        if dataset != "review":
            raise StoreError(f"{path}: write_csv() is only defined for a review queue")
        unmapped = [c for c in fieldnames if c not in _REVIEW_BY_CSV]
        if unmapped:
            raise StoreError(f"review column(s) with no column in statements: {', '.join(unmapped)}. Add them to db/schema.sql and "
                             "research_record.store.REVIEW_COLUMNS; they are not dropped.")
        db_columns = [db for _, db, _ in REVIEW_COLUMNS]
        params = []
        seen: set[str] = set()
        for seq, row in enumerate(rows):
            rid = row.get("record_id", "")
            extra = [c for c in row if c not in _REVIEW_BY_CSV]
            if extra:
                raise StoreError(f"{rid}: column(s) with no column in statements: {', '.join(extra)}")
            if row.get("cik") != cik:
                raise StoreError(f"{rid}: cik {row.get('cik')!r} written to {cik}'s review queue")
            if rid in seen:
                raise StoreError(f"{rid} appears twice in {cik}'s review queue")
            seen.add(rid)
            values = []
            for column, _, kind in REVIEW_COLUMNS:
                text = row.get(column, "") or ""
                try:
                    values.append(_to_db(text, kind))
                except ValueError as exc:
                    raise StoreError(f"{rid}: column {column}: {text!r} cannot be stored and given back unchanged ({exc})") from exc
            decision = review_decision(row)
            params.append((*values, decision, _published_status(row), seq))
        assignments = ", ".join(f"{c} = EXCLUDED.{c}" for c in db_columns if c not in ("record_id", "cik"))
        held = "(EXCLUDED.review_decision IS NULL AND statements.review_decision = 's' AND NOT COALESCE(EXCLUDED.empty_block, false))"
        with self.conn.transaction():
            self.conn.execute("UPDATE statements SET queue_seq = NULL WHERE cik = %s AND queue_seq IS NOT NULL", (cik,))
            if params:
                with self.conn.cursor() as cur:
                    cur.executemany(
                        f"INSERT INTO statements ({', '.join(db_columns)}, review_decision, published_status, queue_seq, review_tier) "
                        f"VALUES ({', '.join(['%s'] * (len(db_columns) + 3))}, NULL) "
                        f"ON CONFLICT (record_id) DO UPDATE SET {assignments}, published_status = EXCLUDED.published_status, "
                        f"queue_seq = EXCLUDED.queue_seq, "
                        f"review_decision = CASE WHEN {held} THEN 's' ELSE EXCLUDED.review_decision END "
                        "WHERE statements.cik = EXCLUDED.cik", params)
                written = self._one("SELECT count(*) FROM statements WHERE cik = %s AND queue_seq IS NOT NULL", (cik,))
                if written != len(params):
                    raise StoreError(f"{cik}: {len(params) - written} review row(s) carry a record_id that belongs to another company")
            # Every decided row here was decided by a person; nothing in the files says otherwise.
            self.conn.execute(
                "UPDATE statements SET review_tier = CASE WHEN review_decision IS NOT NULL THEN 'hand' END "
                "WHERE cik = %s AND queue_seq IS NOT NULL AND review_tier IS DISTINCT FROM CASE WHEN review_decision IS NOT NULL THEN 'hand' END "
                "AND review_tier IS DISTINCT FROM 'machine'", (cik,))
            blank = ", ".join(f"{c} = NULL" for c in db_columns if c not in ("record_id", "cik"))
            self.conn.execute(
                f"UPDATE statements SET {blank}, review_decision = NULL, review_tier = NULL, published_status = NULL "
                "WHERE cik = %s AND queue_seq IS NULL", (cik,))
            self._drop_empty_statements(cik)

    # -- filings --

    def record_filing(self, meta: dict[str, Any]) -> None:
        unknown = set(meta) - _SIDECAR_KEYS
        if unknown:
            raise StoreError(f"{meta.get('accession')}: sidecar field(s) with no column in filings: {', '.join(sorted(unknown))}")
        self.conn.execute(
            "INSERT INTO filings (accession, cik, form, filed_at, url, fetched_at, requested_url, doc_type, items, http_status, "
            "content_type, bytes, raw_sha256, content_sha256) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (accession) DO UPDATE SET cik = EXCLUDED.cik, form = EXCLUDED.form, filed_at = EXCLUDED.filed_at, "
            "url = EXCLUDED.url, fetched_at = EXCLUDED.fetched_at, requested_url = EXCLUDED.requested_url, doc_type = EXCLUDED.doc_type, "
            "items = EXCLUDED.items, http_status = EXCLUDED.http_status, content_type = EXCLUDED.content_type, bytes = EXCLUDED.bytes, "
            "raw_sha256 = EXCLUDED.raw_sha256, content_sha256 = EXCLUDED.content_sha256",
            (meta["accession"], meta["cik"], meta["filing_type"], meta["filed_at"], meta["final_url"],
             datetime.fromisoformat(meta["fetched_at"]), meta.get("requested_url"), meta.get("doc_type"),
             _json_param(meta["items"]) if meta.get("items") is not None else None, meta.get("http_status"), meta.get("content_type"),
             meta.get("bytes"), meta.get("raw_sha256"), meta.get("content_sha256")))

    def filings(self, cik: str) -> list[dict[str, Any]]:
        """The company's filings as sidecar dicts, oldest first: what was passed to `record_filing`."""
        rows = self._all(
            "SELECT accession, bytes, cik, content_sha256, content_type, doc_type, fetched_at, filed_at, form, url, http_status, items, "
            "raw_sha256, requested_url FROM filings WHERE cik = %s ORDER BY filed_at, accession", (cik,))
        keys = ("accession", "bytes", "cik", "content_sha256", "content_type", "doc_type", "fetched_at", "filed_at", "filing_type",
                "final_url", "http_status", "items", "raw_sha256", "requested_url")
        out = []
        for r in rows:
            meta = dict(zip(keys, r))
            meta["fetched_at"] = meta["fetched_at"].astimezone(timezone.utc).isoformat(timespec="seconds")
            meta["filed_at"] = meta["filed_at"].isoformat()
            out.append(meta)
        return out

    # -- the model archive and its accounting (pipeline/llm.py) --

    def archive_read(self, path: Path) -> list[dict[str, Any]]:
        _, step = _classify(Path(path))
        rows = self._all(
            "SELECT custom_id, status, response_text, input_tokens, output_tokens, error, batch_id, fingerprint FROM llm_calls "
            "WHERE step = %s AND kind <> 'count' AND status <> 'pending' ORDER BY id", (step,))
        keys = ("custom_id", "status", "text", "input_tokens", "output_tokens", "error", "batch_id", "fingerprint")
        return [dict(zip(keys, r)) for r in rows]

    def _call_params(self, step: str, row: dict[str, Any], model: str | None, usd: float, created_at: Any = None,
                     archive_seq: int | None = None) -> tuple:
        text = row.get("text")
        m = _CANDIDATE_CIK.match(row["custom_id"] or "")
        return (step, row["custom_id"], m.group(1) if m else self.job_cik, self.job_id, row.get("fingerprint"), model,
                _call_kind(row.get("batch_id")), row["status"], row.get("batch_id"), row.get("fingerprint"),
                hashlib.sha256(text.encode("utf-8")).hexdigest() if text is not None else None, text, row.get("error"),
                row.get("input_tokens", 0), row.get("output_tokens", 0), Decimal(repr(float(usd))), created_at, archive_seq)

    _CALL_INSERT = (
        "INSERT INTO llm_calls (step, custom_id, cik, job_id, fingerprint, model, kind, status, batch_id, request_sha256, "
        "response_sha256, response_text, error, input_tokens, output_tokens, usd, created_at, archive_seq) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, now()), %s)")

    def archive_append(self, path: Path, rows: list[dict[str, Any]], *, model: str | None = None,
                       costs: dict[str, float] | None = None) -> None:
        """One llm_calls row per answer, each with what it cost. The answer text is stored as it came back."""
        _, step = _classify(Path(path))
        costs = costs or {}
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.executemany(self._CALL_INSERT, [self._call_params(step, r, model, costs.get(r["custom_id"], 0.0)) for r in rows])

    def load_archive(self, step: str, rows: list[dict[str, Any]], *, model: str | None, costs: list[float],
                     created: list[datetime | None]) -> None:
        """The lines of data/batches/{step}.raw.jsonl, one llm_calls row each, keyed by line number so loading the same
        file again changes nothing. `costs` and `created` are per line."""
        params = []
        for seq, (row, usd, when) in enumerate(zip(rows, costs, created, strict=True)):
            unknown = set(row) - {"custom_id", "status", "text", "input_tokens", "output_tokens", "error", "batch_id", "fingerprint"}
            if unknown:
                raise StoreError(f"{step} line {seq + 1}: archive field(s) with no column in llm_calls: {', '.join(sorted(unknown))}")
            p = self._call_params(step, row, model, usd, when, seq)
            params.append((*p[:3], *p[4:]))  # no job: these calls were made before there were jobs
        if not params:
            return
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO llm_calls (step, custom_id, cik, fingerprint, model, kind, status, batch_id, request_sha256, "
                "response_sha256, response_text, error, input_tokens, output_tokens, usd, created_at, archive_seq) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (step, archive_seq) WHERE archive_seq IS NOT NULL DO UPDATE SET custom_id = EXCLUDED.custom_id, "
                "cik = COALESCE(EXCLUDED.cik, llm_calls.cik), fingerprint = EXCLUDED.fingerprint, model = EXCLUDED.model, kind = EXCLUDED.kind, "
                "status = EXCLUDED.status, batch_id = EXCLUDED.batch_id, request_sha256 = EXCLUDED.request_sha256, "
                "response_sha256 = EXCLUDED.response_sha256, response_text = EXCLUDED.response_text, error = EXCLUDED.error, "
                "input_tokens = EXCLUDED.input_tokens, output_tokens = EXCLUDED.output_tokens, usd = EXCLUDED.usd, "
                "created_at = EXCLUDED.created_at", params)

    def attribute_calls(self) -> int:
        """Fill llm_calls.cik where the custom_id names a statement ({v|o|k|w}-{record_id}). How many rows it set."""
        return self.conn.execute(
            "UPDATE llm_calls c SET cik = s.cik FROM statements s WHERE c.cik IS NULL AND c.custom_id ~ '^[vokw]-' "
            "AND s.record_id = substr(c.custom_id, 3)").rowcount

    def state_read(self, path: Path) -> dict[str, Any] | None:
        """The step's batch that has been submitted and has not ended, in the shape of the {step}.state.json file."""
        _, step = _classify(Path(path))
        rows = self._all("SELECT batch_id, custom_id, fingerprint FROM llm_calls WHERE step = %s AND status = 'pending' ORDER BY id", (step,))
        if not rows:
            return None
        return {"batch_id": rows[0][0], "requests": len(rows), "fingerprints": {cid: fp for _, cid, fp in rows}}

    def state_write(self, path: Path, payload: dict[str, Any], *, model: str | None = None) -> None:
        _, step = _classify(Path(path))
        rows = [{"custom_id": cid, "status": "pending", "batch_id": payload["batch_id"], "fingerprint": fp}
                for cid, fp in payload["fingerprints"].items()]
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.executemany(self._CALL_INSERT, [self._call_params(step, r, model, 0.0) for r in rows])

    def state_clear(self, path: Path) -> None:
        _, step = _classify(Path(path))
        self.conn.execute("DELETE FROM llm_calls WHERE step = %s AND status = 'pending'", (step,))

    def ledger_append(self, path: Path, entry: dict[str, Any]) -> None:
        """Each call's cost is already on its llm_calls row. The one thing a ledger line adds is the worst case a
        submitted batch is held at until it ends: it is spread over the batch's pending rows."""
        if entry.get("kind") != "submitted":
            return
        n = self._one("SELECT count(*) FROM llm_calls WHERE batch_id = %s AND status = 'pending'", (entry["batch_id"],))
        if n:
            self.conn.execute("UPDATE llm_calls SET usd = %s WHERE batch_id = %s AND status = 'pending'",
                              (Decimal(repr(float(entry["usd"]))) / n, entry["batch_id"]))

    def ledger_entries(self, path: Path) -> list[dict[str, Any]]:
        """The ledger pipeline.llm.Ledger reads, rebuilt from llm_calls: one line per canary or synchronous call, one
        `submitted` line per batch still pending and one `ended` line per batch that has results."""
        out: list[dict[str, Any]] = []
        for step, kind, usd in self._all("SELECT step, kind, usd FROM llm_calls WHERE kind IN ('canary', 'sync') ORDER BY id"):
            out.append({"kind": kind, "step": step, "batch_id": kind, "usd": float(usd)})
        for step, batch_id, pending, usd in self._all(
                "SELECT step, batch_id, status = 'pending', sum(usd) FROM llm_calls WHERE kind = 'batch' "
                "GROUP BY step, batch_id, status = 'pending' ORDER BY min(id), status = 'pending' DESC"):
            out.append({"kind": "submitted" if pending else "ended", "step": step, "batch_id": batch_id, "usd": float(usd)})
        return out

    def token_cache_read(self, path: Path) -> dict[str, int]:
        return dict(self._all("SELECT fingerprint, input_tokens FROM llm_calls WHERE kind = 'count'"))

    def token_cache_write(self, path: Path, cache: dict[str, int], *, new: set[str] | None = None, model: str | None = None) -> None:
        keys = sorted(cache if new is None else new)
        if not keys:
            return
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO llm_calls (fingerprint, request_sha256, model, kind, status, input_tokens, usd, created_at) "
                "VALUES (%s, %s, %s, 'count', 'counted', %s, 0, NULL) ON CONFLICT (fingerprint) WHERE kind = 'count' "
                "DO UPDATE SET input_tokens = EXCLUDED.input_tokens", [(k, k, model, int(cache[k])) for k in keys])

    def spend_append(self, path: Path, entry: dict[str, Any]) -> None:
        """Nothing to add: a stage's spend is the sum of its llm_calls rows."""

    def priced(self, fingerprints: list[str]) -> dict[str, dict[str, Any]]:
        """What the database knows about each request fingerprint: {"answered": an answer that succeeded is archived,
        "input_tokens": its counted or billed input tokens, "usd": what its answer cost}. A fingerprint it has never seen
        is left out."""
        if not fingerprints:
            return {}
        out: dict[str, dict[str, Any]] = {}
        for fp, kind, status, tokens, usd in self._all(
                "SELECT fingerprint, kind, status, input_tokens, usd FROM llm_calls WHERE fingerprint = ANY(%s) ORDER BY id", (list(fingerprints),)):
            entry = out.setdefault(fp, {"answered": False, "input_tokens": None, "usd": 0.0})
            if kind == "count":
                entry["input_tokens"] = entry["input_tokens"] or tokens
            elif status == "succeeded":
                entry.update(answered=True, input_tokens=tokens, usd=float(usd))
        return out

    # -- companies and jobs (pipeline/job.py) --

    COMPANY_COLUMNS = ("cik", "ticker", "name", "sector", "fiscal_year_end_month", "fiscal_year_named_for", "tax_rate_parens_negative",
                       "publish", "unpublished_reason", "status", "first_built_at", "last_refreshed_at", "last_filing_date",
                       "accessions_on_last_date")

    def company(self, cik: str) -> dict[str, Any] | None:
        row = self.conn.execute(f"SELECT {', '.join(self.COMPANY_COLUMNS)} FROM companies WHERE cik = %s", (cik,)).fetchone()
        return dict(zip(self.COMPANY_COLUMNS, row)) if row else None

    def companies(self) -> list[dict[str, Any]]:
        rows = self._all(f"SELECT {', '.join(self.COMPANY_COLUMNS)} FROM companies ORDER BY cik")
        return [dict(zip(self.COMPANY_COLUMNS, r)) for r in rows]

    def upsert_company(self, cik: str, **fields: Any) -> None:
        """Insert the company (when `ticker` is given) or update the given fields on it. A field that is not given is left as it is."""
        unknown = set(fields) - set(self.COMPANY_COLUMNS)
        if unknown:
            raise StoreError(f"companies has no column(s) {', '.join(sorted(unknown))}")
        if "accessions_on_last_date" in fields and fields["accessions_on_last_date"] is not None:
            fields["accessions_on_last_date"] = _json_param(sorted(fields["accessions_on_last_date"]))
        if "ticker" not in fields:  # not a whole company: a change to one that is already there
            if fields:
                self.conn.execute(f"UPDATE companies SET {', '.join(f'{c} = %s' for c in fields)} WHERE cik = %s", (*fields.values(), cik))
            return
        columns = ["cik", *fields]
        update = ", ".join(f"{c} = EXCLUDED.{c}" for c in fields) or "cik = EXCLUDED.cik"
        self.conn.execute(
            f"INSERT INTO companies ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))}) "
            f"ON CONFLICT (cik) DO UPDATE SET {update}", (cik, *fields.values()))

    def ticker_holder(self, ticker: str) -> str | None:
        """The cik of the company that has this ticker, if one does (companies.ticker is unique)."""
        return self._one("SELECT cik FROM companies WHERE ticker = %s", (ticker,))

    def create_job(self, cik: str, kind: str, log_path: str | None = None) -> int:
        return self._one("INSERT INTO jobs (cik, kind, log_path) VALUES (%s, %s, %s) RETURNING id", (cik, kind, log_path))

    def update_job(self, job_id: int, **fields: Any) -> None:
        if fields:
            self.conn.execute(f"UPDATE jobs SET {', '.join(f'{c} = %s' for c in fields)} WHERE id = %s", (*fields.values(), job_id))

    def job(self, job_id: int) -> dict[str, Any]:
        columns = ("id", "cik", "kind", "status", "stage", "started_at", "finished_at", "usd_spent", "error", "log_path")
        return dict(zip(columns, self.conn.execute(f"SELECT {', '.join(columns)} FROM jobs WHERE id = %s", (job_id,)).fetchone()))

    def jobs(self, cik: str) -> list[dict[str, Any]]:
        """The company's jobs, newest first."""
        columns = ("id", "cik", "kind", "status", "stage", "started_at", "finished_at", "usd_spent", "error", "log_path")
        return [dict(zip(columns, r)) for r in self._all(f"SELECT {', '.join(columns)} FROM jobs WHERE cik = %s ORDER BY id DESC", (cik,))]

    def add_job_spend(self, job_id: int, usd: float) -> None:
        self.conn.execute("UPDATE jobs SET usd_spent = usd_spent + %s WHERE id = %s", (Decimal(repr(float(usd))), job_id))

    def spent_by_jobs_started_on(self, day: date) -> float:
        """USD spent by every job that started on `day` (UTC)."""
        return float(self._one(
            "SELECT COALESCE(sum(usd_spent), 0) FROM jobs WHERE (started_at AT TIME ZONE 'UTC')::date = %s", (day,)))

    def spent_in_month(self, day: date) -> float:
        """USD of every model call recorded in the calendar month `day` falls in (UTC), jobs or not, batches still
        pending counted at their worst case."""
        return float(self._one(
            "SELECT COALESCE(sum(usd), 0) FROM llm_calls WHERE date_trunc('month', created_at AT TIME ZONE 'UTC') = date_trunc('month', %s::timestamp)",
            (day,)))

    def record_call(self, *, step: str, custom_id: str, usd: float, fingerprint: str | None = None, model: str | None = None,
                    kind: str = "sync", status: str = "succeeded", text: str | None = None, input_tokens: int = 0,
                    output_tokens: int = 0, created_at: datetime | None = None) -> None:
        """One llm_calls row, for a caller that is not pipeline.llm (the job tests' stand-in pricer)."""
        row = {"custom_id": custom_id, "status": status, "text": text, "input_tokens": input_tokens, "output_tokens": output_tokens,
               "batch_id": kind if kind in ("sync", "canary") else None, "fingerprint": fingerprint}
        self.conn.execute(self._CALL_INSERT, self._call_params(step, row, model, usd, created_at))

    @contextlib.contextmanager
    def job_lock(self) -> Iterator[None]:
        """Held for the length of a job, on a connection of its own: a second job waits here until the first is done, so
        jobs run one at a time whoever starts them. The lock is per schema."""
        import psycopg

        key = int.from_bytes(hashlib.sha256(f"assumption-drift job {self.schema}".encode()).digest()[:8], "big", signed=True)
        with psycopg.connect(self.url, autocommit=True) as conn:
            conn.execute("SELECT pg_advisory_lock(%s)", (key,))
            try:
                yield
            finally:
                conn.execute("SELECT pg_advisory_unlock(%s)", (key,))

    # -- company requests (pipeline/worker.py, pipeline/notify.py) --

    REQUEST_COLUMNS = ("id", "cik", "ticker", "name", "email", "role", "status", "reason", "job_id", "created_at", "notified_at",
                       "source", "resend_id")

    def create_request(self, cik: str, email: str, **fields: Any) -> int:
        """One company_requests row, as the site inserts it. For the tests and for a request entered by hand."""
        unknown = set(fields) - set(self.REQUEST_COLUMNS)
        if unknown:
            raise StoreError(f"company_requests has no column(s) {', '.join(sorted(unknown))}")
        columns = ["cik", "email", *fields]
        return self._one(f"INSERT INTO company_requests ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))}) RETURNING id",
                         (cik, email, *fields.values()))

    def request(self, request_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(f"SELECT {', '.join(self.REQUEST_COLUMNS)} FROM company_requests WHERE id = %s", (request_id,)).fetchone()
        return dict(zip(self.REQUEST_COLUMNS, row)) if row else None

    def requests(self, *, status: str | None = None, cik: str | None = None, unnotified: bool = False) -> list[dict[str, Any]]:
        """Requests, oldest first, narrowed by status, by company, and to the ones nobody has been emailed about."""
        where, params = ["true"], []
        if status is not None:
            where.append("status = %s")
            params.append(status)
        if cik is not None:
            where.append("cik = %s")
            params.append(cik)
        if unnotified:
            where.append("notified_at IS NULL")
        rows = self._all(f"SELECT {', '.join(self.REQUEST_COLUMNS)} FROM company_requests WHERE {' AND '.join(where)} "
                         "ORDER BY created_at, id", tuple(params))
        return [dict(zip(self.REQUEST_COLUMNS, r)) for r in rows]

    def update_request(self, request_id: int, **fields: Any) -> None:
        unknown = set(fields) - set(self.REQUEST_COLUMNS)
        if unknown:
            raise StoreError(f"company_requests has no column(s) {', '.join(sorted(unknown))}")
        if fields:
            self.conn.execute(f"UPDATE company_requests SET {', '.join(f'{c} = %s' for c in fields)} WHERE id = %s",
                              (*fields.values(), request_id))

    def ciks_owed_an_email(self) -> list[str]:
        """Built companies with a live or duplicate request nobody has been emailed about yet."""
        return [r[0] for r in self._all(
            "SELECT DISTINCT r.cik FROM company_requests r JOIN companies c ON c.cik = r.cik "
            "WHERE r.notified_at IS NULL AND r.status IN ('live', 'duplicate') AND c.status = 'built' ORDER BY r.cik")]

    def emailed_before(self, cik: str, email: str, request_id: int) -> bool:
        """Another request from this address (case ignored) for this company has already been emailed."""
        return bool(self._one(
            "SELECT EXISTS (SELECT 1 FROM company_requests WHERE cik = %s AND lower(email) = lower(%s) AND id <> %s "
            "AND notified_at IS NOT NULL)", (cik, email, request_id)))

    def emails_sent_on(self, email: str, day: date) -> int:
        """How many companies this address (case ignored) was emailed about on `day` (UTC)."""
        return int(self._one(
            "SELECT count(DISTINCT cik) FROM company_requests WHERE lower(email) = lower(%s) AND (notified_at AT TIME ZONE 'UTC')::date = %s",
            (email, day)))

    def spent_on_day(self, day: date) -> float:
        """USD of every model call recorded on `day` (UTC), jobs or not, batches still pending counted at their worst case."""
        return float(self._one(
            "SELECT COALESCE(sum(usd), 0) FROM llm_calls WHERE (created_at AT TIME ZONE 'UTC')::date = %s", (day,)))

    def mark_machine_tier(self, cik: str) -> int:
        """review_tier = machine on the company's rows that have no tier yet: rows the pipeline produced and no person
        has decided. A row a person decided is `hand` and stays so. How many rows it set."""
        return self.conn.execute(
            "UPDATE statements SET review_tier = 'machine' WHERE cik = %s AND queue_seq IS NOT NULL AND review_tier IS NULL "
            "AND NOT COALESCE(empty_block, false)", (cik,)).rowcount

    def email_counts(self, cik: str) -> dict[str, Any]:
        """The numbers the "it is live" email states, from statements and filings. A statement is counted when it is in
        the review queue, is not an outlook-block flag, and is either machine tier or approved by a person: the rows a
        reader is shown. It has a resolution when its status (the published one, or for a machine row the rubric's
        proposed one) is met, missed or withdrawn. The filings are the ones those statements quote, dated by the
        filings table."""
        row = self.conn.execute(
            "SELECT count(*), count(DISTINCT s.assumption_accession), min(COALESCE(f.filed_at, s.assumption_filed_at)), "
            "max(COALESCE(f.filed_at, s.assumption_filed_at)), "
            "count(*) FILTER (WHERE COALESCE(s.published_status, s.aid_proposed_status) IN ('met', 'missed', 'withdrawn')), "
            "count(*) FILTER (WHERE s.review_tier = 'machine') "
            "FROM statements s LEFT JOIN filings f ON f.accession = s.assumption_accession "
            "WHERE s.cik = %s AND s.queue_seq IS NOT NULL AND NOT COALESCE(s.empty_block, false) "
            "AND (s.review_tier = 'machine' OR s.approved)", (cik,)).fetchone()
        n, n_filings, first, last, resolved, machine = row
        return {"n_statements": n, "n_filings": n_filings, "first_filing_date": first, "last_filing_date": last,
                "n_resolved": resolved, "n_open": n - resolved, "n_machine": machine}

    def worker_lock(self) -> Any | None:
        """Try to take the worker's lock, without waiting: a connection that holds it, or None when another worker has
        it. The lock is a session advisory lock on a fixed key (per schema), so it lasts exactly as long as the returned
        connection: close the connection, or let the process die, and it is released."""
        import psycopg

        key = int.from_bytes(hashlib.sha256(f"assumption-drift worker {self.schema}".encode()).digest()[:8], "big", signed=True)
        conn = psycopg.connect(self.url, autocommit=True)
        if conn.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()[0]:
            return conn
        conn.close()
        return None


# --- choosing one -----------------------------------------------------------------------------------------------------

_FILES = FilesBackend()
_POSTGRES: dict[tuple[str, str], PostgresBackend] = {}
_OVERRIDE: contextvars.ContextVar[Any] = contextvars.ContextVar("store_override", default=None)


def kind() -> str:
    """`files` or `postgres`: the STORE setting, read each time it is asked for."""
    value = (os.environ.get("STORE") or "files").strip().lower()
    if value not in ("files", "postgres"):
        raise StoreError(f"STORE must be files or postgres, got {value!r}")
    return value


def backend() -> Any:
    override = _OVERRIDE.get()
    if override is not None:
        return override
    if kind() == "files":
        return _FILES
    key = (database_url(), os.environ.get("STORE_SCHEMA") or "public")
    if key not in _POSTGRES or _POSTGRES[key].conn.closed:
        _POSTGRES[key] = PostgresBackend(*key)
    return _POSTGRES[key]


@contextlib.contextmanager
def use(chosen: Any) -> Iterator[Any]:
    """Use `chosen` as the store inside the block, whatever STORE says. For tests, and for a job that owns its connection."""
    token = _OVERRIDE.set(chosen)
    try:
        yield chosen
    finally:
        _OVERRIDE.reset(token)


def apply_schema() -> None:
    backend().apply_schema()


# The functions the stages call. Each takes the path the stage has always used.

def exists(path: Path) -> bool:
    return backend().exists(path)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """The rows of a .jsonl the pipeline wrote; an empty list when there are none."""
    return backend().read_jsonl(path)


def write_jsonl(path: Path, rows: list[dict[str, Any]], *, atomic: bool = False) -> None:
    backend().write_jsonl(path, rows, atomic=atomic)


def read_csv(path: Path) -> list[dict[str, str]]:
    return backend().read_csv(path)


def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    backend().write_csv(path, rows, fieldnames)
