"""Load the published dataset from the per-CIK files into Postgres.

    python scripts/load_published.py [--all] [--company TICKER ...] [--no-verify]

Needs DATABASE_URL (the environment, or .env at the repo root); STORE_SCHEMA picks the schema (default public). It
applies db/schema.sql first, so it runs against an empty database.

What goes in, for the 15 published companies (`publish: true` in pipeline/config.yaml; --all adds the unpublished ones
that have files, --company picks by ticker):

  companies    every company in config.yaml, published or not, with its fiscal calendar; the refresh watermark from
               data/manifest.json
  filings      every sidecar under data/raw/{cik}/
  candidates   data/candidates/{cik}.jsonl
  statements   data/review/{cik}.csv (every row: approved, rejected, held, pending, and the outlook-block flags, with
               the reviewer's note), data/drafts/{cik}.jsonl and data/outcomes/{cik}.jsonl
  stage_diagnostics  data/drafts/{cik}.{rejects,skipped,empty_blocks}.jsonl, data/outcomes/{cik}.rejects.jsonl
  llm_calls    every line of data/batches/*.raw.jsonl (all companies: the archive is not split by company), each with its
               cost and, from data/batches/ledger.jsonl, when it was made; data/batches/token_cache.json

Each list is replaced whole from its file, and archive lines are keyed by line number, so running it twice leaves the
database exactly as running it once. It is a load FROM the files: a row changed in the database since the last load is
overwritten by what the file says.

After loading, unless --no-verify, every list is read back from the database and compared with its file: the rows must be
equal and must serialise to the same bytes the file holds. Any difference fails the run.

A file that is not there is skipped and counted (data/candidates/, data/drafts/, data/batches/ and data/raw/ are
git-ignored, so a fresh checkout has only the review queues and the outcomes).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from collections import Counter
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import common, llm  # noqa: E402
from research_record import store  # noqa: E402

FILES = store.FilesBackend()
DRAFT_BYPRODUCTS = ("rejects", "skipped", "empty_blocks")


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


def _shown(path: Path) -> str:
    try:
        return str(path.relative_to(common.REPO_ROOT))
    except ValueError:
        return str(path)


def _jsonl_bytes(rows: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)


def _csv_text(rows: list[dict[str, str]], fieldnames: list[str]) -> str:
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def sidecars(cik: str) -> list[dict[str, Any]]:
    """Every sidecar under data/raw/{cik}/, oldest filing first."""
    metas = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((common.RAW_DIR / cik).glob("*.meta.json"))]
    return sorted(metas, key=lambda m: (m["filed_at"], m["accession"]))


def company_paths(cik: str) -> dict[str, Path]:
    paths = {
        "candidates": common.CANDIDATES_DIR / f"{cik}.jsonl",
        "drafts": common.DRAFTS_DIR / f"{cik}.jsonl",
        "outcomes": common.OUTCOMES_DIR / f"{cik}.jsonl",
        "outcomes.rejects": common.OUTCOMES_DIR / f"{cik}.rejects.jsonl",
    }
    paths.update({f"drafts.{k}": common.DRAFTS_DIR / f"{cik}.{k}.jsonl" for k in DRAFT_BYPRODUCTS})
    return paths


def load_companies(db: store.PostgresBackend, config: dict[str, Any]) -> list[common.Company]:
    try:
        manifest = json.loads(common.MANIFEST_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        manifest = {"companies": {}}
    every = common.companies(config)
    for c in every:
        fields: dict[str, Any] = dict(
            ticker=c.ticker, name=c.name, sector=c.sector, fiscal_year_end_month=c.fiscal_year_end_month,
            fiscal_year_named_for=c.fiscal_year_named_for, tax_rate_parens_negative=c.tax_rate_parens_negative,
            publish=c.publish, unpublished_reason=c.unpublished_reason)
        state = manifest["companies"].get(c.cik)
        if state:
            fields.update(last_filing_date=date.fromisoformat(state["last_filing_date"]),
                          accessions_on_last_date=state["accessions_on_last_date"],
                          last_refreshed_at=_midnight(date.fromisoformat(manifest["processed_through"])))
        db.upsert_company(c.cik, **fields)
    return every


def load_company(db: store.PostgresBackend, company: common.Company, counts: Counter, missing: list[str]) -> None:
    cik = company.cik
    metas = sidecars(cik)
    if not metas:
        missing.append(_shown(common.RAW_DIR / cik) + "/")
    for meta in metas:
        db.record_filing(meta)
    counts["filings"] += len(metas)

    for name, path in company_paths(cik).items():
        if not path.exists():
            if "." not in name:  # a by-product file exists only when the stage had something to put in it
                missing.append(_shown(path))
            db.write_jsonl(path, [])
            continue
        rows = FILES.read_jsonl(path)
        db.write_jsonl(path, rows)
        counts[name] += len(rows)

    review = common.REVIEW_DIR / f"{cik}.csv"
    if review.exists():
        with review.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
            fieldnames = list(reader.fieldnames or [])
        db.write_csv(review, rows, fieldnames)
        counts["review rows"] += len(rows)
        if rows:
            # The day 05_review first queued a row for the company: the earliest last_reviewed_at it stamped.
            first = min((r["last_reviewed_at"] for r in rows if r["last_reviewed_at"]), default=None)
            db.conn.execute(
                "UPDATE companies SET first_built_at = COALESCE(first_built_at, %s), status = CASE WHEN status = 'config' AND publish THEN 'built' ELSE status END "
                "WHERE cik = %s", (_midnight(date.fromisoformat(first)) if first else None, cik))
    else:
        missing.append(_shown(review))
        db.write_csv(review, [], [c for c, _, _ in store.REVIEW_COLUMNS])
    if company.publish:
        # Held: in the queue of a published company, not a flag, not approved, no note. The CSV has no mark for it.
        db.conn.execute(
            "UPDATE statements SET review_decision = 's', review_tier = 'hand' WHERE cik = %s AND queue_seq IS NOT NULL "
            "AND review_decision IS NULL AND approved IS NOT TRUE AND empty_block IS NOT TRUE", (cik,))


def ledger_times(batch_dir: Path) -> tuple[dict[tuple[str, str], list[datetime]], dict[str, datetime]]:
    """From data/batches/ledger.jsonl: ((step, canary|sync) -> the time of each such call, in order), and
    (batch id -> when it ended)."""
    single: dict[tuple[str, str], list[datetime]] = {}
    ended: dict[str, datetime] = {}
    for entry in FILES.ledger_entries(batch_dir / "ledger.jsonl"):
        when = datetime.fromisoformat(entry["ts"])
        if entry["kind"] in ("canary", "sync"):
            single.setdefault((entry["step"], entry["kind"]), []).append(when)
        elif entry["kind"] == "ended":
            ended[entry["batch_id"]] = when
    return single, ended


def load_llm(db: store.PostgresBackend, cfg: llm.LlmConfig, counts: Counter, notes: list[str]) -> float:
    """The archive and the token cache. Returns the cost of every archived call, at config.yaml's prices."""
    batch_dir = llm.BATCH_DIR
    single, ended = ledger_times(batch_dir)
    total = 0.0
    for path in sorted(batch_dir.glob("*.raw.jsonl")):
        step = path.name[: -len(".raw.jsonl")]
        rows = FILES.archive_read(path)
        costs, created = [], []
        seen: Counter = Counter()
        undated = 0
        for row in rows:
            kind = row["batch_id"] if row["batch_id"] in ("sync", "canary") else "batch"
            costs.append(cfg.usd(row["input_tokens"], row["output_tokens"], batch=kind == "batch"))
            if kind == "batch":
                when = ended.get(row["batch_id"])
            else:
                times = single.get((step, kind), [])
                when = times[seen[kind]] if seen[kind] < len(times) else None
                seen[kind] += 1
            undated += when is None
            created.append(when)
        for kind in ("canary", "sync"):
            if seen[kind] != len(single.get((step, kind), [])):
                notes.append(f"{step}: {seen[kind]} {kind} answers in the archive, {len(single.get((step, kind), []))} in the ledger; "
                             "their times are matched in order as far as they go")
        if undated:
            notes.append(f"{step}: {undated} archived answer(s) have no ledger line, so no created_at")
        db.load_archive(step, rows, model=cfg.model, costs=costs, created=created)
        counts["llm answers"] += len(rows)
        total += sum(costs)
    cache_path = batch_dir / "token_cache.json"
    if cache_path.exists():
        cache = FILES.token_cache_read(cache_path)
        db.token_cache_write(cache_path, cache, model=cfg.model)
        counts["token counts"] += len(cache)
    counts["llm answers given a company"] += db.attribute_calls()
    return total


