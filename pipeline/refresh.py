"""The weekly refresh's bookkeeping: what the job did, as data (summary.json) and as the issue it opens.

    python -m pipeline.refresh snapshot                 before the stages: the review rows as they are now
    python -m pipeline.refresh new-ids                  after 05_review: ids of the rows added since the snapshot
    python -m pipeline.refresh remaining --max-usd X --since-line N     what is left of X after this run's spend so far
    python -m pipeline.refresh summary --date D ...     write data/refresh/D/summary.json
    python -m pipeline.refresh issue --summary PATH --branch B          print the issue body

scripts/weekly_refresh.sh calls these between the pipeline stages. The two pure pieces, `build_summary` and
`render_issue_body`, take plain data and are what the tests cover.
"""

from __future__ import annotations

import argparse
import csv
import importlib
import json
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from pipeline import common

csv.field_size_limit(10**9)

#: The review columns the before/after comparison needs.
SNAPSHOT_COLUMNS = (
    "empty_block", "outcome.reported_value", "acknowledged_at", "ack_pending_review", "change_pending_review",
    "aid_proposed_change", "aid_withdrawal_note",
)
SNAPSHOT_PATH = common.WORK_DIR / "review_before.json"
NEW_IDS_PATH = common.WORK_DIR / "new_row_ids.txt"
VERIFY_IDS_PATH = common.WORK_DIR / "new_verify_ids.txt"


# --- reading the repo --------------------------------------------------------------------------------------------------------


def read_review(review_dir: Path) -> dict[str, dict[str, dict[str, str]]]:
    """cik -> record_id -> the SNAPSHOT_COLUMNS of every row in data/review/{cik}.csv."""
    out: dict[str, dict[str, dict[str, str]]] = {}
    for path in sorted(review_dir.glob("*.csv")):
        with path.open(newline="", encoding="utf-8") as fh:
            out[path.stem] = {r["record_id"]: {c: r.get(c, "") for c in SNAPSHOT_COLUMNS} for r in csv.DictReader(fh)}
    return out


def read_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def spend_this_run(ledger: list[dict[str, Any]], since_line: int) -> dict[str, dict[str, float]]:
    """stage -> {requests, usd} for the ledger lines added after the first `since_line` lines."""
    out: dict[str, dict[str, float]] = defaultdict(lambda: {"requests": 0, "usd": 0.0})
    for entry in ledger[since_line:]:
        out[entry["stage"]]["requests"] += int(entry.get("requests", 0))
        out[entry["stage"]]["usd"] = round(out[entry["stage"]]["usd"] + float(entry.get("usd", 0.0)), 6)
    return dict(out)


def count_candidates(candidates_dir: Path, scope: dict[str, set[str]]) -> dict[str, int]:
    """cik -> candidate sentences in the new filings (02 --accessions wrote them to data/candidates/{cik}.jsonl)."""
    out: dict[str, int] = {}
    for cik, accessions in scope.items():
        path = candidates_dir / f"{cik}.jsonl"
        if not path.exists():
            continue
        n = sum(1 for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip() and json.loads(line).get("accession") in accessions)
        if n:
            out[cik] = n
    return out


def pending_count(published: list[common.Company], review_dir: Path) -> int:
    """Rows nobody has decided on across the published companies: what the card and the Space call "rows pending review"."""
    publish = importlib.import_module("pipeline.06_publish")
    return sum(len(publish.pending_rows(c, review_dir)) for c in published)


# --- the comparison ----------------------------------------------------------------------------------------------------------


def _proposes_withdrawal(row: dict[str, str]) -> bool:
    try:
        return "aid_withdrawal_note" in json.loads(row.get("aid_proposed_change") or "{}")
    except json.JSONDecodeError:
        return False


