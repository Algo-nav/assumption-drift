"""Phase 2, step 1: candidate -> draft assumptions, with Haiku over the Message Batches API.

    python -m pipeline.03_structure [--company TICKER ...] [--filing-type FORM ...]
                                    [--limit N | --per-company N] [--submit] [--wait-minutes M]

A dry run unless --submit is given: it builds every request, counts input tokens,
prints the projected cost and submits nothing, and it writes nothing to data/drafts/.
--submit runs the cost gate, one canary request, then the batch, and writes the drafts once
the batch has ended (a batch still processing when --wait-minutes runs out writes nothing and
exits 5; run it again to resume). With every request already answered it sends nothing and
re-derives the drafts from the archive. Every request is sent at temperature 0. By default only 8-K candidates are sent.

Two kinds of request:

  sentence  one sentence and the text around it (capture_method "sentence").
  section   the WHOLE guidance block under a heading, its lines numbered (capture_method
            "section"). A bare value line such as "$915 million" is never sent alone: the pilot
            showed that without its label the model invents metrics and ranges. The model says
            which lines each item came from, and those lines, verbatim, are the evidence.

The model may only name a metric from the `metrics` list in config.yaml, and a range needs
range language in the evidence. It returns the numbers the text states; everything that can
be worked out by code is: `stated_at` is the filing date, the excerpt is verbatim text from
the filing, evidence comes from the cached filing's sidecar, and "$108.0 billion, plus or
minus 2%" becomes a range by arithmetic here. Range language is checked in code too, so a
model that ignores the rule still cannot produce a false range.

Each of these is checked in code, whatever the model was told, and a failure is a reject with
its reason in data/drafts/{cik}.rejects.jsonl:

  unit     the unit must be the right kind for the metric (`metric_kinds` in config.yaml): a margin
           is never dollars, operating income and free cash flow are never a percentage.
  numbers  every number the item states must appear in its own evidence text. An answer lifted
           from the neighbouring line has no support in the excerpt that would be stored with it.
  period   a quarter ("Q3 FY2027") or a fiscal year ("FY2027"), nothing else. Evidence about "the
           second half" is never turned into a full year.
  tense    evidence in the past tense with nothing forward-looking in it is a result, not guidance.

And one thing is decided by code, overriding the model: a section whose heading names GAAP or
non-GAAP ("Adjusted diluted earnings per share guidance") fixes that basis for every metric in it
that has a GAAP and a non-GAAP entry, unless the section's own lines name a basis.

A figure printed as a floor ("$1.30+", "at least", "or more", "or better") is a low-only target
whatever the model made of it. Where two drafts share (cik, metric, target_period, stated_at)
a range is kept over a point that lies inside it; otherwise the sentence-captured one is
kept, and each drop is logged with both sets of numbers. Two SECTION-captured drafts with
different numbers are not a duplicate but a conflict: both are kept and marked `conflict`, the
review queue shows the mark, and nothing here picks a winner between them.

An 8-K outlook block the model answered that produced no draft (an empty answer, or every item
rejected) is listed in data/drafts/{cik}.empty_blocks.jsonl, and the review queue flags it.

Every output is a draft. Nothing from this step is trusted until a human has approved
it in the review queue (05_review.py).

Reads   data/candidates/{cik}.jsonl, data/raw/{cik}/
Writes  data/drafts/{cik}.jsonl          validated draft assumptions
        data/drafts/{cik}.rejects.jsonl  model output that failed validation, with the reason
        data/drafts/{cik}.skipped.jsonl  candidates never sent, and drafts dropped as duplicates
        data/drafts/{cik}.empty_blocks.jsonl  outlook blocks that produced no draft, with the reason
        data/batches/03_structure.*      raw model output, ledger entries, pending batch state
"""

from __future__ import annotations

import argparse
import importlib
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
from research_record.text import html_to_text, sentence_spans

candidates_step = importlib.import_module("pipeline.02_candidates")

STEP = "03_structure"
MAX_TOKENS_SENTENCE = 700
MAX_TOKENS_BLOCK = 1500
#: For the "expected" cost line only. Most requests return an empty list, which is a handful of tokens.
EXPECTED_OUTPUT_TOKENS = 120
EXCERPT_LIMIT = 400  # Evidence.excerpt
UNITS = [
    "USD", "USD thousands", "USD millions", "USD billions", "USD per share",
    "percent", "percentage points", "basis points", "units", "other",
]
PERIOD = re.compile(r"^(?:Q[1-4] )?FY\d{4}$")
#: What each unit measures, for the per-metric guard. `metric_kinds` in config.yaml says what each metric needs.
UNIT_KIND = {
    "USD": "dollars", "USD thousands": "dollars", "USD millions": "dollars", "USD billions": "dollars",
    "USD per share": "per share",
    "percent": "percent", "percentage points": "percent", "basis points": "percent",
    "units": "units", "other": "other",
}

_NUMBER_OR_NULL = {"anyOf": [{"type": "number"}, {"type": "null"}]}


def item_schema(metrics: list[str], *, block: bool) -> dict[str, Any]:
    """The JSON the model must return. `metric` is an enum of the config's list, so nothing else can come back."""
    props: dict[str, Any] = {
        "metric": {"type": "string", "enum": list(metrics)},
        "unit": {"type": "string", "enum": UNITS},
        "target_period": {"type": "string"},
        "value_low": _NUMBER_OR_NULL,
        "value_high": _NUMBER_OR_NULL,
        "plus_minus": _NUMBER_OR_NULL,
        "plus_minus_kind": {"type": "string", "enum": ["none", "percent", "absolute"]},
    }
    if block:
        props["line_first"] = {"type": "integer"}
        props["line_last"] = {"type": "integer"}
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {"type": "object", "properties": props, "required": list(props), "additionalProperties": False},
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }


_INTRO_SENTENCE = (
    "You are shown one line from a filing (a sentence or a bullet) and the text around it. Decide whether it states "
    "numeric forward guidance, and list every metric it gives guidance for. If it gives none, return an empty list."
)
_INTRO_BLOCK = (
    "You are shown a whole guidance section: the lines under one heading such as \"Outlook\" or \"Q4 FY26 Guidance\", "
    "numbered [1], [2] and so on. Read it as a whole. In a table the label is on one line and its figures are on the "
    "lines after it, and a figure means nothing without its label. The heading or a line near the top says the period. "
    "The section may run on past the guidance into results or other text; ignore all of that. When the section is a "
    "table, a line marked [header] above [1] is the table's header row: it names the period of each column. List "
    "every metric the section gives guidance for. If it gives none, return an empty list."
)


def _rules(metrics: list[str], *, block: bool) -> str:
    listed = "; ".join(f'"{m}"' for m in metrics)
    where = (
        "\n- Each item says where it came from: line_first and line_last are the numbers of the first and last section "
        "lines that hold its label and its figure (the label line and the value line, or just the one line). Keep the "
        "span as short as it can be. Every number in the item must be printed in those lines."
        "\n- A [header] line, if there is one, is the table's header row; cells that were on separate lines are joined "
        "with \" | \". Its periods are the columns, left to right, and "
        "the figures under a label are in the same order, whether they share one line or follow on the lines after it. "
        "A column that says N/A or shows a dash has no figure. The header is context only: never cite it in line_first "
        "or line_last."
        if block else
        "\n- Every number in the item must be printed in the line to assess itself. A figure that is only in the lines "
        "before or after it belongs to that line, not to this one."
    )
    return f"""\
What counts
- Forward guidance only: a number the company itself expects, targets or forecasts for a future period. Anything in the past tense, or about a period that has already finished, returns nothing ("revenue was $89.0 billion", "returned $1.95 billion to shareholders", "grew 18%"). If the text is not forward guidance, return an empty list.
- Only these metrics, spelled exactly as written: {listed}. A figure for any other metric returns nothing. That includes gross profit, growth rates, segment revenue, remaining performance obligation, and capital return, share repurchases and dividends.
- "gross margin", "operating margin", "operating expenses" and "EPS" each have a GAAP and a non-GAAP entry: use the one the text names, and if a line gives both ("GAAP and non-GAAP gross margins are expected to be 74.8% and 75.0%, respectively") make one item for each. The other metrics have one entry each; if the text gives one figure for GAAP and non-GAAP together ("both expected to be income of approximately $20 million") make one item, and if it gives different figures use the GAAP one.
- Margins and rates are percentages of revenue: "gross margin", "operating margin" and "tax rate", unit percent. "Operating margin" is never "operating income". Amounts are dollars: "revenue", "operating income", "operating expenses", "other income and expense", "free cash flow", "total expenses" and "capital expenditures". "Total expenses" (all of the company's costs and expenses) is not "operating expenses", and "capital expenditures" (including principal payments on finance leases) is not "other income and expense" or "free cash flow". Growth in one of them ("free cash flow growth 9% - 10%") is neither the amount nor the margin, so it returns nothing.
- The period must be explicit somewhere in the text you are shown: the line, the lead-in line, the section heading, the lines around it, or the title. If you cannot tell which period a figure is for, return nothing for it. Do not guess the period.
- The number must be explicit. "Strong growth" and "a low-single digit decline" have no number, so they give nothing.

How to fill each item
- target_period: "FY2027" for a fiscal year, "Q3 FY2027" for a fiscal quarter. Use the company's own fiscal labels: "fiscal 2027", "fiscal year 2027" and "FY27" are all FY2027, and "third quarter of fiscal 2027" is Q3 FY2027. A company that calls its year "2026" (Target, for example) gives "the fourth quarter of 2026", which is Q4 FY2026. "Next quarter" or "the year" counts only when the text names the current quarter or year so that you can work it out.
- unit: one of {", ".join(UNITS)}. Take the unit as printed and do not rescale: "$46.1 billion" is 46.1 in USD billions.
- A range needs the word "to" or "between", a dash between two numbers in the same figure ("$11.13 - $11.23 billion", "9% - 10%"), or plus or minus language. Put the low end in value_low and the high end in value_high. Two separate figures are never a range. A single figure is a point: put it in both fields. "At least X", "X+", "X or more" and "X or better" give value_low only: "$1.30+" is value_low 1.30 and no value_high. "No more than X" gives value_high only. The other one is null.
- Two values joined by "and" or "respectively" are two point items, one per metric, not a range between them.
- plus_minus and plus_minus_kind: for "X, plus or minus N%" put X in both value fields, N in plus_minus and "percent". For "74.0%, plus or minus 50 basis points" put 74.0 in both value fields, 0.5 in plus_minus (the same unit as X) and "absolute". Otherwise plus_minus is null and plus_minus_kind is "none".
- Words like "approximately" and "about" do not change the numbers.
- If the same metric and period appears twice (previous guidance and updated guidance), return only the updated figure. If you cannot tell which is which, return neither. One item per metric and period.{where}
"""


