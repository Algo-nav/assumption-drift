"""Phase 2, step 1: candidate sentence -> draft assumption, with Haiku over the Message Batches API.

    python -m pipeline.03_structure [--company TICKER ...] [--filing-type FORM ...] [--limit N]
                                    [--submit] [--wait-minutes M]

A dry run unless --submit is given: it builds every request, counts input tokens,
prints the projected cost and submits nothing. --submit runs the cost gate, one
canary request, then the batch. By default only 8-K candidates are sent.

The model reads a line and the text around it and returns the numbers the line states
for each metric it gives guidance on. Everything that can be worked out by code is:
`stated_at` is the filing date, the excerpt is the verbatim sentence, evidence comes
from the cached filing's sidecar, and "$108.0 billion, plus or minus 2%" becomes a
range by arithmetic here, not by the model.

Every output is a draft. Nothing from this step is trusted until a human has approved
it in the review queue (05_review.py).

Reads   data/candidates/{cik}.jsonl, data/raw/{cik}/
Writes  data/drafts/{cik}.jsonl          validated draft assumptions
        data/drafts/{cik}.rejects.jsonl  model output that failed validation, with the reason
        data/drafts/{cik}.skipped.jsonl  candidates never sent, or drafts dropped as duplicates
        data/batches/03_structure.*      raw model output, ledger entries, pending batch state
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from pipeline import llm
from pipeline.common import (
    CANDIDATES_DIR,
    CONFIG_PATH,
    DRAFTS_DIR,
    Company,
    companies,
    load_config,
    raw_paths,
    read_meta,
    stable_ulid,
)
from research_record.schema import Assumption, Evidence
from research_record.text import html_to_text

STEP = "03_structure"
MAX_TOKENS = 700
#: For the "expected" cost line only. Most lines return an empty list, which is a handful of tokens.
EXPECTED_OUTPUT_TOKENS = 120
EXCERPT_LIMIT = 400  # Evidence.excerpt
UNITS = [
    "USD", "USD thousands", "USD millions", "USD billions", "USD per share",
    "percent", "percentage points", "basis points", "units", "other",
]
PERIOD = re.compile(r"^(?:Q[1-4] )?FY\d{4}$")

_NUMBER_OR_NULL = {"anyOf": [{"type": "number"}, {"type": "null"}]}
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "metric": {"type": "string"},
                    "unit": {"type": "string", "enum": UNITS},
                    "target_period": {"type": "string"},
                    "value_low": _NUMBER_OR_NULL,
                    "value_high": _NUMBER_OR_NULL,
                    "plus_minus": _NUMBER_OR_NULL,
                    "plus_minus_kind": {"type": "string", "enum": ["none", "percent", "absolute"]},
                },
                "required": ["metric", "unit", "target_period", "value_low", "value_high", "plus_minus", "plus_minus_kind"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You extract numeric forward guidance from SEC filings of US public companies.

You are shown one line from a filing (a sentence, a bullet, or a single table line) and the text around it. Decide whether that line states numeric forward guidance: a number or range that the company itself expects, targets or forecasts for a named metric over a named future period. List every distinct metric the line gives guidance for. If it gives none, list nothing.

What counts
- Guidance from the company about its own future results only. Not results that already happened, not descriptions of the past, not analyst views.
- The number must be explicit. "Strong growth" or "a low-single digit decline" has no number, so it gives nothing.
- The period must be explicit somewhere in the text you are shown: the line itself, the lead-in line, the section heading, the lines around it, or the title. If you cannot tell which period the number is for, list nothing. Do not guess the period.
- Table lines: the label is usually the line before ("Revenue") and the period is usually in the section heading ("Q4 FY26 Guidance"). Use them. Watch for GAAP and Non-GAAP labels.

How to fill each item
- metric: short and lower case, for example "revenue", "non-gaap gross margin", "gaap operating expenses", "gaap eps", "non-gaap eps", "operating margin", "operating cash flow growth", "tax rate", "other income and expense", "capital expenditures". Say GAAP or non-GAAP only when the text says which. If it says both ("GAAP and non-GAAP gross margins are expected to be 74.8% and 75.0%, respectively"), make one item for each.
- target_period: "FY2027" for a fiscal year, "Q3 FY2027" for a fiscal quarter. Use the company's own fiscal labels: "fiscal 2027", "fiscal year 2027" and "FY27" are all FY2027, and "third quarter of fiscal 2027" is Q3 FY2027. A company that calls its year "2026" (Target, for example) gives "the fourth quarter of 2026", which is Q4 FY2026. "Next quarter" or "the year" counts only when the text names the current quarter or year, so that you can work it out.
- unit: one of USD, USD thousands, USD millions, USD billions, USD per share, percent, percentage points, basis points, units, other. Take the unit as printed and do not rescale: "$46.1 billion" is 46.1 in USD billions.
- value_low and value_high: the stated range. A single number goes in both. "At least X" gives value_low only. "No more than X" gives value_high only. The other one is null.
- plus_minus and plus_minus_kind: for "X, plus or minus N%" put X in both value fields, N in plus_minus and "percent". For "74.0%, plus or minus 50 basis points" put 74.0 in both value fields, 0.5 in plus_minus (the same unit as the value) and "absolute". Otherwise plus_minus is null and plus_minus_kind is "none".
- A growth rate that only describes a dollar guidance in the same line ("...to $46.1 billion to $46.4 billion, up 11% - 12% Y/Y") is not a separate item. A growth rate that is itself the guidance (its own bullet, its own table row, "operating cash flow growth of 4% - 5%") is.
- Words like "approximately" and "about" do not change the numbers.

Examples

Lead-in line: NVIDIA's outlook for the fourth quarter of fiscal 2026 is as follows:
Line to assess: Revenue is expected to be $65.0 billion, plus or minus 2%.
{"items":[{"metric":"revenue","unit":"USD billions","target_period":"Q4 FY2026","value_low":65.0,"value_high":65.0,"plus_minus":2.0,"plus_minus_kind":"percent"}]}

Lead-in line: NVIDIA's outlook for the fourth quarter of fiscal 2026 is as follows:
Line to assess: GAAP and non-GAAP gross margins are expected to be 74.8% and 75.0%, respectively, plus or minus 50 basis points.
{"items":[{"metric":"gaap gross margin","unit":"percent","target_period":"Q4 FY2026","value_low":74.8,"value_high":74.8,"plus_minus":0.5,"plus_minus_kind":"absolute"},{"metric":"non-gaap gross margin","unit":"percent","target_period":"Q4 FY2026","value_low":75.0,"value_high":75.0,"plus_minus":0.5,"plus_minus_kind":"absolute"}]}

Section heading: Q4 FY26 Guidance
Lines before: "GAAP" / "Revenue"
Line to assess: $11.13 - $11.23 billion
{"items":[{"metric":"revenue","unit":"USD billions","target_period":"Q4 FY2026","value_low":11.13,"value_high":11.23,"plus_minus":null,"plus_minus_kind":"none"}]}

Line to assess: Raises full year FY26 revenue guidance to $41.45 billion to $41.55 billion, up 9% - 10% Y/Y and approximately 9% in CC
{"items":[{"metric":"revenue","unit":"USD billions","target_period":"FY2026","value_low":41.45,"value_high":41.55,"plus_minus":null,"plus_minus_kind":"none"}]}

Line to assess: Second-quarter revenue was $89.0 billion, up 18% from the previous quarter and up 117% from a year ago.
{"items":[]}

Line to assess: For the fourth quarter of 2025, the Company is maintaining its expectation of a low-single digit decline in sales.
{"items":[]}
"""