def verify(db: store.PostgresBackend, targets: list[common.Company]) -> list[str]:
    """Every list read back from the database against its file. The differences found, as text; empty when there are none."""
    problems: list[str] = []
    for c in targets:
        cik = c.cik
        from_db = db.filings(cik)
        if from_db != sidecars(cik):
            problems.append(f"{c.ticker}: filings read back differ from the sidecars")
        for name, path in company_paths(cik).items():
            if not path.exists():
                continue
            if _jsonl_bytes(db.read_jsonl(path)) != path.read_text(encoding="utf-8"):
                problems.append(f"{c.ticker}: {name} read back from the database is not byte-identical to {path.name}")
        review = common.REVIEW_DIR / f"{cik}.csv"
        if review.exists():
            text = review.read_text(encoding="utf-8")
            header = next(csv.reader(io.StringIO(text, newline="")))
            rows = db.read_csv(review) if db.exists(review) else []
            if _csv_text(rows, header) != text:
                problems.append(f"{c.ticker}: the review queue read back from the database is not byte-identical to {review.name}")
    for path in sorted(llm.BATCH_DIR.glob("*.raw.jsonl")):
        in_file = [{**{"fingerprint": None}, **row} for row in FILES.archive_read(path)]
        in_db = db._all(
            "SELECT custom_id, status, response_text, input_tokens, output_tokens, error, batch_id, fingerprint FROM llm_calls "
            "WHERE step = %s AND archive_seq IS NOT NULL ORDER BY archive_seq", (path.name[: -len(".raw.jsonl")],))
        keys = ("custom_id", "status", "text", "input_tokens", "output_tokens", "error", "batch_id", "fingerprint")
        if [dict(zip(keys, r)) for r in in_db] != [{k: row[k] for k in keys} for row in in_file]:
            problems.append(f"{path.name}: the archive read back from the database differs from the file")
    return problems