_EX_SENTENCE = """\
Examples

Lead-in line: NVIDIA's outlook for the fourth quarter of fiscal 2026 is as follows:
Line to assess: Revenue is expected to be $65.0 billion, plus or minus 2%.
{"items":[{"metric":"revenue","unit":"USD billions","target_period":"Q4 FY2026","value_low":65.0,"value_high":65.0,"plus_minus":2.0,"plus_minus_kind":"percent"}]}

Lead-in line: NVIDIA's outlook for the fourth quarter of fiscal 2026 is as follows:
Line to assess: GAAP and non-GAAP gross margins are expected to be 74.8% and 75.0%, respectively, plus or minus 50 basis points.
{"items":[{"metric":"gross margin GAAP","unit":"percent","target_period":"Q4 FY2026","value_low":74.8,"value_high":74.8,"plus_minus":0.5,"plus_minus_kind":"absolute"},{"metric":"gross margin non-GAAP","unit":"percent","target_period":"Q4 FY2026","value_low":75.0,"value_high":75.0,"plus_minus":0.5,"plus_minus_kind":"absolute"}]}

Lead-in line: NVIDIA's outlook for the fourth quarter of fiscal 2026 is as follows:
Line to assess: GAAP and non-GAAP other income and expense are expected to be an income of approximately $500 million.
{"items":[{"metric":"other income and expense","unit":"USD millions","target_period":"Q4 FY2026","value_low":500.0,"value_high":500.0,"plus_minus":null,"plus_minus_kind":"none"}]}

Lines before: "For the fourth quarter of 2025, the Company is maintaining its expectation of a low-single digit decline in sales."
Line to assess: Full-year GAAP EPS is now expected to be approximately $7.70 to $8.70.
{"items":[{"metric":"EPS GAAP","unit":"USD per share","target_period":"FY2025","value_low":7.7,"value_high":8.7,"plus_minus":null,"plus_minus_kind":"none"}]}

Lead-in line: Example Corp's outlook for the third quarter of fiscal 2031 is as follows:
Line to assess: GAAP and non-GAAP operating margins are expected to be 21.5% and 33.0%, respectively.
{"items":[{"metric":"operating margin GAAP","unit":"percent","target_period":"Q3 FY2031","value_low":21.5,"value_high":21.5,"plus_minus":null,"plus_minus_kind":"none"},{"metric":"operating margin non-GAAP","unit":"percent","target_period":"Q3 FY2031","value_low":33.0,"value_high":33.0,"plus_minus":null,"plus_minus_kind":"none"}]}

Line to assess: For the second quarter of fiscal 2031, the Company expects a 1 to 3 percent increase in its comparable sales.
Lines after:
  For the full year, the Company expects GAAP EPS of $9.10 to $9.90.
{"items":[{"metric":"comparable sales","unit":"percent","target_period":"Q2 FY2031","value_low":1.0,"value_high":3.0,"plus_minus":null,"plus_minus_kind":"none"}]}

Line to assess: Second-quarter revenue was $89.0 billion, up 18% from the previous quarter and up 117% from a year ago.
{"items":[]}

Line to assess: For fiscal 2030 and 2029, the Company used a tax rate of 23.5% and 22.0%, respectively.
{"items":[]}

Line to assess: NVIDIA intends to return $3.00 billion to shareholders in fiscal 2020 through share repurchases and dividends.
{"items":[]}

Line to assess: For the fourth quarter of 2025, the Company is maintaining its expectation of a low-single digit decline in sales.
{"items":[]}
"""

_EX_BLOCK = """\
Examples

Section heading: Q4 FY26 Guidance
Section lines:
  [1] GAAP
  [2] Non-GAAP(1)
  [3] Revenue
  [4] $11.13 - $11.23 billion
  [5] Revenue growth(2)
  [6] 11% - 12%
  [7] Diluted net income per share
  [8] $1.47 - $1.49 $3.02 - $3.04
{"items":[{"metric":"revenue","unit":"USD billions","target_period":"Q4 FY2026","value_low":11.13,"value_high":11.23,"plus_minus":null,"plus_minus_kind":"none","line_first":3,"line_last":4},{"metric":"EPS GAAP","unit":"USD per share","target_period":"Q4 FY2026","value_low":1.47,"value_high":1.49,"plus_minus":null,"plus_minus_kind":"none","line_first":7,"line_last":8},{"metric":"EPS non-GAAP","unit":"USD per share","target_period":"Q4 FY2026","value_low":3.02,"value_high":3.04,"plus_minus":null,"plus_minus_kind":"none","line_first":7,"line_last":8}]}

Section heading: GAAP diluted earnings per share guidance
Section lines:
  [header] Q3 2031 Full Year 2031
  [1] $2.10 - $2.40 $9.00 - $10.00
  [2] Estimated adjustments
{"items":[{"metric":"EPS GAAP","unit":"USD per share","target_period":"Q3 FY2031","value_low":2.1,"value_high":2.4,"plus_minus":null,"plus_minus_kind":"none","line_first":1,"line_last":1},{"metric":"EPS GAAP","unit":"USD per share","target_period":"FY2031","value_low":9.0,"value_high":10.0,"plus_minus":null,"plus_minus_kind":"none","line_first":1,"line_last":1}]}

Section heading: Adjusted diluted earnings per share guidance
Section lines:
  [header] Q1 2031 | Full Year 2031
  [1] $1.10+(a)
  [2] $6.00 - $7.00
{"items":[{"metric":"EPS non-GAAP","unit":"USD per share","target_period":"Q1 FY2031","value_low":1.1,"value_high":null,"plus_minus":null,"plus_minus_kind":"none","line_first":1,"line_last":1},{"metric":"EPS non-GAAP","unit":"USD per share","target_period":"FY2031","value_low":6.0,"value_high":7.0,"plus_minus":null,"plus_minus_kind":"none","line_first":2,"line_last":2}]}

Section heading: Full Year FY31 Guidance
Section lines:
  [1] GAAP
  [2] Non-GAAP(1)
  [3] Revenue
  [4] $30.0 - $30.4 billion
  [5] Operating margin 18.0% 30.5%
  [6] Free cash flow growth Approximately 8% - 9%
{"items":[{"metric":"revenue","unit":"USD billions","target_period":"FY2031","value_low":30.0,"value_high":30.4,"plus_minus":null,"plus_minus_kind":"none","line_first":3,"line_last":4},{"metric":"operating margin GAAP","unit":"percent","target_period":"FY2031","value_low":18.0,"value_high":18.0,"plus_minus":null,"plus_minus_kind":"none","line_first":5,"line_last":5},{"metric":"operating margin non-GAAP","unit":"percent","target_period":"FY2031","value_low":30.5,"value_high":30.5,"plus_minus":null,"plus_minus_kind":"none","line_first":5,"line_last":5}]}

Section heading: Outlook
Section lines:
  [1] NVIDIA's outlook for the fourth quarter of fiscal 2026 is as follows:
  [2] Revenue is expected to be $65.0 billion, plus or minus 2%.
  [3] GAAP and non-GAAP tax rates are expected to be 17.0%, plus or minus 1%, excluding any discrete items.
  [4] Highlights
  [5] Third-quarter revenue was a record $51.2 billion, up 25% from the previous quarter.
{"items":[{"metric":"revenue","unit":"USD billions","target_period":"Q4 FY2026","value_low":65.0,"value_high":65.0,"plus_minus":2.0,"plus_minus_kind":"percent","line_first":2,"line_last":2},{"metric":"tax rate","unit":"percent","target_period":"Q4 FY2026","value_low":17.0,"value_high":17.0,"plus_minus":1.0,"plus_minus_kind":"absolute","line_first":3,"line_last":3}]}

Section heading: Guidance
Section lines:
  [1] Second-quarter revenue was $89.0 billion, up 18% from the previous quarter.
  [2] Gross margin 75.0 % 73.4 %
{"items":[]}
"""


def system_prompt(metrics: list[str], *, block: bool) -> str:
    return "\n\n".join([
        "You extract numeric forward guidance from SEC filings of US public companies.",
        _INTRO_BLOCK if block else _INTRO_SENTENCE,
        _rules(metrics, block=block),
        _EX_BLOCK if block else _EX_SENTENCE,
    ])


# --- building requests -----------------------------------------------------


def load_candidates(company: Company, filing_types: set[str], directory: Path | None = None) -> list[dict[str, Any]]:
    path = (directory or CANDIDATES_DIR) / f"{company.cik}.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if r["filing_type"] in filing_types]


def is_block(candidate: dict[str, Any]) -> bool:
    return candidate["capture_method"] == "section"


def custom_id(candidate: dict[str, Any]) -> str:
    prefix = "b" if is_block(candidate) else "c"
    return f"{prefix}-{candidate['cik']}-{candidate['accession'].replace('-', '')}-{candidate['char_start']}"


_TITLE_WORDS = re.compile(r"\b(results|reports|announces|earnings)\b", re.IGNORECASE)


_NAMES_A_QUARTER = re.compile(r"\bquarter\b|\b(?:F|FQ)?Q[1-4]\b|\b[1-4]Q\b", re.IGNORECASE)
HEADLINE_LINES = 3  # a headline that wraps is spread over at most this many lines
HEADLINE_CHARS = 300


def document_title(text: str, *, wrap: bool = False) -> str | None:
    """The release headline, if one is near the top: the first line that reads like one.

    With `wrap`, a headline that has not named a quarter yet is continued onto the lines after it, up to three lines
    in all and 300 characters: Micron's "MICRON TECHNOLOGY, INC. REPORTS RESULTS FOR THE" ends there, and "FIRST
    QUARTER OF FISCAL 2025" is the next line. Once the headline names a quarter it stops, so a subtitle further down
    ("Raises fourth quarter outlook") is never read as part of it. Only the outcome search wraps; the prompt in
    `build_prompt` shows the first line alone, as it always did, so no archived answer is made stale by this."""
    lines = text.split("\n")
    for i, line in enumerate(lines[:25]):
        if _TITLE_WORDS.search(line) and 4 <= len(line.split()) <= 30:
            if not wrap:
                return line
            headline = line
            for extra in lines[i + 1 : i + HEADLINE_LINES]:
                if _NAMES_A_QUARTER.search(headline) or not extra.strip():
                    break
                headline = f"{headline} {extra.strip()}"
            return headline[:HEADLINE_CHARS]
    return None


