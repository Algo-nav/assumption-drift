"""Phase 2, step 3b: a second machine opinion on the review queue, with Haiku over the Message Batches API.

    python -m pipeline.03b_verify [--company TICKER ...] [--submit] [--wait-minutes M]

For every row already in data/review/{cik}.csv that is a draft (not an empty-block flag), Haiku is shown
the metric, the numbers, the unit, the period, the excerpt, and the context around the excerpt: the
company's fiscal calendar, and, where the row carries them, the section heading, lead-in line and table
header. It answers yes or no to one question: does the excerpt itself state exactly that guidance, once
that context is used to read it? It writes back `aid_verify` ("yes" or "no"), `aid_verify_reason` (one
line), and, on a "no", `aid_verify_class`: one of wrong_metric, wrong_value, wrong_period, wrong_sign,
not_guidance or other, saying what kind of mismatch it is.

This is a second opinion, not a decision. It never touches `approved`, `hand_verified`, `reviewer_note`, or
any schema field, and a "no" is a prompt to look closer, not a rejection. An outlook-block flag row
(`empty_block=true`) has no metric or excerpt to check and is left alone.

A dry run unless --submit is given. Same cost gate as 03_structure and 04_outcomes: one ledger
(data/batches/ledger.jsonl), one budget across every step. Nothing is written to data/review/ unless
--submit is given and the batch has ended; a batch still processing when --wait-minutes runs out writes
nothing and exits 5, and running the same command again resumes it without resubmitting.

Re-running only sends a row again when the row itself changed: editing a row's metric, numbers, unit,
period or excerpt by hand changes the request sent for it, so the old answer no longer applies and it is
sent again. An untouched row is not resent, whatever else in the file changed. Editing the prompt or the
schema in this file (as happened between the first and second pass) changes the fingerprint of every
request alike, so the whole file is re-sent once.

Once every row that was sent has an answer, the CSV is rewritten: every human edit, approved,
hand_verified, reviewer_note, and any hand-corrected field, is carried over exactly as it was, and only
aid_verify, aid_verify_reason and aid_verify_class change. The rows are then sorted so aid_verify "no"
comes first, then section-captured rows, then the rest. Ties keep their existing order.

Reads   data/review/{cik}.csv
Writes  data/review/{cik}.csv       aid_verify, aid_verify_reason, aid_verify_class filled in, rows re-sorted
        data/batches/03b_verify.*   raw model output, ledger entries, pending batch state
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from pipeline import llm
from pipeline.common import CONFIG_PATH, REVIEW_DIR, Company, companies, load_config

review = importlib.import_module("pipeline.05_review")

STEP = "03b_verify"
MAX_TOKENS = 220
#: Whatever is sent now goes at this cap, because an answer at MAX_TOKENS was cut off for some rows (a canary included). The cap is part of a request's fingerprint, so
#: raising MAX_TOKENS itself would make every answer already archived stale and send the whole file again.
RETRY_MAX_TOKENS = 600
EXPECTED_OUTPUT_TOKENS = 55
REASON_LIMIT = 200

VERIFY_CLASSES = ["wrong_metric", "wrong_value", "wrong_period", "wrong_sign", "not_guidance", "other"]

VERIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verified": {"type": "boolean"},
        "reason": {"type": "string"},
        "class": {"type": "string", "enum": VERIFY_CLASSES},
    },
    "required": ["verified", "reason", "class"],
    "additionalProperties": False,
}

VERIFY_SYSTEM = """\
You check a single line of guidance data against the excerpt of text it is supposed to come from.

You are given a metric, a number or a range, a unit, a period, and an excerpt of text taken verbatim from an SEC filing. Decide whether the excerpt itself states, in those words or an unambiguous equivalent, exactly that guidance: that metric, for that period, with that number or range. You are checking the excerpt against the claim, not checking whether the claim is a good business forecast.

