"""Phase 2, step 3b: a second machine opinion on the review queue, with Haiku over the Message Batches API.

    python -m pipeline.03b_verify [--company TICKER ...] [--submit] [--wait-minutes M]

For every row already in data/review/{cik}.csv that is a draft (not an empty-block flag), Haiku is shown
only five things: the metric, the numbers, the unit, the period and the excerpt. Nothing else, not the
company, not the filing, not what a human already decided. It answers yes or no to one question: does the
excerpt itself state exactly that guidance? It writes back `aid_verify` ("yes" or "no") and
`aid_verify_reason` (one line) to the row.

This is a second opinion, not a decision. It never touches `approved`, `hand_verified`, `reviewer_note`, or
any schema field, and a "no" is a prompt to look closer, not a rejection. An outlook-block flag row
(`empty_block=true`) has no metric or excerpt to check and is left alone.

A dry run unless --submit is given. Same cost gate as 03_structure and 04_outcomes: one ledger
(data/batches/ledger.jsonl), one budget across every step. Nothing is written to data/review/ unless
--submit is given and the batch has ended; a batch still processing when --wait-minutes runs out writes
nothing and exits 5, and running the same command again resumes it without resubmitting.

Re-running only sends a row again when the row itself changed: editing a row's metric, numbers, unit,
period or excerpt by hand changes the request sent for it, so the old answer no longer applies and it is
sent again. An untouched row is not resent, whatever else in the file changed.

Once every row that was sent has an answer, the CSV is rewritten: every human edit, approved,
hand_verified, reviewer_note, and any hand-corrected field, is carried over exactly as it was, and only
aid_verify and aid_verify_reason change. The rows are then sorted so aid_verify "no" comes first, then
section-captured rows, then the rest. Ties keep their existing order.

Reads   data/review/{cik}.csv
Writes  data/review/{cik}.csv       aid_verify, aid_verify_reason filled in, rows re-sorted
        data/batches/03b_verify.*   raw model output, ledger entries, pending batch state
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path
from typing import Any

from pipeline import llm
from pipeline.common import CONFIG_PATH, REVIEW_DIR, Company, companies, load_config

review = importlib.import_module("pipeline.05_review")

STEP = "03b_verify"
MAX_TOKENS = 150
EXPECTED_OUTPUT_TOKENS = 40
REASON_LIMIT = 200

VERIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verified": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["verified", "reason"],
    "additionalProperties": False,
}

VERIFY_SYSTEM = """\
You check a single line of guidance data against the excerpt of text it is supposed to come from.

You are given a metric, a number or a range, a unit, a period, and an excerpt of text taken verbatim from an \
SEC filing. Decide whether the excerpt itself states, in those words or an unambiguous equivalent, exactly \
that guidance: that metric, for that period, with that number or range. You are checking the excerpt against \
the claim, not checking whether the claim is a good business forecast.

Answer false if any of these is true:
- the excerpt is about a different metric, or does not name a metric that matches the one given;
- the excerpt is about a different period, or does not say which period the number is for;
- the number or range printed in the excerpt does not match the number given (rounding or a symbol like "~" \
is fine; a different figure is not);
- the excerpt reports a past result rather than stating forward guidance;
- the excerpt has no number in it at all.

Otherwise answer true. Give a one-line reason either way, quoting the part of the excerpt your answer turns on.
"""


# --- rows --------------------------------------------------------------------


def load_rows(company: Company, review_dir: Path | None = None) -> list[dict[str, str]]:
    path = (review_dir or REVIEW_DIR) / f"{company.cik}.csv"
    return review.read_csv(path) if path.exists() else []


def is_verifiable(row: dict[str, str]) -> bool:
    """A draft row with something to check: not an empty-block flag, and it has a metric and an excerpt."""
    return row.get("empty_block") != "true" and bool(row.get("assumption.metric")) and bool(row.get("assumption.evidence.excerpt"))


def custom_id(row: dict[str, str]) -> str:
    return f"v-{row['record_id']}"


def build_prompt(row: dict[str, str]) -> str:
    numbers = review.range_words({
        "target_low": float(row["assumption.target_low"]) if row.get("assumption.target_low") else None,
        "target_high": float(row["assumption.target_high"]) if row.get("assumption.target_high") else None,
        "unit": row.get("assumption.unit", ""),
    })
    return (
        f"Metric: {row.get('assumption.metric', '')}\n"
        f"Numbers: {numbers}\n"
        f"Unit: {row.get('assumption.unit', '')}\n"
        f"Period: {row.get('assumption.target_period', '')}\n"
        f'Excerpt: "{row.get("assumption.evidence.excerpt", "")}"'
    )


def build_requests(targets: list[Company], review_dir: Path | None = None) -> tuple[list[llm.LlmRequest], dict[str, tuple[Company, str]]]:
    """(requests, custom_id -> (company, record_id)), one request per verifiable row, in file order."""
    requests: list[llm.LlmRequest] = []
    index: dict[str, tuple[Company, str]] = {}
    for company in targets:
        for row in load_rows(company, review_dir):
            if not is_verifiable(row):
                continue
            cid = custom_id(row)
            requests.append(llm.LlmRequest(cid, VERIFY_SYSTEM, build_prompt(row), MAX_TOKENS, VERIFY_SCHEMA))
            index[cid] = (company, row["record_id"])
    return requests, index


def derive_verdicts(index: dict[str, tuple[Company, str]], results: dict[str, llm.Result]) -> dict[str, tuple[str, str]]:
    """record_id -> (aid_verify, aid_verify_reason), from archived results. Pure: no model call."""
    verdicts: dict[str, tuple[str, str]] = {}
    for cid, (_, record_id) in index.items():
        res = results.get(cid)
        if res is None or not res.ok:
            continue
        payload = llm.parse_json_text(res.text)
        if not isinstance(payload, dict) or "verified" not in payload or "reason" not in payload:
            continue
        verdicts[record_id] = ("yes" if payload["verified"] else "no", str(payload["reason"]).strip()[:REASON_LIMIT])
    return verdicts


# --- writing back: preserve edits, then sort ----------------------------------


def effective_verdict(row: dict[str, str], verdicts: dict[str, tuple[str, str]]) -> tuple[str, str]:
    """A row's (aid_verify, aid_verify_reason): the fresh verdict if there is one, else whatever the row
    already had (not sent this run, or the batch has not answered it yet)."""
    return verdicts.get(row["record_id"], (row.get("aid_verify", ""), row.get("aid_verify_reason", "")))


def apply_verdicts(rows: list[dict[str, str]], verdicts: dict[str, tuple[str, str]]) -> list[dict[str, str]]:
    """Every column but aid_verify and aid_verify_reason is carried over untouched."""
    updated = []
    for row in rows:
        verify, reason = effective_verdict(row, verdicts)
        updated.append({**row, "aid_verify": verify, "aid_verify_reason": reason})
    return updated


def sort_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Stable: aid_verify "no" first, then section-captured rows, then the rest. Ties keep their order."""

    def tier(row: dict[str, str]) -> int:
        if row.get("aid_verify") == "no":
            return 0
        if row.get("aid_capture_method") == "section":
            return 1
        return 2

    return sorted(rows, key=tier)