def _block_of(label: str, items: list[str]) -> str:
    return f"{label}:\n" + "\n".join(f"  {i}" for i in items) if items else f"{label}: (none)"


def build_prompt(candidate: dict[str, Any], company: Company, title: str | None) -> str:
    """A sentence request."""
    return "\n".join(
        [
            f"Company: {company.name} ({company.ticker})",
            f"Filing: {candidate['filing_type']} exhibit, filed {candidate['filed_at']}",
            f"Title: {title}" if title else "Title: (not found)",
            f"Section heading: {candidate.get('heading') or '(none)'}",
            f"Lead-in line: {candidate.get('lead_in') or '(none)'}",
            _block_of("Lines before", candidate["context_before"]),
            f"Line to assess: {candidate['sentence']}",
            _block_of("Lines after", candidate["context_after"]),
        ]
    )


def build_block_prompt(candidate: dict[str, Any], company: Company, title: str | None) -> str:
    """A section request: every line under the heading, numbered, so a label and its figure are read together."""
    numbered = "\n".join(f"  [{i}] {line}" for i, line in enumerate(candidate["sentence"].split("\n"), 1))
    if candidate.get("table_header"):  # the header row names each column's period; it is not a numbered line
        numbered = f"  [header] {candidate['table_header']}\n{numbered}"
    return "\n".join(
        [
            f"Company: {company.name} ({company.ticker})",
            f"Filing: {candidate['filing_type']} exhibit, filed {candidate['filed_at']}",
            f"Title: {title}" if title else "Title: (not found)",
            f"Section heading: {candidate['heading']}",
            _block_of("Text before the section", candidate["context_before"]),
            "Section lines:\n" + numbered,
            _block_of("Text after the section", candidate["context_after"]),
        ]
    )


def pick_evenly(items: list[Any], k: int) -> list[Any]:
    """k items spread evenly across the list, always distinct, in order."""
    n = len(items)
    if k >= n:
        return list(items)
    return [items[int((i + 0.5) * n / k)] for i in range(max(k, 0))]