Context you are also given, which is not itself part of the claim to check
- Fiscal calendar: the company's fiscal year end month and which calendar year its label names. A company often states a period as a calendar date ("the three months ended April 30, 2019") rather than a fiscal label ("Q1 FY2020"). Work out which fiscal quarter and year that calendar date falls in from the fiscal year end month, the way the company itself would label it, and judge the period against that, not against whether the words match.
- Table columns: when the section's table puts GAAP and non-GAAP (and sometimes an Adjustments column between them) side by side, this lists the columns left to right and says which column the claim's figure was taken from. A section heading names only one of them (Micron's "Non-GAAP (2) Outlook" sits over the second column, with "GAAP (1) Outlook" in the first), so judge the claim's basis, GAAP or non-GAAP, by the column the figure is in, not by the heading.
- Section heading, lead-in line, table header: text that was near the excerpt in the filing but is not itself the excerpt. A period, or which of two "respectively" values belongs to this claim, is often only stated in one of these and never repeated in the excerpt itself. Use them to read the excerpt correctly; do not fail a claim only because the excerpt does not repeat something its heading, lead-in or table header already establishes.

Rules for reading the numbers
- A point value P stated with "plus or minus X" (or "X%, plus or minus Y%" for a rate) means the correct range is [P-X, P+X]. If the numbers you are given are exactly that range, the excerpt supports them.
- "$21B" and "$21.0B" are the same number: trailing zeros never change a figure. "Adjusted" and "non-GAAP" name the same accounting basis.
- "A decline of 3 to 5 percent" and a range of -5% to -3% describe the same thing: a negative change of between 3 and 5 percent in magnitude. Do not fail a claim only because the excerpt spells the direction out in words ("decline", "down") while the claim gives it as a negative number, or the reverse.
- When two values are given "respectively" (GAAP and non-GAAP, or two periods in a table), match the claim's metric and period to the one it names; do not fail it only because the OTHER value is also printed nearby.

Answer false if any of these is true:
- the excerpt is about a different metric, or does not name a metric that matches the one given;
- the excerpt is about a different period, or does not say which period the number is for, once the fiscal calendar above is used to read any calendar date;
- the number or range printed in the excerpt does not match the number given, once the rules above are applied (rounding or a symbol like "~" is fine; a genuinely different figure is not);
- the excerpt's figure and the claim's figure differ only in being positive versus negative: the sign is backwards;
- the excerpt reports a past result rather than stating forward guidance, or has no number in it at all.

Otherwise answer true. Give a one-line reason either way, quoting the part of the excerpt your answer turns on.

When your answer is false, also classify why, as exactly one of: wrong_metric (a different metric than the one given), wrong_value (the number or range itself does not match), wrong_period (a different period, including a calendar date that does not map to the stated fiscal label), wrong_sign (right magnitude, backwards sign), not_guidance (a past result, or no number at all), other (any other reason). When your answer is true, return "other" for this field; it is not used.
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


def fiscal_note(company: Company) -> str:
    """What the verifier needs to map a calendar date onto the company's own fiscal labels."""
    if company.fiscal_year_end_month is None:
        return f"{company.name}'s fiscal calendar is not known."
    named = "the calendar year its fiscal year ENDS in" if company.fiscal_year_named_for == "end" else "the calendar year its fiscal year STARTS in"
    return (f"{company.name}'s fiscal year ends in month {company.fiscal_year_end_month} of the calendar year "
            f"(1=January, 12=December), and a fiscal year is named for {named}.")


def build_prompt(row: dict[str, str], company: Company) -> str:
    numbers = review.range_words({
        "target_low": float(row["assumption.target_low"]) if row.get("assumption.target_low") else None,
        "target_high": float(row["assumption.target_high"]) if row.get("assumption.target_high") else None,
        "unit": row.get("assumption.unit", ""),
    })
    parts = [
        f"Metric: {row.get('assumption.metric', '')}",
        f"Numbers: {numbers}",
        f"Unit: {row.get('assumption.unit', '')}",
        f"Period: {row.get('assumption.target_period', '')}",
        f"Fiscal calendar: {fiscal_note(company)}",
    ]
    if row.get("aid_heading"):
        parts.append(f"Section heading: {row['aid_heading']}")
    if row.get("aid_lead_in"):
        parts.append(f"Lead-in line: {row['aid_lead_in']}")
    if row.get("aid_table_header"):
        parts.append(f"Table header: {row['aid_table_header']}")
    if row.get("aid_value_column"):
        parts.append(f"Table columns: {row['aid_value_column']}")
    parts.append(f'Excerpt: "{row.get("assumption.evidence.excerpt", "")}"')
    return "\n".join(parts)


def build_requests(targets: list[Company], review_dir: Path | None = None) -> tuple[list[llm.LlmRequest], dict[str, tuple[Company, str]]]:
    """(requests, custom_id -> (company, record_id)), one request per verifiable row, in file order."""
    requests: list[llm.LlmRequest] = []
    index: dict[str, tuple[Company, str]] = {}
    for company in targets:
        for row in load_rows(company, review_dir):
            if not is_verifiable(row):
                continue
            cid = custom_id(row)
            requests.append(llm.LlmRequest(cid, VERIFY_SYSTEM, build_prompt(row, company), MAX_TOKENS, VERIFY_SCHEMA))
            index[cid] = (company, row["record_id"])
    return requests, index


def with_retry_cap(request: llm.LlmRequest) -> llm.LlmRequest:
    return dataclasses.replace(request, max_tokens=RETRY_MAX_TOKENS)


def plan_requests(requests: list[llm.LlmRequest], archived: dict[str, llm.Result], model: str) -> tuple[dict[str, llm.Result], list[llm.LlmRequest]]:
    """(answers in hand, requests still to send). A row answered at either cap is done, so the answers archived at
    MAX_TOKENS stay valid. Whatever is still to send goes at RETRY_MAX_TOKENS: a row that was cut off at the lower
    cap, and a new row that might be, are both given room."""
    retried = {r.custom_id: with_retry_cap(r) for r in requests}
    base = llm.current_results(archived, requests, model)
    retry = llm.current_results(archived, list(retried.values()), model)
    have = {cid: res for cid, res in {**base, **retry}.items() if res.ok}
    return have, [retried[r.custom_id] for r in requests if r.custom_id not in have]


def derive_verdicts(index: dict[str, tuple[Company, str]], results: dict[str, llm.Result]) -> dict[str, tuple[str, str, str]]:
    """record_id -> (aid_verify, aid_verify_reason, aid_verify_class), from archived results. Pure: no model
    call. class is blank when the answer is "yes": it is only meaningful for a "no"."""
    verdicts: dict[str, tuple[str, str, str]] = {}
    for cid, (_, record_id) in index.items():
        res = results.get(cid)
        if res is None or not res.ok:
            continue
        payload = llm.parse_json_text(res.text)
        if not isinstance(payload, dict) or not {"verified", "reason", "class"} <= set(payload):
            continue
        verified = bool(payload["verified"])
        reason = str(payload["reason"]).strip()[:REASON_LIMIT]
        klass = str(payload["class"]) if not verified and payload["class"] in VERIFY_CLASSES else ""
        verdicts[record_id] = ("yes" if verified else "no", reason, klass)
    return verdicts


# --- writing back: preserve edits, then sort ----------------------------------


def effective_verdict(row: dict[str, str], verdicts: dict[str, tuple[str, str, str]]) -> tuple[str, str, str]:
    """A row's (aid_verify, aid_verify_reason, aid_verify_class): the fresh verdict if there is one, else
    whatever the row already had (not sent this run, or the batch has not answered it yet)."""
    return verdicts.get(row["record_id"], (row.get("aid_verify", ""), row.get("aid_verify_reason", ""), row.get("aid_verify_class", "")))


def apply_verdicts(rows: list[dict[str, str]], verdicts: dict[str, tuple[str, str, str]]) -> list[dict[str, str]]:
    """Every column but aid_verify, aid_verify_reason and aid_verify_class is carried over untouched."""
    updated = []
    for row in rows:
        verify, reason, klass = effective_verdict(row, verdicts)
        updated.append({**row, "aid_verify": verify, "aid_verify_reason": reason, "aid_verify_class": klass})
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
    if not isinstance(payload, dict) or not {"verified", "reason", "class"} <= set(payload):
        raise RuntimeError(f"canary output is not {{verified, reason, class}}: {text[:200]!r}")


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
    have, todo = plan_requests(requests, archive.load(), cfg.model)
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
        results, _ = plan_requests(requests, archive.load(), cfg.model)
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
        seen = [effective_verdict(r, verdicts) for r in checkable]
        checked = sum(1 for r in checkable if r["record_id"] in verdicts)
        nos = [v for v in seen if v[0] == "no"]
        print(f"  {company.ticker}: {len(checkable):,} rows to check, {checked:,} have an answer, {len(nos):,} say no")
        if nos:
            by_class = Counter(v[2] or "other" for v in nos)
            print(f"     no, by class: {dict(sorted(by_class.items()))}")
    if not args.submit:
        print("dry run: nothing was written to data/review/. Add --submit to run the batch.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