def summary(db: store.PostgresBackend) -> dict[str, Any]:
    one = db._one
    published = "cik IN (SELECT cik FROM companies WHERE publish)"
    queue = f"queue_seq IS NOT NULL AND {published}"
    return {
        "companies": one("SELECT count(*) FROM companies"),
        "companies built": one("SELECT count(*) FROM companies WHERE status = 'built'"),
        "filings": one("SELECT count(*) FROM filings"),
        "candidates": one("SELECT count(*) FROM candidates"),
        "statements": one("SELECT count(*) FROM statements"),
        "published companies: rows in the review queues": one(f"SELECT count(*) FROM statements WHERE {queue}"),
        "published companies: approved (y or v)": one(f"SELECT count(*) FROM statements WHERE {queue} AND review_decision IN ('y', 'v')"),
        "published companies: of those, hand-verified (v)": one(f"SELECT count(*) FROM statements WHERE {queue} AND review_decision = 'v'"),
        "published companies: rejected (n)": one(f"SELECT count(*) FROM statements WHERE {queue} AND review_decision = 'n'"),
        "published companies: held (s)": one(f"SELECT count(*) FROM statements WHERE {queue} AND review_decision = 's'"),
        "published companies: outlook-block flags": one(f"SELECT count(*) FROM statements WHERE {queue} AND empty_block"),
        "stage_diagnostics": one("SELECT count(*) FROM stage_diagnostics"),
        "llm_calls answers": one("SELECT count(*) FROM llm_calls WHERE kind <> 'count'"),
        "llm_calls token counts": one("SELECT count(*) FROM llm_calls WHERE kind = 'count'"),
        "llm_calls usd": float(one("SELECT COALESCE(sum(usd), 0) FROM llm_calls")),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load the published dataset from the per-CIK files into Postgres.")
    parser.add_argument("--config", type=Path, default=common.CONFIG_PATH)
    parser.add_argument("--all", action="store_true", help="also the unpublished companies that have files (JPM, MU, ...)")
    parser.add_argument("--company", action="append", metavar="TICKER", help="only these; repeatable")
    parser.add_argument("--no-verify", action="store_true", help="skip reading everything back and comparing it with the files")
    parser.add_argument("--json", type=Path, metavar="PATH", help="also write the counts as JSON here")
    args = parser.parse_args(argv)

    config = common.load_config(args.config)
    db = store.PostgresBackend()
    db.apply_schema()
    every = load_companies(db, config)
    targets = [c for c in every if (args.all or c.publish) and (not args.company or c.ticker in {t.upper() for t in args.company})]

    counts: Counter = Counter()
    missing: list[str] = []
    notes: list[str] = []
    for company in targets:
        load_company(db, company, counts, missing)
        print(f"  {company.ticker}: loaded")
    usd = load_llm(db, llm.LlmConfig.from_config(config["llm"]), counts, notes)

    print(f"loaded {len(targets)} companies: " + ", ".join(f"{v:,} {k}" for k, v in counts.items()))
    print(f"archived model calls cost ${usd:.4f} at config.yaml's prices")
    if missing:
        print(f"not on disk, skipped ({len(missing)}): " + ", ".join(missing[:8]) + (" ..." if len(missing) > 8 else ""))
    for note in notes:
        print(f"note: {note}")
    result = summary(db)
    for key, value in result.items():
        print(f"  {key}: {value:,}" if isinstance(value, int) else f"  {key}: {value:.4f}")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({**result, "missing": missing, "notes": notes}, indent=2) + "\n", encoding="utf-8")

    if not args.no_verify:
        problems = verify(db, targets)
        if problems:
            for problem in problems:
                print(f"DIFFERS: {problem}", file=sys.stderr)
            print(f"FAILED: {len(problems)} list(s) read back from the database differ from the files.", file=sys.stderr)
            return 1
        print("verified: every list read back from the database is byte-identical to its file")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