def sample_requests(pairs: list[tuple[Any, dict[str, Any]]], k: int) -> list[tuple[Any, dict[str, Any]]]:
    """k requests for one company, spread across its filings: half sections, half sentences where it has them,
    so a pilot exercises both kinds. Deterministic."""
    blocks = [p for p in pairs if is_block(p[1])]
    sentences = [p for p in pairs if not is_block(p[1])]
    want_blocks = min(len(blocks), k // 2)
    want_sentences = min(len(sentences), k - want_blocks)
    want_blocks = min(len(blocks), k - want_sentences)
    chosen = pick_evenly(blocks, want_blocks) + pick_evenly(sentences, want_sentences)
    position = {id(p): i for i, p in enumerate(pairs)}
    return sorted(chosen, key=lambda p: position[id(p)])


def build_requests(
    targets: list[Company],
    filing_types: set[str],
    *,
    candidates_dir: Path | None = None,
    limit: int | None = None,
    per_company: int | None = None,
    metrics: list[str] | None = None,
) -> tuple[list[llm.LlmRequest], dict[str, tuple[Company, dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """(requests, custom_id -> (company, candidate), skipped rows per cik)."""
    metrics = metrics or load_config()["metrics"]
    sentence_system, block_system = system_prompt(metrics, block=False), system_prompt(metrics, block=True)
    sentence_schema, block_schema = item_schema(metrics, block=False), item_schema(metrics, block=True)
    requests: list[llm.LlmRequest] = []
    index: dict[str, tuple[Company, dict[str, Any]]] = {}
    skipped: dict[str, list[dict[str, Any]]] = {c.cik: [] for c in targets}
    titles: dict[tuple[str, str], str | None] = {}

    for company in targets:
        seen: set[tuple[str, str]] = set()
        pairs: list[tuple[llm.LlmRequest, dict[str, Any]]] = []
        for cand in load_candidates(company, filing_types, candidates_dir):
            cid = custom_id(cand)
            if not is_block(cand) and len(cand["sentence"]) > EXCERPT_LIMIT:
                skipped[company.cik].append({"custom_id": cid, "reason": f"sentence is {len(cand['sentence'])} chars, over the {EXCERPT_LIMIT} char excerpt limit"})
                continue
            key = (company.cik, cand["accession"])
            if key not in titles:
                html_path, _ = raw_paths(company.cik, cand["accession"])
                titles[key] = document_title(html_to_text(html_path.read_bytes())) if html_path.exists() else None
            user = (build_block_prompt if is_block(cand) else build_prompt)(cand, company, titles[key])
            if (cand["accession"], user) in seen:
                skipped[company.cik].append({"custom_id": cid, "reason": "identical prompt already sent for this filing"})
                continue
            seen.add((cand["accession"], user))
            if is_block(cand):
                request = llm.LlmRequest(cid, block_system, user, MAX_TOKENS_BLOCK, block_schema)
            else:
                request = llm.LlmRequest(cid, sentence_system, user, MAX_TOKENS_SENTENCE, sentence_schema)
            pairs.append((request, cand))
        if per_company is not None:
            pairs = sample_requests(pairs, per_company)
        for request, cand in pairs:
            requests.append(request)
            index[request.custom_id] = (company, cand)
    if limit is not None:
        requests = requests[:limit]
        index = {r.custom_id: index[r.custom_id] for r in requests}
    return requests, index, skipped


# --- turning model output into drafts --------------------------------------

_PLUS_MINUS = re.compile(r"plus or minus|plus/minus|plus-or-minus|\u00b1|\+/-", re.IGNORECASE)
_BETWEEN = re.compile(r"\bbetween\s+[~$]?\s*\d", re.IGNORECASE)
_DASH = re.compile(r"[\d%]\s*[-\u2013\u2014]\s*[~$]?\s*\d")
_TO = re.compile(r"\bto\b", re.IGNORECASE)


def has_range_language(text: str) -> bool:
    """'X to Y', 'between X and Y', or 'X - Y' with figures on both sides. A bare "to" is not enough:
    "are expected to be approximately $6.7 billion and $5.0 billion" is prose, not a range."""
    if _BETWEEN.search(text) or _DASH.search(text):
        return True
    return any(
        re.search(r"\d", text[max(0, m.start() - 24) : m.start()]) and re.match(r"\s*[~$]?\s*\d", text[m.end() : m.end() + 6])
        for m in _TO.finditer(text)
    )


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


_AMOUNT_WORD = {"thousand": 1e3, "k": 1e3, "million": 1e6, "m": 1e6, "billion": 1e9, "b": 1e9}
_UNIT_BASE = {"USD": 1.0, "USD per share": 1.0, "USD thousands": 1e3, "USD millions": 1e6, "USD billions": 1e9}
_AMOUNT = r"\$\s*(?P<{n}>\d[\d,]*(?:\.\d+)?)\s*(?P<{w}>billion|million|thousand|B|M|K)?\b"
# "$1.211 billion \u00b1 $15 million", "$0.42 \u00b1 $0.07", "$18.90 plus or minus $0.40": a currency centre, then a currency spread.
_CURRENCY_PLUS_MINUS = re.compile(
    _AMOUNT.format(n="centre", w="cw") + r"\s*(?:\u00b1|\+\s*/\s*-|plus or minus)\s*" + _AMOUNT.format(n="spread", w="sw"), re.IGNORECASE
)


def with_currency_spread_absolute(metric: str, item: dict[str, Any], evidence: str, kinds: dict[str, str]) -> tuple[dict[str, Any], str | None]:
    """A plus or minus followed by a currency amount ("$0.42 \u00b1 $0.07", "$1.13 billion \u00b1 $25 million") is an absolute
    spread in dollars, never a percent of the centre, and it is worked out in the metric's own unit: $25 million on a
    figure given in USD billions is 0.025. The model is told this, and it gets it right on the pilot filings, but a
    spread read as a percent (0.42 \u00b1 0.07% is 0.4197 to 0.4203) or left unscaled (1.13 \u00b1 25 billion) would be a wrong
    range that nothing downstream notices, so it is settled here from the printed text. Only a dollar or per-share
    metric, and only when the centre figure in the evidence is the item's own value; anything else is left as it was.

    Returns (item, note): note says what was changed, or is None if the item already agreed with the text."""
    if kinds.get(metric) not in ("dollars", "per share") or item.get("plus_minus_kind", "none") == "none":
        return item, None
    base = _UNIT_BASE.get(item.get("unit", ""))
    centre = item.get("value_low")
    if base is None or centre is None or item.get("value_high") != centre:
        return item, None
    for m in _CURRENCY_PLUS_MINUS.finditer(evidence):
        if round(float(m["centre"].replace(",", "")), 6) != round(centre, 6):
            continue
        centre_word = _AMOUNT_WORD.get((m["cw"] or "").lower(), 1.0)
        spread_word = _AMOUNT_WORD.get((m["sw"] or "").lower(), centre_word if m["sw"] is None and item["unit"] != "USD per share" else 1.0)
        spread = float(m["spread"].replace(",", "")) * spread_word / base
        if item["plus_minus_kind"] == "absolute" and item.get("plus_minus") is not None and abs(item["plus_minus"] - spread) < 1e-9 * max(1.0, abs(spread)):
            return item, None
        return {**item, "plus_minus": _round(spread), "plus_minus_kind": "absolute"}, (
            f"the plus or minus is a currency amount in the evidence, so it is absolute: {m['spread']}"
            f"{' ' + m['sw'] if m['sw'] else ''} is {_round(spread):g} in {item['unit']}")
    return item, None


def with_absolute_percent_spread(metric: str, item: dict[str, Any], kinds: dict[str, str]) -> dict[str, Any]:
    """A percent metric's own spread is always absolute, whatever the model called it: "8 percent, plus or
    minus 1 percent" is 7 to 9 percentage points, never 8 relative to itself (7.92 to 8.08, a pilot review
    found the model give this inconsistently, the identical phrasing correctly "absolute" elsewhere in the same
    filing). Relative % ("$108.0 billion, plus or minus 2%") only makes sense on a dollar metric, where the
    percent is of the dollar figure; a percent metric has nothing else for its own percent to be relative to."""
    if kinds.get(metric) == "percent" and item.get("plus_minus_kind") == "percent":
        return {**item, "plus_minus_kind": "absolute"}
    return item


_PM_MARK = r"\s*[,;]?\s*(?:\u00b1|\+\s*/\s*-|plus or minus|plus/minus|plus-or-minus)\s*"
_PM_CURRENCY_CENTRE = r"\$\s*(?P<c>\d[\d,]*(?:\.\d+)?)\s*(?P<cw>billion|million|thousand|B|M|K)?\b"
_PM_PERCENT_UNIT = r"(?:%|percent\b|per\s*cent\b)"
_PM_SPREAD_PERCENT = r"(?P<s>\d[\d,]*(?:\.\d+)?)\s*(?P<su>%|percentage\s+points?|percent\b|per\s*cent\b|basis\s+points?|bps\b)"
_PRINTED_PLUS_MINUS = [
    # "$1.52 billion, plus or minus $50 million" / "$0.42 \u00b1 $0.07"
    ("currency", re.compile(_PM_CURRENCY_CENTRE + _PM_MARK + r"\$\s*(?P<s>\d[\d,]*(?:\.\d+)?)\s*(?P<sw>billion|million|thousand|B|M|K)?\b", re.IGNORECASE)),
    # "$108.0 billion, plus or minus 2%": a relative spread on a dollar centre
    ("currency_rel", re.compile(_PM_CURRENCY_CENTRE + _PM_MARK + _PM_SPREAD_PERCENT, re.IGNORECASE)),
    # "41%, plus or minus 1%" / "62.3 percent \u00b1 50 basis points"
    ("percent", re.compile(r"(?P<c>\d[\d,]*(?:\.\d+)?)\s*" + _PM_PERCENT_UNIT + _PM_MARK + _PM_SPREAD_PERCENT, re.IGNORECASE)),
]


def _stated_centre(item: dict[str, Any]) -> float | None:
    low, high = item.get("value_low"), item.get("value_high")
    if low is None or high is None:
        return None
    return (low + high) / 2


def with_printed_plus_minus(metric: str, item: dict[str, Any], evidence: str, kinds: dict[str, str]) -> tuple[dict[str, Any], str | None]:
    """A plus or minus the evidence prints in full (a centre and a spread, side by side) decides the range, by code,
    whatever endpoints or spread the model returned: "$1.52 billion, plus or minus $50 million" is 1.47 to 1.57 in
    USD billions, "41%, plus or minus 1%" is 40 to 42, "62.3%, plus or minus 50 bps" is 61.8 to 62.8. The model
    sometimes returns the endpoints (so the numbers-in-evidence check finds 1.47 nowhere in the text) or a malformed
    centre and spread; either is replaced. The item gets `_printed`, the centre and spread exactly as printed, and
    check_numbers_in_evidence then validates those two figures against the text instead of the endpoints.

    Bound to the item only when the printed centre is the figure the model was after (its centre, or the midpoint
    of its range): an evidence line with two plus-or-minus statements, or one for another metric, is left alone.
    A currency spread is absolute and worked out in the metric's own unit; a percent spread on a currency centre
    is relative; on a percent metric a percent or percentage-point spread is absolute points and basis points are
    a hundredth of one. Returns (item, note); note is None when nothing changed."""
    kind = kinds.get(metric)
    want = {"dollars": ("currency", "currency_rel"), "per share": ("currency", "currency_rel"), "percent": ("percent",)}.get(kind)
    wanted = _stated_centre(item)
    if want is None or wanted is None or not _PLUS_MINUS.search(evidence):
        return item, None
    base = _UNIT_BASE.get(item.get("unit", ""))
    for label, pattern in _PRINTED_PLUS_MINUS:
        if label not in want:
            continue
        for m in pattern.finditer(evidence):
            centre_printed = float(m["c"].replace(",", ""))
            centre_word = _AMOUNT_WORD.get((m.groupdict().get("cw") or "").lower(), 1.0)
            if label == "percent":
                centre, spread_kind = centre_printed, "absolute"
            elif base is None:
                continue
            else:
                centre, spread_kind = centre_printed * centre_word / base, "absolute"
            if abs(centre - wanted) > 1e-6 * max(1.0, abs(wanted)):
                continue
            spread_printed = float(m["s"].replace(",", ""))
            if label == "currency":
                spread_word = _AMOUNT_WORD.get((m["sw"] or "").lower(), centre_word if m["sw"] is None and item.get("unit") != "USD per share" else 1.0)
                spread = spread_printed * spread_word / base
            elif label == "currency_rel":
                spread, spread_kind = spread_printed, "percent"
            else:
                spread = spread_printed / 100 if m["su"].lower().startswith(("basis", "bps")) else spread_printed
            new = {**item, "value_low": _round(centre), "value_high": _round(centre), "plus_minus": _round(spread),
                   "plus_minus_kind": spread_kind, "_printed": (centre_printed, spread_printed)}
            changed = any(new[k] != item.get(k) for k in ("value_low", "value_high", "plus_minus", "plus_minus_kind"))
            note = (f"the range was worked out from the printed plus or minus ({m.group().strip()!r}), not taken from the model; the spread is {spread_kind}"
                    if changed else None)
            return new, note
    return item, None


def check_range_language(item: dict[str, Any], evidence: str) -> None:
    """A range must be in the words. Whatever the model was told, two separate figures ("$755 million" and
    "$915 million") never become 755 to 915, and a plus or minus needs plus or minus in the evidence."""
    if item["plus_minus_kind"] != "none":
        if not _PLUS_MINUS.search(evidence):
            raise ValueError("a plus or minus range, but the evidence has no plus or minus language")
    elif item["value_low"] is not None and item["value_high"] is not None and item["value_low"] != item["value_high"]:
        if not has_range_language(evidence):
            raise ValueError("a range, but the evidence has no 'to', 'between', dash or plus or minus")


# --- guards that do not trust the model --------------------------------------


def check_unit_kind(metric: str, unit: str, kinds: dict[str, str]) -> None:
    """The unit has to be the kind of thing the metric is. Dollar metrics reject a percent, percent metrics
    reject dollars, and EPS is dollars per share and nothing else."""
    want, got = kinds.get(metric), UNIT_KIND.get(unit)
    if want is not None and got != want:
        raise ValueError(f"{metric!r} is measured in {want}, but the unit is {unit!r} ({got or 'unknown'})")


# "grow"/"grows"/"growing"/"growth" in one group; "increase of" and "decline of" name a change directly; "up"/
# "down" followed by a number and a percent sign name one implicitly ("revenue up 12 percent"). None of these
# say what the metric will BE, only how it will move.
_CHANGE_WORDS = re.compile(
    r"\bgrow(?:s|ing|th)?\b|\bincrease of\b|\bdecline of\b"
    r"|\b(?:up|down)\s+(?:approximately\s+|about\s+|roughly\s+)?[\d.,]+\s*(?:%|percent|per\s*cent)\b",
    re.IGNORECASE,
)


def check_not_a_change(metric: str, values: tuple[float | None, float | None], evidence: str, kinds: dict[str, str]) -> None:
    """A dollar metric's guidance has to be a level, not a change in one: "operating income is expected to grow
    more than $1 billion" says how much MORE, not what operating income will be, so it is not a level the rubric
    can resolve a reported dollar figure against. A percent metric is unaffected: "comparable sales growth of 2
    to 3 percent" IS the metric, not a change in it, so growth words there are not a change-not-level problem.

    Scoped to the sentence that actually prints one of `values` (the item's own value_low/value_high), not the
    whole excerpt: a section bullet often runs on past the figure into colour about a different subject ("Gaming
    ... revenue are expected to decline sequentially ... offset by ... growth in Data Center"), and that must
    not disqualify a clean, unrelated dollar figure stated earlier in the same bullet."""
    if kinds.get(metric) != "dollars":
        return
    printed = {round(abs(v), 6) for v in values if v is not None}
    if not printed:
        return
    for start, end in sentence_spans(evidence):
        sentence = evidence[start:end]
        if numbers_in(sentence) & printed and _CHANGE_WORDS.search(sentence):
            raise ValueError(f"{metric!r} is a dollar level, but the evidence describes a change (growth, an increase or a decrease), not a level")


# "higher/lower than", "above"/"below", "compared to" and "versus"/"vs" all set the figure against a prior
# period rather than stating it outright: "20 basis points higher than the 4.6 percent rate in 2025" never says
# what the new rate itself is.
_RELATIVE_TO_PRIOR = re.compile(
    r"\b(?:higher|lower)\s+than\b|\babove\b|\bbelow\b|\bcompared to\b|\bversus\b|\bvs\.?\b",
    re.IGNORECASE,
)
_PERCENT_SIGN_NUMBER = re.compile(r"[\d.,]+\s*(?:%|percent\b|per\s*cent\b)", re.IGNORECASE)


def _percent_figures_printed(text: str) -> set[float]:
    """Numbers printed with a percent sign or the word "percent"/"per cent" right after them: an absolute rate,
    as opposed to a bare basis-point or point delta ("20 basis points higher") that names no rate of its own."""
    found: set[float] = set()
    for match in _PERCENT_SIGN_NUMBER.finditer(text):
        number = _NUMBER_TOKEN.search(match.group())
        if number:
            try:
                found.add(round(float(number.group().replace(",", "")), 6))
            except ValueError:
                continue
    return found


def check_not_relative_to_prior_period(metric: str, values: tuple[float | None, float | None], evidence: str, kinds: dict[str, str]) -> None:
    """A percent metric's guidance has to state its own level, not just how it compares to a prior period's:
    "Full-year 2026 operating income margin rate approximately 20 basis points higher than the 4.6 percent ...
    rate in 2025" never says what the new rate itself is, only the size of the move and the OLD rate. Rejected
    unless the item's own figure is also printed as an explicit percent somewhere in the same sentence: "operating
    margin of 21%, compared to 19% last year" states its own level too, so the comparison wording is just colour.
    Scoped to the sentence that prints the item's own number, the same as check_not_a_change."""
    if kinds.get(metric) != "percent":
        return
    printed = {round(v, 6) for v in values if v is not None}
    if not printed:
        return
    for start, end in sentence_spans(evidence):
        sentence = evidence[start:end]
        if numbers_in(sentence) & printed and _RELATIVE_TO_PRIOR.search(sentence):
            if not (_percent_figures_printed(sentence) & printed):
                raise ValueError(f"{metric!r} is stated relative to a prior period, and its own absolute figure is not printed")


_NUMBER_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?")
# A footnote marker stuck to a word or another marker: "margin(1)", "range(1)(2)". "($0.03)" and "(200)" are values.
_FOOTNOTE = re.compile(r"(?<=[A-Za-z)])\(\d{1,2}\)")


def numbers_in(text: str) -> set[float]:
    """Every number printed in the text, as a float. Thousands separators and footnote markers do not count."""
    found: set[float] = set()
    for match in _NUMBER_TOKEN.finditer(_FOOTNOTE.sub("", text)):
        try:
            found.add(round(float(match.group().replace(",", "")), 6))
        except ValueError:
            continue
    return found


def check_numbers_in_evidence(item: dict[str, Any], evidence: str) -> None:
    """Every number the item states (its low and high, or the centre of a plus or minus) must be printed in its
    evidence. The sign is not compared: "($0.03)" and "an expense of $10 million" are printed without one.
    An answer taken from the line before or after the evidence would have nothing in its excerpt to support it."""
    printed = numbers_in(evidence)
    if item.get("_printed"):  # a plus or minus read from the text: the centre and the spread as printed are what has to be there
        for value in item["_printed"]:
            if round(abs(value), 6) not in printed:
                raise ValueError(f"{value:g} (the printed centre or spread of a plus or minus) is not in the evidence text")
        return
    for key in ("value_low", "value_high"):
        value = item[key]
        if value is not None and round(abs(value), 6) not in printed:
            raise ValueError(f"{key} {value:g} is not in the evidence text")


_PAREN_NUMBER = re.compile(r"\(\s*[^()]*\d[^()]*\)")
_PARENS_CAN_BE_NEGATIVE = re.compile(r"\bnegative\b|\bbenefit\b|\bloss\b", re.IGNORECASE)


def fix_parens_sign(
    metric: str, low: float | None, high: float | None, evidence: str, kinds: dict[str, str], *, tax_rate_parens_negative: bool = False
) -> tuple[float | None, float | None, str | None]:
    """Parentheses are accounting notation for a negative number, but on a rate (a margin, a tax rate,
    comparable sales) a company also prints one around a figure that is unusual or worth calling out, with no
    loss meant by it: a GAAP tax provision of "(146%)" is a rate over 100%, not a rate of -146%. Flip such a
    metric's negative value back to positive unless the evidence itself says negative, benefit or loss. EPS and
    other income are dollar amounts where a loss is a common, real thing to guide to, so their parentheses are
    always left negative. What decides which rule applies is the metric's own kind, from config.yaml's
    metric_kinds ("percent" or not), never a hardcoded list of metric names that could drift out of sync with it.
    The one exception is the tax rate, whose rule is the filer's own: a company with `tax_rate_parens_negative: true`
    in config.yaml prints a benefit or an unusually low rate in parentheses, and keeps the sign the model read.
    A range whose upper end is zero or below ("(0.5%) to 0.0%", "(2.5%) to (2.0%)") is left negative too: nobody
    guides to a rate range of 0.0 to 0.5 by writing the low end in parentheses and the high end as plain zero.

    Returns (low, high, note): note says the rule fired, or is None if nothing changed."""
    if kinds.get(metric) != "percent" or (metric == "tax rate" and tax_rate_parens_negative):
        return low, high, None
    if not ((low is not None and low < 0) or (high is not None and high < 0)):
        return low, high, None
    if not _PAREN_NUMBER.search(evidence) or _PARENS_CAN_BE_NEGATIVE.search(evidence):
        return low, high, None
    if low is not None and high is not None and low != high and high <= 0:
        return low, high, None  # "(0.5%) to 0.0%": a range ending at zero or below is a negative range, not an unusual positive rate
    flipped = sorted(abs(v) for v in (low, high) if v is not None)
    new_low = flipped[0] if low is not None else None
    new_high = flipped[-1] if high is not None else None
    note = (f"{metric!r} is a rate: a parenthesised figure in the evidence was read as positive, not negative, "
            f"because the evidence has no 'negative', 'benefit' or 'loss' wording")
    return new_low, new_high, note


# A cell with no figure: a bare "N/A", or an em or en dash that is a cell of its own (a hyphen is a range's dash).
_EMPTY_CELL = re.compile(r"^(?:n/?a|[\u2014\u2013])$", re.IGNORECASE)
_VALUE_CELL = re.compile(r"^(?:[~$(\-]|\+)*\$?\d|^(?:approximately|approx\.?|about|slightly)$", re.IGNORECASE)


def leading_empty_cells(evidence: str) -> int:
    """How many empty cells (N/A) sit directly before the first figure of the evidence: "Non-GAAP operating margin
    N/A ~17.7%" has one. Cells are what whitespace and line breaks separate, so a row printed on one line and one
    broken into a label line, an N/A line and a value line count the same."""
    tokens = evidence.split()
    for i, token in enumerate(tokens):
        if _VALUE_CELL.match(token):
            count = 0
            while i - 1 - count >= 0 and _EMPTY_CELL.match(tokens[i - 1 - count]):
                count += 1
            return count
    return 0


def _period_label(period: tuple[int | None, int]) -> str:
    quarter, year = period
    return f"Q{quarter} FY{year}" if quarter else f"FY{year}"


def fix_na_column_period(period: str, candidate: dict[str, Any], evidence: str) -> tuple[str, str | None]:
    """A value that sits after an N/A cell belongs to the table's column it sits under, not to the row's first column.
    Salesforce prints a quarter column and a full-year column side by side, and for a metric guided only for the year
    the quarter cell is N/A ("Non-GAAP operating margin N/A ~17.7%"): the figure is in the second column, the header
    says which period that is, and the row's own text does not. When the evidence has k empty cells before its figure,
    the header has more than k periods, and the model's period is one of the k columns that were empty, the period
    is the header's period at column k+1 instead. Anything else (no header, no N/A, a period the header does not
    name) is left as the model said it.

    Returns (period, note): note says the rule fired, or is None if nothing changed."""
    header = candidate.get("table_header")
    skipped = leading_empty_cells(evidence)
    if not header or not skipped:
        return period, None
    columns = [_period_label(p) for p in candidates_step.parse_periods(header)]
    if len(columns) <= skipped or period not in columns[:skipped]:
        return period, None
    bound = columns[skipped]
    return bound, (f"the figure follows {skipped} N/A cell{'s' if skipped > 1 else ''}, so it sits under the table's "
                   f"{bound} column and not {period}, the column that is N/A")


PERIOD_BEFORE_STATED = "period closes before stated; likely next quarter"
_FISCAL_PERIOD = re.compile(r"^(?:Q([1-4]) )?FY(\d{4})$")


def next_fiscal_period(period: str) -> str | None:
    """The fiscal period after this one: Q3 FY2020 is followed by Q4 FY2020, Q4 FY2020 by Q1 FY2021, FY2020 by FY2021."""
    m = _FISCAL_PERIOD.match(period)
    if not m:
        return None
    year = int(m.group(2))
    if not m.group(1):
        return f"FY{year + 1}"
    quarter = int(m.group(1))
    return f"Q{quarter + 1} FY{year}" if quarter < 4 else f"Q1 FY{year + 1}"


def period_closes_before_stated(company: Company, period: str, stated_at: date) -> str | None:
    """A guidance statement cannot be about a period that was already over when it was made. Micron's March 2020 release
    guides the third quarter, and a draft labelled Q2 FY2020 (which closed on February 29) is the quarter before the one
    it is about: the label is off by one. The draft is kept, not rejected or corrected: the note puts it in front of a
    reviewer, and 03c_suggest names the next fiscal quarter as the likely one. Nothing here approves anything, and
    nothing is known without the company's fiscal calendar.

    Returns the flag text, or None when the period is still open at `stated_at` (or cannot be dated)."""
    m = _FISCAL_PERIOD.match(period)
    if not m:
        return None
    close = company.period_end(int(m.group(2)), int(m.group(1)) if m.group(1) else None)
    return PERIOD_BEFORE_STATED if close is not None and close < stated_at else None


_EXPENSE_OF = re.compile(r"\bexpense of\b", re.IGNORECASE)


def fix_expense_of_sign(metric: str, low: float | None, high: float | None, evidence: str) -> tuple[float | None, float | None, str | None]:
    """"Other income and expense" is a net dollar figure: positive is net income, negative is net expense.
    Evidence that says "an expense of $X million" is guiding to a net expense, whatever sign the model gave
    the number: the model reads "expense of" inconsistently, printing the same phrasing as positive on one
    draft and negative on another. Force it negative by code instead. Only this one metric is touched: EPS
    and operating income are guided to a loss in their own right and are not read this way.

    Returns (low, high, note): note says the rule fired, or is None if nothing changed."""
    if metric != "other income and expense" or not _EXPENSE_OF.search(evidence):
        return low, high, None
    if (low is None or low <= 0) and (high is None or high <= 0):
        return low, high, None  # already negative (or absent): nothing to flip
    flipped = sorted(-abs(v) for v in (low, high) if v is not None)
    new_low = flipped[0] if low is not None else None
    new_high = flipped[-1] if high is not None else None
    note = ("'other income and expense' evidence says 'expense of': the figure was read as a net expense "
            "(negative), whatever sign the model gave it")
    return new_low, new_high, note


# Words that make a figure a floor and not a point: "$1.30+", "at least $1.30", "$1.30 or more", "$1.30 or better".
# A "+" that is part of "+/-" or a signed change ("+2%") is not one.
_LOW_AFTER = re.compile(
    r"\s*(?:%|percent|per cent|billion|million|thousand|bps|basis points)?\s*(?:\+(?![/\d])|or\s+(?:more|better)\b)", re.IGNORECASE
)
_LOW_BEFORE = re.compile(r"\bat least\s+(?:approximately\s+|about\s+|roughly\s+)?[~$\u20ac\u00a3]?\s*$", re.IGNORECASE)


def has_low_marker(value: float, evidence: str) -> bool:
    """True when the evidence prints `value` as a floor: with a "+" or "or more" or "or better" after it, or "at
    least" before it."""
    for match in _NUMBER_TOKEN.finditer(evidence):
        try:
            printed = round(float(match.group().replace(",", "")), 6)
        except ValueError:
            continue
        if printed == round(value, 6) and (
            _LOW_AFTER.match(evidence, match.end()) or _LOW_BEFORE.search(evidence[max(0, match.start() - 40) : match.start()])
        ):
            return True
    return False


def with_one_sided_low(item: dict[str, Any], evidence: str) -> dict[str, Any]:
    """A low-only target, by code. "$1.30+" is at least 1.30, and a model that reads it as the point 1.30 to 1.30
    has turned a floor into a claim that the company will land exactly there. Only a point with nothing else in the
    item is changed: a range, and a plus or minus, are left as they are."""
    low = item["value_low"]
    if item["plus_minus_kind"] != "none" or low is None or item["value_high"] != low:
        return item
    return {**item, "value_high": None} if has_low_marker(low, evidence) else item


_HALF_YEAR = re.compile(
    r"\b(?:first|second|back|1st|2nd)[- ]half\b|\b[12]H\b|\bH[12]\b|\bsix[- ]months?\b|\bhalf[- ]year\b"
    r"|\b(?:first|last|final) two quarters\b|\bremainder of the year\b",
    re.IGNORECASE,
)
_FULL_YEAR = re.compile(r"\bfull[- ]year\b|\bfor the (?:full |fiscal )?year\b|\bfiscal year\b|\bannual\b|\byear ended\b", re.IGNORECASE)


def check_period(period: str, evidence: str) -> None:
    """target_period is a quarter ("Q3 FY2027") or a fiscal year ("FY2027"), and nothing else. A label that names
    a half is refused outright, and so is a fiscal year drawn from evidence that speaks of "the second half" and
    never of the full year: "the third quarter and second half of 2019" has a Q3 in it and no FY."""
    if _HALF_YEAR.search(period):
        raise ValueError(f"target_period {period!r} is a half-year; it must be a quarter or a fiscal year")
    if not PERIOD.match(period):
        raise ValueError(f"target_period {period!r} is not like FY2027 or Q3 FY2027")
    if not period.startswith("Q") and _HALF_YEAR.search(evidence) and not _FULL_YEAR.search(evidence):
        raise ValueError(f"target_period {period!r} is a full year, but the evidence is about a half-year")


# Verbs that report what already happened. Forward language is `expect`, `anticipate`, `forecast`, `will`, `intend`, `target`;
# "guidance" and "outlook" are not in it, because a result is often set against them ("above our guidance").
_PAST = re.compile(
    r"\b(?:was|were|had|did|used|delivered|reported|returned|grew|increased|decreased|declined|rose|fell|totaled|totalled|"
    r"generated|achieved|completed|recorded|posted|reached|earned|gained|lost|exceeded|repurchased|paid)\b",
    re.IGNORECASE,
)
_FORWARD = re.compile(r"\b(?:expect(?:s|ed|ing)?|anticipat(?:e|es|ed|ing)|forecast(?:s|ed|ing)?|will|intends?|targets?)\b", re.IGNORECASE)


def check_not_past_tense(evidence: str) -> None:
    """Evidence that reports what happened, with nothing that looks ahead, is a result and not a guidance. A
    forward verb anywhere in the evidence lets it through: "Target expects ... in line with the 3.4 percent the
    company delivered" is guidance that mentions a past figure."""
    past = _PAST.search(evidence)
    if past and not _FORWARD.search(evidence):
        raise ValueError(f"the evidence is in the past tense ({past.group()!r}) and says nothing forward-looking")


_NON_GAAP_WORD = re.compile(r"non[- ]?gaap|adjusted", re.IGNORECASE)
_GAAP_WORD = re.compile(r"(?<!non-)(?<!non )\bgaap\b", re.IGNORECASE)


def named_bases(text: str) -> set[str]:
    """Which of "GAAP" and "non-GAAP" the text names. "Adjusted" counts as non-GAAP."""
    return {b for b, pattern in (("GAAP", _GAAP_WORD), ("non-GAAP", _NON_GAAP_WORD)) if pattern.search(text)}


_COLUMN_WORD = re.compile(r"non[- ]?gaap|gaap|adjustments?", re.IGNORECASE)
_VALUE_AMOUNT = r"\(?[~$]?\s?\d[\d,]*(?:\.\d+)?\)?\s?(?:%|billion|million|thousand)?"
# One cell of a value row: a bare dash, or an amount with an optional range end, an optional plus or minus, and the footnote
# letters a reconciliation table prints after an adjustment ("$66 million B", "$0.14 A, B, C, D").
_VALUE_CELL_IN_ROW = re.compile(
    r"[\u2014\u2013](?![\w$])|"
    + _VALUE_AMOUNT + r"(?:\s*(?:[-\u2013]|to)\s*" + _VALUE_AMOUNT + r")?"
    r"(?:\s*(?:\u00b1|\+/-|plus or minus)\s*" + _VALUE_AMOUNT + r")?(?:\s+[A-D](?:\s*,\s*[A-D])*(?![\w]))?",
    re.IGNORECASE,
)


def basis_columns(candidate: dict[str, Any]) -> list[str] | None:
    """The basis of each column of a section's table, left to right ("GAAP", "Adjustments", "non-GAAP"), when its header
    names both GAAP and non-GAAP; otherwise None. A header is the section heading, or, where the cells were broken onto
    separate lines (Micron: "GAAP (1) Outlook" above "Non-GAAP (2) Outlook"), the short lines just before the heading and
    the heading. A heading that names both on its own ("GAAP Outlook Adjustments Non-GAAP Outlook") is the whole header."""
    if not is_block(candidate):
        return None

    def words(text: str) -> list[str]:
        return ["non-GAAP" if w.lower().startswith("non") else "Adjustments" if w.lower().startswith("adj") else "GAAP"
                for w in _COLUMN_WORD.findall(text)]

    heading = words(candidate.get("heading") or "")
    if "GAAP" in heading and "non-GAAP" in heading:
        return heading
    before = [t for t in (candidate.get("context_before") or [])[-2:] if len(t.split()) <= 6]
    found = words(" ".join(before + [candidate.get("heading") or ""]))
    return found if "GAAP" in found and "non-GAAP" in found else None


def value_cells(line: str) -> list[str]:
    """The cells of one value row, left to right: "$891 million \u00b1 $25 million $66 million B $825 million \u00b1 $25 million" is three."""
    return [m.group().strip() for m in _VALUE_CELL_IN_ROW.finditer(line) if m.group().strip() and not re.fullmatch(r"\(\d\)", m.group().strip())]


def locate_column(columns: list[str], item: dict[str, Any], evidence: str) -> int | None:
    """The 0-based column of the table the item's figure sits in, or None: the figure is found among the cells of the
    evidence row that has exactly as many cells as the header has columns, and it has to be in exactly one of them."""
    if item.get("value_low") is None:
        return None
    wanted = round(abs(item["value_low"]), 6)
    for line in reversed(evidence.split("\n")):
        cells = value_cells(line)
        hits = [i for i, c in enumerate(cells) if wanted in numbers_in(c)]
        if hits and len(cells) == len(columns):
            return hits[0] if len(hits) == 1 else None
    return None


def value_column_text(candidate: dict[str, Any], item: dict[str, Any], evidence: str) -> str | None:
    """Where the figure sits, for the reviewer and for 03b_verify, which sees only the excerpt and the heading and
    would otherwise read Micron's GAAP column under the heading "Non-GAAP (2) Outlook" as the wrong basis:
    "GAAP | non-GAAP: the figure is in column 1, GAAP". None when the section has no basis header or the column
    cannot be told."""
    columns = basis_columns(candidate)
    hit = locate_column(columns, item, evidence) if columns else None
    return None if hit is None else f"{' | '.join(columns)}: the figure is in column {hit + 1}, {columns[hit]}"


def with_column_basis(metric: str, candidate: dict[str, Any], item: dict[str, Any], evidence: str, metrics: list[str]) -> tuple[str, str | None, bool]:
    """GAAP or non-GAAP from the table's own column header, by code, overriding the section heading and the model.
    Micron prints "GAAP | non-GAAP" (and, in its reconciliation, "GAAP | Adjustments | non-GAAP") side by side, so one
    row holds one figure per basis and the heading, which names whichever column it sits over, says nothing about the
    others. The item's figure is found among the cells of its row; when the row has exactly as many cells as the header
    has columns, that column's basis is the metric's. A figure in an Adjustments column is not guidance and is refused.
    Only a metric with both a GAAP and a non-GAAP entry is touched, and only when the figure sits in one column (a figure
    printed in two columns keeps the basis the model gave it).

    Returns (metric, note, applied): `applied` says the header decided, so the heading rule must not run after it."""
    columns = basis_columns(candidate)
    suffix = next((x for x in (" GAAP", " non-GAAP") if metric.endswith(x)), None)
    if columns is None or suffix is None:
        return metric, None, False
    base = metric[: -len(suffix)]
    if f"{base} GAAP" not in metrics or f"{base} non-GAAP" not in metrics or item.get("value_low") is None:
        return metric, None, False
    hit = locate_column(columns, item, evidence)
    if hit is None:
        return metric, None, False
    basis = columns[hit]
    if basis == "Adjustments":
        raise ValueError("the figure is in the table's Adjustments column, which is a reconciling item and not guidance")
    new = f"{base} {basis}"
    note = (f"basis taken from the table's column header ({' | '.join(columns)}): the figure is in column {hit + 1}, {basis}"
            + (f", not {metric.rsplit(' ', 1)[1]} as the model said" if new != metric else ""))
    return new, note if new != metric else None, True


def with_heading_basis(metric: str, candidate: dict[str, Any], evidence: str, metrics: list[str]) -> str:
    """GAAP or non-GAAP from the section heading, by code. "Adjusted diluted earnings per share guidance" over bare
    value lines means non-GAAP whatever the model said. It applies to a metric that has both a GAAP and a non-GAAP
    entry, and only when the heading names exactly one basis and the evidence lines do not name the other: a line
    that says "Non-GAAP operating margin" is more specific than the heading above it."""
    if not is_block(candidate):
        return metric
    heading_bases = named_bases(candidate.get("heading") or "")
    if len(heading_bases) != 1 or named_bases(evidence) - heading_bases:
        return metric
    (basis,) = heading_bases
    for suffix in (" GAAP", " non-GAAP"):
        if metric.endswith(suffix):
            base = metric[: -len(suffix)]
            if f"{base} GAAP" in metrics and f"{base} non-GAAP" in metrics:
                return f"{base} {basis}"
    return metric


def trim_to_figure_sentences(text: str, item: dict[str, Any]) -> str:
    """`text` cut down to the sentence(s) that print the item's own figure, for a line that runs over the excerpt
    limit: an outlook paragraph carries its revenue and its margin guidance together, and a company's boilerplate
    ("outlook statements are based on current expectations") can sit in the same line. The span from the first
    sentence with the figure to the last is kept if it fits; if not, only the first such sentence. Verbatim: the
    result is a slice of `text`. Unchanged when no sentence prints the figure (the guard that follows rejects it)."""
    figures = {round(abs(v), 6) for key in ("value_low", "value_high", "plus_minus") if isinstance(v := item.get(key), (int, float))}
    if (centre := _stated_centre(item)) is not None:
        figures.add(round(abs(centre), 6))  # the model may have returned the endpoints of a "$1.52 billion, plus or minus $50 million"
    hits = [(start, end) for start, end in sentence_spans(text) if numbers_in(text[start:end]) & figures]
    if not hits:
        return text
    span = text[hits[0][0] : hits[-1][1]]
    return span if len(span) <= EXCERPT_LIMIT else text[hits[0][0] : hits[0][1]]


def evidence_text(candidate: dict[str, Any], item: dict[str, Any]) -> str:
    """The verbatim text the item rests on: the sentence, or the lines of the section the model pointed at."""
    if not is_block(candidate):
        return candidate["sentence"]
    lines = candidate["sentence"].split("\n")
    first, last = item["line_first"], item["line_last"]
    if not (isinstance(first, int) and isinstance(last, int) and 1 <= first <= last <= len(lines)):
        raise ValueError(f"line_first {first!r} and line_last {last!r} are not lines 1 to {len(lines)} of the section")
    text = "\n".join(lines[first - 1 : last])
    if len(text) > EXCERPT_LIMIT:
        text = lines[last - 1]  # the value line alone
    if len(text) > EXCERPT_LIMIT:
        text = trim_to_figure_sentences(text, item)
    if len(text) > EXCERPT_LIMIT:
        raise ValueError(f"the evidence is {len(text)} chars, over the {EXCERPT_LIMIT} char excerpt limit")
    return text


# Words that describe what a number is measured in, not what it IS: stripping them out of an excerpt should
# leave nothing behind for a real label. "$915 million" reduces to nothing; "Operating expenses $915 million"
# still has a label once "million" is gone.
_BARE_UNIT_WORDS = re.compile(
    r"\b(?:usd|million|billion|thousand|percent|per\s*cent|bps|basis\s*points?|per\s+share|units?)\b",
    re.IGNORECASE,
)
_ANY_LETTER = re.compile(r"[A-Za-z]")
# Words a section heading uses to scope a period or say "this is guidance", not to name a metric: "Updated Q4
# Fiscal 2019 Guidance" is entirely these, and names nothing. "GAAP diluted earnings per share guidance" is not:
# "diluted earnings per share" survives.
_GENERIC_HEADING_WORDS = re.compile(
    r"\b(?:guidance|outlook|financial|updated|previous|prior|current|full[- ]?year|annual|fiscal|quarter(?:ly)?|"
    r"q[1-4]|for|the|of|and)\b|\b(?:first|second|third|fourth)\b|\b20\d\d\b|\bfy\s?\d{2,4}\b",
    re.IGNORECASE,
)


def heading_names_a_metric(heading: str | None) -> bool:
    """True when the section heading, its generic scaffolding stripped out, still says what the numbers under
    it are: "GAAP diluted earnings per share guidance" does, "Full Year FY31 Guidance" does not."""
    return bool(heading) and bool(_ANY_LETTER.search(_GENERIC_HEADING_WORDS.sub("", heading)))


def is_bare_value(excerpt: str, heading: str | None = None) -> bool:
    """True when `excerpt`, its unit words stripped out, has no letters left, AND the section heading (if any)
    does not name the metric on its own. "62.3%, plus or minus 50 bps" is not bare: "plus or minus" is still
    there once "bps" is gone. "$1.95 - $2.35 $8.60 - $9.60" under the heading "GAAP diluted earnings per share
    guidance" is not bare either: the heading alone already says what the numbers are."""
    if _ANY_LETTER.search(_BARE_UNIT_WORDS.sub("", excerpt)):
        return False
    return not heading_names_a_metric(heading)


def check_not_bare_value(excerpt: str, heading: str | None = None) -> None:
    """Section evidence is the label line plus the value line, never a bare value line: a pilot review found
    section items whose model-pointed line_first/line_last landed on the figure alone, in a table where no
    label line was ever a safe guess (a previous/updated, GAAP/non-GAAP table with several candidate label
    lines above it, any of which could be wrongly paired). A heading that already names the metric on its own
    ("GAAP diluted earnings per share guidance" over a bare value row) is not this problem and is not rejected;
    a heading that only scopes a period ("Updated Q4 Fiscal 2019 Guidance") is no label at all. Applies to
    every draft, not just section ones, though a sentence capture is never bare in practice: it is always the
    whole flagged sentence."""
    if is_bare_value(excerpt, heading):
        raise ValueError("the excerpt is a bare value: a number and a unit, with nothing saying what it is, and the heading does not name the metric either")


def evidence_for(company: Company, candidate: dict[str, Any], excerpt: str) -> Evidence:
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
        excerpt=excerpt,
    )


def _summary(d: dict[str, Any]) -> dict[str, Any]:
    a = d["assumption"]
    return {"draft_id": d["draft_id"], "capture_method": d["capture_method"], "target_low": a["target_low"],
            "target_high": a["target_high"], "unit": a["unit"], "text": a["text"][:120]}


def _numbers(d: dict[str, Any]) -> tuple[Any, Any, Any]:
    a = d["assumption"]
    return a["target_low"], a["target_high"], a["unit"]


def _is_point(d: dict[str, Any]) -> bool:
    low, high, _ = _numbers(d)
    return low is not None and low == high


def _bounds(d: dict[str, Any]) -> str:
    low, high, _ = _numbers(d)
    return f"at least {low:g}" if high is None else f"at most {high:g}" if low is None else f"{low:g} to {high:g}"


def _holds(interval: dict[str, Any], point: dict[str, Any]) -> bool:
    """The point, in the same unit, lies within the interval: two figures, or one figure and a floor or a ceiling."""
    (low, high, unit), (value, _, point_unit) = _numbers(interval), _numbers(point)
    return unit == point_unit and (low is None or value >= low) and (high is None or value <= high)


def dedupe_drafts(drafts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """One draft per (cik, metric, target_period, stated_at), except for a conflict.

    A point that lies inside a range on the same key is the same statement made less precisely (a table's "62.4%"
    beside the outlook's "62.4%, plus or minus 50 basis points"), so the range is kept over the point, whichever
    way each was captured. A point that lies OUTSIDE the range disagrees with it, and is left to the rules below.

    A sentence-captured draft wins over section ones, and between sentence drafts the earliest wins. Section drafts
    are different: two of them with the same numbers are one draft seen twice (the earliest is kept), but with
    different numbers they disagree, and nothing here can tell which is right. Both are kept, each marked
    `conflict: true`, for a person to settle in the review queue. Every drop is returned, with both sets of
    numbers, so nothing disappears without a trace. The drafts passed in are not changed."""
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for d in drafts:
        a = d["assumption"]
        groups.setdefault((d["cik"], a["metric"], a["target_period"], a["stated_at"]), []).append(d)
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for group in groups.values():
        ranked = sorted(group, key=lambda d: (d["capture_method"] != "sentence", d["char_start"], d["item_index"]))
        held_by: dict[str, dict[str, Any]] = {}  # draft_id of a point -> the range that holds it
        for d in ranked:
            if _is_point(d):
                holder = next((r for r in ranked if not _is_point(r) and _holds(r, d)), None)
                if holder is not None:
                    held_by[d["draft_id"]] = holder
        for point_id, holder in held_by.items():
            point = next(d for d in ranked if d["draft_id"] == point_id)
            dropped.append({
                "custom_id": point["custom_id"],
                "reason": (f"range kept over point: draft {holder['draft_id']} states {_bounds(holder)} for the same cik, metric, "
                           f"target period and stated date, and this point ({_numbers(point)[0]:g}) lies inside it"),
                "kept": _summary(holder),
                "dropped": _summary(point),
            })
        rest = [d for d in ranked if d["draft_id"] not in held_by]
        if rest[0]["capture_method"] == "sentence":
            winner_for = {i: rest[0] for i in range(len(rest))}
            winners = [rest[0]]
        else:  # every draft left is section-captured: one winner per distinct set of numbers
            by_numbers: dict[Any, dict[str, Any]] = {}
            for d in rest:
                by_numbers.setdefault(_numbers(d), d)
            conflict = len(by_numbers) > 1
            winners = [{**d, "conflict": True} if conflict else d for d in by_numbers.values()]
            flagged = {_numbers(d): w for d, w in zip(by_numbers.values(), winners)}
            winner_for = {i: flagged[_numbers(d)] for i, d in enumerate(rest)}
        kept.extend(winners)
        for i, d in enumerate(rest):
            keeper = winner_for[i]
            if keeper["draft_id"] == d["draft_id"]:
                continue
            differ = _numbers(d) != _numbers(keeper)
            dropped.append({
                "custom_id": d["custom_id"],
                "reason": (f"duplicate of draft {keeper['draft_id']}: same cik, metric, target period and stated date; "
                           f"kept the {keeper['capture_method']}-captured draft, dropped the {d['capture_method']}-captured one"
                           + ("; the numbers differ" if differ else "")),
                "kept": _summary(keeper),
                "dropped": _summary(d),
            })
    kept.sort(key=lambda d: (d["assumption"]["stated_at"], d["assumption"]["evidence"]["accession_number"], d["char_start"], d["item_index"]))
    return kept, dropped


def derive_drafts(
    index: dict[str, tuple[Company, dict[str, Any]]],
    results: dict[str, llm.Result],
    metrics: list[str] | None = None,
    kinds: dict[str, str] | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """(drafts, rejects, dropped duplicates) per cik, from archived results. Pure: no model call."""
    if metrics is None or kinds is None:
        config = load_config()
        metrics, kinds = metrics or config["metrics"], kinds or config["metric_kinds"]
    drafts: dict[str, list[dict[str, Any]]] = {}
    rejects: dict[str, list[dict[str, Any]]] = {}
    dupes: dict[str, list[dict[str, Any]]] = {}

    for cid in sorted(index, key=lambda c: (index[c][0].cik, index[c][1]["filed_at"], index[c][1]["accession"], index[c][1]["char_start"])):
        company, cand = index[cid]
        res = results.get(cid)
        if res is None or not res.ok:
            continue
        drafts.setdefault(company.cik, [])
        rejects.setdefault(company.cik, [])
        payload = llm.parse_json_text(res.text)
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            rejects[company.cik].append({"custom_id": cid, "reason": "output did not parse as {items: [...]}"})
            continue
        for n, item in enumerate(payload["items"]):
            try:
                metric = str(item["metric"])
                period = str(item["target_period"]).strip()
                if metric not in metrics:
                    raise ValueError(f"metric {metric!r} is not in the metrics list")
                excerpt = evidence_text(cand, item)
                check_not_bare_value(excerpt, cand.get("heading"))
                period, period_note = fix_na_column_period(period, cand, excerpt)
                check_period(period, excerpt)
                value_column = value_column_text(cand, item, excerpt)
                before_note = period_closes_before_stated(company, period, date.fromisoformat(cand["filed_at"]))
                if metric == "tax rate":
                    candidates_step.check_tax_footnote_periods(excerpt)
                metric, basis_note, from_columns = with_column_basis(metric, cand, item, excerpt, metrics)
                if not from_columns:
                    metric = with_heading_basis(metric, cand, excerpt, metrics)
                check_unit_kind(metric, item["unit"], kinds)
                item, printed_note = with_printed_plus_minus(metric, item, excerpt, kinds)
                if "_printed" not in item:  # "$1.52 billion, plus or minus $50 million" is a level whatever else the sentence says
                    check_not_a_change(metric, (item.get("value_low"), item.get("value_high")), excerpt, kinds)
                check_not_relative_to_prior_period(metric, (item.get("value_low"), item.get("value_high")), excerpt, kinds)
                stated = with_one_sided_low(item, excerpt)
                stated, spread_note = with_currency_spread_absolute(metric, stated, excerpt, kinds)
                stated = with_absolute_percent_spread(metric, stated, kinds)
                low, high = resolve_range(stated)
                low, high, parens_note = fix_parens_sign(
                    metric, low, high, excerpt, kinds, tax_rate_parens_negative=company.tax_rate_parens_negative)
                low, high, expense_note = fix_expense_of_sign(metric, low, high, excerpt)
                parens_note = "; ".join(n for n in (before_note, period_note, basis_note, printed_note, spread_note, parens_note, expense_note) if n) or None
                check_range_language(stated, excerpt)
                check_numbers_in_evidence(stated, excerpt)
                check_not_past_tense(excerpt)
                assumption = Assumption(
                    text=excerpt, metric=metric, target_low=low, target_high=high,
                    unit=item["unit"], target_period=period,
                    stated_at=date.fromisoformat(cand["filed_at"]),
                    evidence=evidence_for(company, cand, excerpt),
                )
            except (KeyError, TypeError, ValueError, ValidationError) as exc:
                rejects[company.cik].append({"custom_id": cid, "item": item, "reason": str(exc)[:300]})
                continue
            record = {
                "draft_id": stable_ulid(f"{cid}|{n}|{metric}|{period}", assumption.stated_at),
                "cik": company.cik, "ticker": company.ticker, "company": company.name,
                "custom_id": cid, "capture_method": cand["capture_method"], "char_start": cand["char_start"],
                "heading": cand.get("heading"), "lead_in": cand.get("lead_in"), "table_header": cand.get("table_header"),
                "value_column": value_column, "item_index": n, "conflict": False, "assumption": assumption.model_dump(mode="json"),
                "parens_note": parens_note,
            }
            if is_block(cand):
                record["evidence_lines"] = [item["line_first"], item["line_last"]]
            drafts[company.cik].append(record)

    for cik in list(drafts):
        drafts[cik], dupes[cik] = dedupe_drafts(drafts[cik])
    return drafts, rejects, dupes


def find_empty_blocks(
    index: dict[str, tuple[Company, dict[str, Any]]],
    results: dict[str, llm.Result],
    drafts: dict[str, list[dict[str, Any]]],
    rejects: dict[str, list[dict[str, Any]]],
    dupes: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """The 8-K outlook blocks the model answered that produced no draft, per cik, for the review queue to flag.

    A block is empty when its answer had no items, or every item was rejected by a guard: guidance may have been
    lost, and a person should look. A block whose drafts were all dropped as duplicates is not empty (what it said
    is in the queue under another draft), and neither is one that was never answered. Sentences are never listed."""
    represented = {d["custom_id"] for v in drafts.values() for d in v} | {e["custom_id"] for v in dupes.values() for e in v}
    why: dict[str, list[str]] = {}
    for v in rejects.values():
        for r in v:
            why.setdefault(r["custom_id"], []).append(r["reason"])
    found: dict[str, list[dict[str, Any]]] = {}
    for cid, (company, cand) in index.items():
        res = results.get(cid)
        if not is_block(cand) or res is None or not res.ok or cid in represented:
            continue
        payload = llm.parse_json_text(res.text)
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            reason = "the model's output did not parse as {items: [...]}"
        elif not items:
            reason = "the model returned no items"
        else:
            reason = f"{len(items)} item(s) returned, all rejected: " + "; ".join(sorted({r[:90] for r in why.get(cid, [])}))
        try:
            evidence = evidence_for(company, cand, cand["sentence"][:EXCERPT_LIMIT]).model_dump(mode="json")
        except (ValueError, ValidationError):
            evidence = None
        found.setdefault(company.cik, []).append({
            "block_id": stable_ulid(f"{cid}|empty_block", date.fromisoformat(cand["filed_at"])),
            "custom_id": cid, "cik": company.cik, "ticker": company.ticker, "company": company.name,
            "accession": cand["accession"], "filed_at": cand["filed_at"], "char_start": cand["char_start"],
            "heading": cand.get("heading"), "lead_in": cand.get("lead_in"), "table_header": cand.get("table_header"),
            "block_lines": cand.get("block_lines"), "reason": reason, "evidence": evidence,
        })
    for rows in found.values():
        rows.sort(key=lambda r: (r["filed_at"], r["accession"], r["char_start"]))
    return found


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows and not path.exists():  # never leave an empty file that looks like output
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def canary_check(text: str) -> None:
    payload = llm.parse_json_text(text)
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise RuntimeError(f"canary output is not {{items: [...]}}: {text[:200]!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Draft assumptions from candidates, with Haiku batches.")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--company", action="append", metavar="TICKER")
    parser.add_argument("--filing-type", action="append", metavar="FORM", help="default: 8-K only")
    parser.add_argument("--limit", type=int, help="send only the first N requests")
    parser.add_argument("--per-company", type=int, metavar="N",
                        help="send N requests per company, spread evenly across its filings, half of them sections")
    parser.add_argument("--submit", action="store_true", help="really call the API; without it this is a dry run")
    parser.add_argument("--from-archive", action="store_true",
                        help="re-derive and WRITE the drafts from the archived answers to these candidates, matched by custom_id and "
                             "whatever prompt they were answered under. No API call, no cost. For a change that lives in the code "
                             "after the model (a guard, a sign or range rule); a change to the prompt or the metric list "
                             "needs --submit, because the model has not seen it")
    parser.add_argument("--wait-minutes", type=float, default=60)
    args = parser.parse_args(argv)

    config = load_config(args.config)
    cfg = llm.LlmConfig.from_config(config["llm"])
    metrics = config["metrics"]
    targets = companies(config, args.company)
    filing_types = set(args.filing_type or ["8-K"])
    requests, index, skipped = build_requests(targets, filing_types, limit=args.limit, per_company=args.per_company, metrics=metrics)

    archive, ledger = llm.RawArchive(STEP), llm.Ledger()
    if args.from_archive:
        answered = {cid: res for cid, res in archive.load().items() if cid in index and res.ok}
        print(f"{STEP}: --from-archive: {len(answered):,} of {len(requests):,} requests have an archived answer; no API call is made")
        drafts, rejects, dupes = derive_drafts(index, answered, metrics, config["metric_kinds"])
        empties = find_empty_blocks(index, answered, drafts, rejects, dupes)
        for company in targets:
            write_jsonl(DRAFTS_DIR / f"{company.cik}.jsonl", drafts.get(company.cik, []))
            write_jsonl(DRAFTS_DIR / f"{company.cik}.rejects.jsonl", rejects.get(company.cik, []))
            write_jsonl(DRAFTS_DIR / f"{company.cik}.skipped.jsonl", skipped[company.cik] + dupes.get(company.cik, []))
            write_jsonl(DRAFTS_DIR / f"{company.cik}.empty_blocks.jsonl", empties.get(company.cik, []))
            print(f"  {company.ticker}: {len(drafts.get(company.cik, [])):,} drafts, {len(rejects.get(company.cik, []))} rejected, "
                  f"{len(dupes.get(company.cik, []))} duplicates dropped, {len(empties.get(company.cik, []))} outlook blocks with no draft")
        return 0
    have = llm.current_results(archive.load(), requests, cfg.model)
    todo = [r for r in requests if not (r.custom_id in have and have[r.custom_id].ok)]
    kinds = Counter("section" if r.custom_id.startswith("b-") else "sentence" for r in requests)
    print(f"{STEP}: {len(requests):,} requests for {', '.join(sorted(filing_types))} candidates "
          f"({kinds['sentence']} sentence, {kinds['section']} section; "
          f"{len(requests) - len(todo):,} already answered, {len(todo):,} to send); "
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
        # run_batch answers for the requests it was given, which on a re-run are only the ones still to do. The drafts
        # come from every request's answer, this run's and the earlier ones, or a resumed batch would overwrite them.
        results = llm.current_results(archive.load(), requests, cfg.model)
        for note in outcome.notes:
            print(f"note: {note}")
        if outcome.pending_batch:
            # Only what the canary and any earlier run answered is in hand. Deriving from that and writing it would
            # replace a complete set of drafts with a few, so nothing is written until the batch has ended.
            print(f"STOPPED: batch {outcome.pending_batch} has not ended, so nothing was written to data/drafts/. "
                  "Run the same command again to resume it; nothing will be resubmitted.", file=sys.stderr)
            return 5
    else:
        if not args.submit:
            print("dry run: nothing was submitted. Add --submit to run the batch.")
        results = have

    drafts, rejects, dupes = derive_drafts(index, results, metrics, config["metric_kinds"])
    empties = find_empty_blocks(index, results, drafts, rejects, dupes)
    for company in targets:
        if args.submit:  # a dry run never writes to data/drafts/: with stale answers it would empty what is there
            write_jsonl(DRAFTS_DIR / f"{company.cik}.jsonl", drafts.get(company.cik, []))
            write_jsonl(DRAFTS_DIR / f"{company.cik}.rejects.jsonl", rejects.get(company.cik, []))
            write_jsonl(DRAFTS_DIR / f"{company.cik}.skipped.jsonl", skipped[company.cik] + dupes.get(company.cik, []))
            write_jsonl(DRAFTS_DIR / f"{company.cik}.empty_blocks.jsonl", empties.get(company.cik, []))
        print(f"  {company.ticker}: {len(drafts.get(company.cik, [])):,} drafts, {len(rejects.get(company.cik, []))} rejected, "
              f"{len(skipped[company.cik])} skipped before sending, {len(dupes.get(company.cik, []))} duplicates dropped, "
              f"{len(empties.get(company.cik, []))} outlook blocks with no draft")
    if not args.submit:
        print("dry run: the counts above are from answers already archived for these exact requests; nothing was written to data/drafts/.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
