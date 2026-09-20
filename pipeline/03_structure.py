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
- Margins and rates are percentages of revenue: "gross margin", "operating margin" and "tax rate", unit percent. "Operating margin" is never "operating income". Amounts are dollars: "revenue", "operating income", "operating expenses", "other income and expense" and "free cash flow". Growth in one of them ("free cash flow growth 9% - 10%") is neither the amount nor the margin, so it returns nothing.
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


def document_title(text: str) -> str | None:
    """The release headline, if one is near the top: the first line that reads like one."""
    for line in text.split("\n")[:25]:
        if _TITLE_WORDS.search(line) and 4 <= len(line.split()) <= 30:
            return line
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
    for key in ("value_low", "value_high"):
        value = item[key]
        if value is not None and round(abs(value), 6) not in printed:
            raise ValueError(f"{key} {value:g} is not in the evidence text")


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
    r"\b(?:first|second|1st|2nd)[- ]half\b|\b[12]H\b|\bH[12]\b|\bsix[- ]months?\b|\bhalf[- ]year\b"
    r"|\b(?:first|last|final) two quarters\b",
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
        raise ValueError(f"the evidence is {len(text)} chars, over the {EXCERPT_LIMIT} char excerpt limit")
    return text


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
                check_period(period, excerpt)
                metric = with_heading_basis(metric, cand, excerpt, metrics)
                check_unit_kind(metric, item["unit"], kinds)
                stated = with_one_sided_low(item, excerpt)
                low, high = resolve_range(stated)
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
                "item_index": n, "conflict": False, "assumption": assumption.model_dump(mode="json"),
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
    parser.add_argument("--wait-minutes", type=float, default=60)
    args = parser.parse_args(argv)

    config = load_config(args.config)
    cfg = llm.LlmConfig.from_config(config["llm"])
    metrics = config["metrics"]
    targets = companies(config, args.company)
    filing_types = set(args.filing_type or ["8-K"])
    requests, index, skipped = build_requests(targets, filing_types, limit=args.limit, per_company=args.per_company, metrics=metrics)

    archive, ledger = llm.RawArchive(STEP), llm.Ledger()
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