# --- running -------------------------------------------------------------------


def canary_check(text: str) -> None:
    payload = llm.parse_json_text(text)
    if not isinstance(payload, dict) or "verified" not in payload or "reason" not in payload:
        raise RuntimeError(f"canary output is not {{verified, reason}}: {text[:200]!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check each review row's excerpt against its own numbers, with Haiku batches.")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--company", action="append", metavar="TICKER")
    parser.add_argument("--submit", action="store_true", help="really call the API and rewrite data/review/; without it this is a dry run")
    parser.add_argument("--wait-minutes", type=float, default=60)
    args = parser.parse_args(argv)

    config = load_config(args.config)
    cfg = llm.LlmConfig.from_config(config["llm"])
    targets = companies(config, args.company)
    requests, index = build_requests(targets)

    archive, ledger = llm.RawArchive(STEP), llm.Ledger()
    have = llm.current_results(archive.load(), requests, cfg.model)
    todo = [r for r in requests if not (r.custom_id in have and have[r.custom_id].ok)]
    print(f"{STEP}: {len(requests):,} rows to check ({len(requests) - len(todo):,} already answered, {len(todo):,} to send)")

    client = llm.make_client()
    exact = llm.credentials_available(client, cfg.model)
    counts = llm.count_input_tokens(client, cfg, todo) if exact and todo else llm.estimate_tokens(todo)
    projection = llm.project(todo, counts, exact=exact, llm=cfg, expected_output_per_request=EXPECTED_OUTPUT_TOKENS)
    print(projection.describe(STEP, cap=cfg.budget_usd, committed=ledger.committed_usd()))

    if args.submit and todo:
        if not exact:
            print("STOPPED: no API credentials found. Set ANTHROPIC_API_KEY (or put it in .env at the repo root) "
                  "and run again. Nothing was submitted.", file=sys.stderr)
            return 3
        try:
            outcome = llm.run_batch(client, cfg, STEP, todo, projection=projection, canary_check=canary_check,
                                    ledger=ledger, archive=archive, wait_seconds=args.wait_minutes * 60)
        except llm.BudgetExceeded as exc:
            print(f"STOPPED: {exc}", file=sys.stderr)
            return 4
        results = llm.current_results(archive.load(), requests, cfg.model)
        for note in outcome.notes:
            print(f"note: {note}")
        if outcome.pending_batch:
            print(f"STOPPED: batch {outcome.pending_batch} has not ended, so data/review/ was not rewritten. "
                  "Run the same command again to resume it; nothing will be resubmitted.", file=sys.stderr)
            return 5
    else:
        if not args.submit:
            print("dry run: nothing was submitted. Add --submit to run the batch.")
        results = have

    verdicts = derive_verdicts(index, results)
    for company in targets:
        rows = load_rows(company)
        if not rows:
            continue
        checkable = [r for r in rows if is_verifiable(r)]
        if args.submit:  # a dry run never writes: with stale or missing answers it would blank out real verdicts
            review.write_csv(REVIEW_DIR / f"{company.cik}.csv", sort_rows(apply_verdicts(rows, verdicts)))
        checked = sum(1 for r in checkable if r["record_id"] in verdicts)
        nos = sum(1 for r in checkable if effective_verdict(r, verdicts)[0] == "no")
        print(f"  {company.ticker}: {len(checkable):,} rows to check, {checked:,} have an answer, {nos:,} say no")
    if not args.submit:
        print("dry run: nothing was written to data/review/. Add --submit to run the batch.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