# --- building requests -----------------------------------------------------


def load_candidates(company: Company, filing_types: set[str], directory: Path | None = None) -> list[dict[str, Any]]:
    path = (directory or CANDIDATES_DIR) / f"{company.cik}.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if r["filing_type"] in filing_types]


def custom_id(candidate: dict[str, Any]) -> str:
    return f"c-{candidate['cik']}-{candidate['accession'].replace('-', '')}-{candidate['char_start']}"


_TITLE_WORDS = re.compile(r"\b(results|reports|announces|earnings)\b", re.IGNORECASE)


def document_title(text: str) -> str | None:
    """The release headline, if one is near the top: the first line that reads like one."""
    for line in text.split("\n")[:25]:
        if _TITLE_WORDS.search(line) and 4 <= len(line.split()) <= 30:
            return line
    return None


def build_prompt(candidate: dict[str, Any], company: Company, title: str | None) -> str:
    def block(label: str, items: list[str]) -> str:
        return f"{label}:\n" + "\n".join(f"  {i}" for i in items) if items else f"{label}: (none)"

    return "\n".join(
        [
            f"Company: {company.name} ({company.ticker})",
            f"Filing: {candidate['filing_type']} exhibit, filed {candidate['filed_at']}",
            f"Title: {title}" if title else "Title: (not found)",
            f"Section heading: {candidate.get('heading') or '(none)'}",
            f"Lead-in line: {candidate.get('lead_in') or '(none)'}",
            block("Lines before", candidate["context_before"]),
            f"Line to assess: {candidate['sentence']}",
            block("Lines after", candidate["context_after"]),
        ]
    )