def diff_review(before: dict[str, dict[str, dict[str, str]]], after: dict[str, dict[str, dict[str, str]]]) -> dict[str, dict[str, Any]]:
    """Per cik, what the stages changed in the review rows: new row ids (and how many are flags), outcomes attached, acknowledgements
    and withdrawals filled or proposed, and the ids of rows with a new change or acknowledgement proposal."""
    out: dict[str, dict[str, Any]] = {}
    for cik, rows in after.items():
        old = before.get(cik, {})
        d: dict[str, Any] = {"new_ids": [], "new_flag_ids": [], "outcomes_attached": 0, "outcomes_proposed": 0, "acks_filled": 0,
                             "acks_proposed": 0, "withdrawals_filled": 0, "withdrawals_proposed": 0, "proposal_ids": []}
        for rid, row in rows.items():
            prev = old.get(rid)
            if prev is None:
                d["new_ids"].append(rid)
                if row["empty_block"] == "true":
                    d["new_flag_ids"].append(rid)
                continue
            if not prev["outcome.reported_value"] and row["outcome.reported_value"]:
                d["outcomes_attached"] += 1
            if not prev["acknowledged_at"] and row["acknowledged_at"]:
                d["acks_filled"] += 1
            if not prev["aid_withdrawal_note"] and row["aid_withdrawal_note"]:
                d["withdrawals_filled"] += 1
            new_change = row["change_pending_review"] == "true" and prev["change_pending_review"] != "true"
            new_ack = row["ack_pending_review"] == "true" and prev["ack_pending_review"] != "true"
            if new_ack:
                d["acks_proposed"] += 1
            if new_change:
                d["outcomes_proposed"] += "outcome.reported_value" in (row["aid_proposed_change"] or "")
                d["withdrawals_proposed"] += _proposes_withdrawal(row)
            if new_change or new_ack:
                d["proposal_ids"].append(rid)
        out[cik] = d
    return out


# --- the summary -------------------------------------------------------------------------------------------------------------