def build_requests(
    targets: list[Company],
    filing_types: set[str],
    *,
    candidates_dir: Path | None = None,
    limit: int | None = None,
) -> tuple[list[llm.LlmRequest], dict[str, tuple[Company, dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """(requests, custom_id -> (company, candidate), skipped rows per cik)."""
    requests: list[llm.LlmRequest] = []
    index: dict[str, tuple[Company, dict[str, Any]]] = {}
    skipped: dict[str, list[dict[str, Any]]] = {c.cik: [] for c in targets}
    titles: dict[tuple[str, str], str | None] = {}

    for company in targets:
        seen: set[tuple[str, str]] = set()
        for cand in load_candidates(company, filing_types, candidates_dir):
            cid = custom_id(cand)
            if len(cand["sentence"]) > EXCERPT_LIMIT:
                skipped[company.cik].append({"custom_id": cid, "reason": f"sentence is {len(cand['sentence'])} chars, over the {EXCERPT_LIMIT} char excerpt limit"})
                continue
            key = (company.cik, cand["accession"])
            if key not in titles:
                html_path, _ = raw_paths(company.cik, cand["accession"])
                titles[key] = document_title(html_to_text(html_path.read_bytes())) if html_path.exists() else None
            user = build_prompt(cand, company, titles[key])
            if (cand["accession"], user) in seen:
                skipped[company.cik].append({"custom_id": cid, "reason": "identical prompt already sent for this filing"})
                continue
            seen.add((cand["accession"], user))
            requests.append(llm.LlmRequest(cid, SYSTEM_PROMPT, user, MAX_TOKENS, SCHEMA))
            index[cid] = (company, cand)
    if limit is not None:
        requests = requests[:limit]
        index = {r.custom_id: index[r.custom_id] for r in requests}
    return requests, index, skipped


# --- turning model output into drafts --------------------------------------


def _round(value: float) -> float:
    return round(value, 6)


def resolve_range(item: dict[str, Any]) -> tuple[float | None, float | None]:
    """The stated numbers, with any plus-or-minus worked out here. ValueError if it cannot be."""
    low, high = item["value_low"], item["value_high"]
    kind, spread = item["plus_minus_kind"], item["plus_minus"]
    if kind != "none":
        if spread is None or low is None or high is None or low != high:
            raise ValueError("plus_minus needs one centre value in both value_low and value_high, and a spread")
        delta = abs(low) * spread / 100 if kind == "percent" else spread
        low, high = low - delta, high + delta
    if low is None and high is None:
        raise ValueError("no number stated")
    if low is not None and high is not None and low > high:
        raise ValueError(f"range is reversed ({low} > {high})")
    return (None if low is None else _round(low)), (None if high is None else _round(high))


def evidence_for(company: Company, candidate: dict[str, Any]) -> Evidence:
    _, meta_path = raw_paths(company.cik, candidate["accession"])
    meta = read_meta(meta_path)
    if meta is None or meta.get("http_status") != 200:
        raise ValueError(f"no 200 sidecar for {candidate['accession']}")
    return Evidence(
        source_url=meta["final_url"],
        accession_number=meta["accession"],
        filing_type=meta["filing_type"],
        filed_at=date.fromisoformat(meta["filed_at"]),
        fetched_at=datetime.fromisoformat(meta["fetched_at"]),
        content_sha256=meta["content_sha256"],
        excerpt=candidate["sentence"],
    )


def derive_drafts(
    index: dict[str, tuple[Company, dict[str, Any]]],
    results: dict[str, llm.Result],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """(drafts, rejects, duplicates) per cik, from archived results. Pure: no model call."""
    drafts: dict[str, list[dict[str, Any]]] = {}
    rejects: dict[str, list[dict[str, Any]]] = {}
    dupes: dict[str, list[dict[str, Any]]] = {}
    first_seen: dict[tuple, str] = {}

    for cid in sorted(index, key=lambda c: (index[c][0].cik, index[c][1]["filed_at"], index[c][1]["accession"], index[c][1]["char_start"])):
        company, cand = index[cid]
        res = results.get(cid)
        if res is None or not res.ok:
            continue
        drafts.setdefault(company.cik, [])
        rejects.setdefault(company.cik, [])
        dupes.setdefault(company.cik, [])
        payload = llm.parse_json_text(res.text)
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            rejects[company.cik].append({"custom_id": cid, "reason": "output did not parse as {items: [...]}"})
            continue
        for n, item in enumerate(payload["items"]):
            try:
                metric = str(item["metric"]).strip().lower()
                period = str(item["target_period"]).strip()
                if not metric or len(metric) > 80:
                    raise ValueError("metric is empty or over 80 characters")
                if not PERIOD.match(period):
                    raise ValueError(f"target_period {period!r} is not like FY2027 or Q3 FY2027")
                low, high = resolve_range(item)
                assumption = Assumption(
                    text=cand["sentence"], metric=metric, target_low=low, target_high=high,
                    unit=item["unit"], target_period=period,
                    stated_at=date.fromisoformat(cand["filed_at"]),
                    evidence=evidence_for(company, cand),
                )
            except (KeyError, TypeError, ValueError, ValidationError) as exc:
                rejects[company.cik].append({"custom_id": cid, "item": item, "reason": str(exc)[:300]})
                continue
            dedupe_key = (cand["accession"], metric, period, low, high)
            draft_id = stable_ulid(f"{cid}|{n}|{metric}|{period}", assumption.stated_at)
            if dedupe_key in first_seen:
                dupes[company.cik].append({"custom_id": cid, "reason": f"same metric, period and range as draft {first_seen[dedupe_key]} in this filing"})
                continue
            first_seen[dedupe_key] = draft_id
            drafts[company.cik].append(
                {
                    "draft_id": draft_id, "cik": company.cik, "ticker": company.ticker, "company": company.name,
                    "custom_id": cid, "capture_method": cand["capture_method"], "char_start": cand["char_start"],
                    "heading": cand.get("heading"), "lead_in": cand.get("lead_in"),
                    "item_index": n, "assumption": assumption.model_dump(mode="json"),
                }
            )
    return drafts, rejects, dupes


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows and not path.exists():  # a dry run must not leave empty files that look like output
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def canary_check(text: str) -> None:
    payload = llm.parse_json_text(text)
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise RuntimeError(f"canary output is not {{items: [...]}}: {text[:200]!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Draft assumptions from candidate sentences, with Haiku batches.")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--company", action="append", metavar="TICKER")
    parser.add_argument("--filing-type", action="append", metavar="FORM", help="default: 8-K only")
    parser.add_argument("--limit", type=int, help="send only the first N requests")
    parser.add_argument("--submit", action="store_true", help="really call the API; without it this is a dry run")
    parser.add_argument("--wait-minutes", type=float, default=60)
    args = parser.parse_args(argv)

    config = load_config(args.config)
    cfg = llm.LlmConfig.from_config(config["llm"])
    targets = companies(config, args.company)
    filing_types = set(args.filing_type or ["8-K"])
    requests, index, skipped = build_requests(targets, filing_types, limit=args.limit)

    archive, ledger = llm.RawArchive(STEP), llm.Ledger()
    have = archive.load()
    todo = [r for r in requests if not (r.custom_id in have and have[r.custom_id].ok)]
    print(f"{STEP}: {len(requests):,} requests for {', '.join(sorted(filing_types))} candidates "
          f"({len(requests) - len(todo):,} already answered, {len(todo):,} to send); "
          f"{sum(len(v) for v in skipped.values())} candidates skipped before sending")

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
        results = outcome.results
        for note in outcome.notes:
            print(f"note: {note}")
    else:
        if not args.submit:
            print("dry run: nothing was submitted. Add --submit to run the batch.")
        results = have

    drafts, rejects, dupes = derive_drafts(index, results)
    counts_by_company: Counter = Counter()
    for company in targets:
        write_jsonl(DRAFTS_DIR / f"{company.cik}.jsonl", drafts.get(company.cik, []))
        write_jsonl(DRAFTS_DIR / f"{company.cik}.rejects.jsonl", rejects.get(company.cik, []))
        write_jsonl(DRAFTS_DIR / f"{company.cik}.skipped.jsonl", skipped[company.cik] + dupes.get(company.cik, []))
        counts_by_company[company.ticker] = len(drafts.get(company.cik, []))
        print(f"  {company.ticker}: {len(drafts.get(company.cik, [])):,} drafts, {len(rejects.get(company.cik, []))} rejected, "
              f"{len(skipped[company.cik]) + len(dupes.get(company.cik, []))} skipped or duplicate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