def build_summary(*, run_date: str, max_usd: float, companies: list[dict[str, Any]], filings: list[dict[str, Any]],
                  candidates: dict[str, int], stage_spend: dict[str, dict[str, float]], diff: dict[str, dict[str, Any]],
                  pending_published: int, spend_to_date: float, failures: list[dict[str, Any]],
                  dumps: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """Everything the issue says, as one JSON-able dict. `companies` is [{cik, ticker}]; `filings` is new_filings.json's list."""
    ticker = {c["cik"]: c["ticker"] for c in companies}
    per_company: dict[str, dict[str, Any]] = {}

    def entry(cik: str) -> dict[str, Any]:
        return per_company.setdefault(ticker.get(cik, cik), {"filings": 0, "candidates": 0, "new_rows": 0, "new_flags": 0, "proposals": 0})

    for f in filings:
        entry(f["cik"])["filings"] += 1
    for cik, n in candidates.items():
        entry(cik)["candidates"] = n
    totals: Counter = Counter()
    for cik, d in diff.items():
        if not (d["new_ids"] or d["proposal_ids"]):
            continue
        e = entry(cik)
        e["new_rows"] = len(d["new_ids"]) - len(d["new_flag_ids"])
        e["new_flags"] = len(d["new_flag_ids"])
        e["proposals"] = len(d["proposal_ids"])
        for key in ("outcomes_attached", "outcomes_proposed", "acks_filled", "acks_proposed", "withdrawals_filled", "withdrawals_proposed"):
            totals[key] += d[key]
        totals["new_rows"] += e["new_rows"]
        totals["new_flags"] += e["new_flags"]
        totals["proposals"] += e["proposals"]
    for d in diff.values():  # outcomes and the like attach to rows of companies with no new rows too
        if not (d["new_ids"] or d["proposal_ids"]):
            for key in ("outcomes_attached", "outcomes_proposed", "acks_filled", "acks_proposed", "withdrawals_filled", "withdrawals_proposed"):
                totals[key] += d[key]
    stages = {s: {"requests": int(v["requests"]), "usd": round(v["usd"], 6)} for s, v in sorted(stage_spend.items())}
    return {
        "date": run_date,
        "max_usd": max_usd,
        "filings_total": len(filings),
        "filings_by_form": dict(sorted(Counter(f["form"] for f in filings).items())),
        "per_company": dict(sorted(per_company.items())),
        "candidates_total": sum(candidates.values()),
        "stages": stages,
        "run_usd": round(sum(v["usd"] for v in stages.values()), 6),
        "new_rows": totals["new_rows"],
        "new_flags": totals["new_flags"],
        "proposals": totals["proposals"],
        "outcomes": {"attached": totals["outcomes_attached"], "proposed": totals["outcomes_proposed"]},
        "acknowledgements": {"filled": totals["acks_filled"], "proposed": totals["acks_proposed"]},
        "withdrawals": {"filled": totals["withdrawals_filled"], "proposed": totals["withdrawals_proposed"]},
        "pending_published": pending_published,
        "spend_to_date_usd": round(spend_to_date, 6),
        "failures": failures,
        "dumps": dumps or {},
        "ciks": {c["cik"]: c["ticker"] for c in companies},
    }


def issue_title(summary: dict[str, Any]) -> str:
    return f"Refresh {summary['date']}: {summary['new_rows']} new rows, {summary['proposals']} proposals, ${summary['run_usd']:.2f}"


def _table(header: list[str], rows: list[list[Any]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return lines


def render_issue_body(summary: dict[str, Any], branch: str) -> str:
    """The issue body: the summary as tables, the branch, and the command that applies each dump file's decisions."""
    s = summary
    lines = [f"Branch: `{branch}`. Nothing here is published; merge the branch after review, then run `pipeline.06_publish`.", ""]
    forms = ", ".join(f"{n} {f}" for f, n in s["filings_by_form"].items()) or "none"
    lines += _table(["", ""], [
        ["Filings", f"{s['filings_total']} ({forms})"],
        ["Candidates", s["candidates_total"]],
        ["New rows", f"{s['new_rows']} (+ {s['new_flags']} empty-block flags)"],
        ["Outcomes attached / proposed", f"{s['outcomes']['attached']} / {s['outcomes']['proposed']}"],
        ["Acknowledgements filled / proposed", f"{s['acknowledgements']['filled']} / {s['acknowledgements']['proposed']}"],
        ["Withdrawals filled / proposed", f"{s['withdrawals']['filled']} / {s['withdrawals']['proposed']}"],
        ["Rows pending review (published companies)", s["pending_published"]],
        ["Spend this run / cap", f"${s['run_usd']:.4f} / ${s['max_usd']:.2f}"],
        ["Spend to date (ledger)", f"${s['spend_to_date_usd']:.4f}"],
    ])
    if s["per_company"]:
        lines += ["", "**By company**", ""]
        lines += _table(["Company", "Filings", "Candidates", "New rows", "Flags", "Proposals"],
                        [[t, c["filings"], c["candidates"], c["new_rows"], c["new_flags"], c["proposals"]] for t, c in s["per_company"].items()])
    if s["stages"]:
        lines += ["", "**By stage**", ""]
        lines += _table(["Stage", "Requests", "USD"], [[st, v["requests"], f"${v['usd']:.4f}"] for st, v in s["stages"].items()])
    if s["failures"]:
        lines += ["", "**Failures**", ""] + [f"- {f['stage']}: exit {f['exit']}" for f in s["failures"]]
    if s["dumps"]:
        lines += ["", "**Review**", "",
                  "Each dump is plain-text cards. Write `record_id,decision,note` (decision `y|n|s|v`) to a CSV and apply it:", "", "```"]
        for ticker, files in sorted(s["dumps"].items()):
            cik = next((c for c in s.get("ciks", {}) if s["ciks"][c] == ticker), None)
            csv_path = f"data/review/{cik}.csv" if cik else f"data/review/<{ticker} cik>.csv"
            for f in files:
                lines.append(f"rr review {csv_path} --apply decisions_{Path(f).stem}.csv   # decisions for {f}")
        lines.append("```")
    return "\n".join(lines) + "\n"


# --- command line ------------------------------------------------------------------------------------------------------------


def _companies() -> list[dict[str, Any]]:
    return [{"cik": c.cik, "ticker": c.ticker, "publish": c.publish} for c in common.companies(common.load_config())]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pipeline.refresh")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("snapshot")
    sub.add_parser("new-ids")
    r = sub.add_parser("remaining")
    r.add_argument("--max-usd", type=float, required=True)
    r.add_argument("--since-line", type=int, required=True)
    sm = sub.add_parser("summary")
    sm.add_argument("--date", required=True)
    sm.add_argument("--max-usd", type=float, required=True)
    sm.add_argument("--since-line", type=int, required=True)
    sm.add_argument("--failure", action="append", default=[], metavar="STAGE:EXIT")
    sm.add_argument("--out", type=Path, required=True)
    iss = sub.add_parser("issue")
    iss.add_argument("--summary", type=Path, required=True)
    iss.add_argument("--branch", required=True)
    iss.add_argument("--title", action="store_true", help="print the title instead of the body")
    sub.add_parser("dump-targets")
    args = p.parse_args(argv)

    if args.cmd == "snapshot":
        common.WORK_DIR.mkdir(parents=True, exist_ok=True)
        SNAPSHOT_PATH.write_text(json.dumps(read_review(common.REVIEW_DIR)), encoding="utf-8")
        print(f"snapshot: {sum(len(v) for v in json.loads(SNAPSHOT_PATH.read_text()).values())} review rows")
        return 0
    before = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8")) if args.cmd in ("new-ids", "summary", "dump-targets") else {}
    if args.cmd == "new-ids":
        d = diff_review(before, read_review(common.REVIEW_DIR))
        every = sorted(i for v in d.values() for i in v["new_ids"])
        flags = {i for v in d.values() for i in v["new_flag_ids"]}
        NEW_IDS_PATH.write_text("".join(i + "\n" for i in every), encoding="utf-8")
        VERIFY_IDS_PATH.write_text("".join(i + "\n" for i in every if i not in flags), encoding="utf-8")  # 03b skips a flag: it is not a draft
        print(f"new rows: {len(every)} ({len(flags)} empty-block flags, {len(every) - len(flags)} to verify)")
        return 0
    if args.cmd == "remaining":
        spent = sum(v["usd"] for v in spend_this_run(read_ledger(common.SPEND_LEDGER_PATH), args.since_line).values())
        print(f"{max(args.max_usd - spent, 0.0):.6f}")
        return 0
    if args.cmd == "dump-targets":  # "ticker cik new_ids_csv" per company with new rows or proposals, for the shell to dump
        d = diff_review(before, read_review(common.REVIEW_DIR))
        ticker = {c["cik"]: c["ticker"] for c in _companies()}
        for cik, v in sorted(d.items()):
            if v["new_ids"] or v["proposal_ids"]:
                print(ticker.get(cik, cik), cik, ",".join(v["new_ids"]) or "-", len(v["proposal_ids"]))
        return 0
    if args.cmd == "summary":
        cos = _companies()
        nf = common.NEW_FILINGS_PATH
        filings = json.loads(nf.read_text(encoding="utf-8")).get("filings", []) if nf.exists() else []
        scope = common.load_accession_scope(nf) if nf.exists() else {}
        ledger = read_ledger(common.SPEND_LEDGER_PATH)
        out_dir = args.out.parent
        dumps: dict[str, list[str]] = {}
        for f in sorted(out_dir.glob("*_new.txt")) + sorted(out_dir.glob("*_pending.txt")):
            dumps.setdefault(f.name.rsplit("_", 1)[0], []).append(f.name)
        failures = [{"stage": s.rsplit(":", 1)[0], "exit": int(s.rsplit(":", 1)[1])} for s in args.failure]
        summary = build_summary(
            run_date=args.date, max_usd=args.max_usd, companies=cos, filings=filings, candidates=count_candidates(common.CANDIDATES_DIR, scope),
            stage_spend=spend_this_run(ledger, args.since_line), diff=diff_review(before, read_review(common.REVIEW_DIR)),
            pending_published=pending_count([c for c in common.companies(common.load_config()) if c.publish], common.REVIEW_DIR),
            spend_to_date=sum(float(e.get("usd", 0.0)) for e in ledger), failures=failures, dumps=dumps)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
        return 0
    if args.cmd == "issue":
        summary = json.loads(args.summary.read_text(encoding="utf-8"))
        sys.stdout.write(issue_title(summary) + "\n" if args.title else render_issue_body(summary, args.branch))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
