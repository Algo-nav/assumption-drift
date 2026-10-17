"""Phase 2, step 2: what the company later reported, and whether it ever admitted a miss.

    python -m pipeline.04_outcomes [--company TICKER ...] [--stage outcomes|ack|withdrawal|all]
                                   [--submit] [--wait-minutes M] [--only PATH]
                                   [--from-review] [--accessions PATH] [--sync] [--max-usd USD] [--force]

A dry run unless --submit is given. Three stages, each a Haiku batch behind the same
cost gate as 03_structure (one budget across all of them):

  outcomes    For each draft, find the company's later 8-K earnings releases whose
              headline names the draft's period, pick the lines that name the metric
              next to a number, and ask the model which line states the ACTUAL result.
  ack         For each draft whose outcome resolves to "missed" by the rubric, find later
              lines that mention the metric next to words like "below", "higher than" or "short of",
              and ask the model whether one admits the gap. The earliest wins.
  withdrawal  For each draft, find the 8-K exhibits filed after the guidance and up to the
              day its period closed, pick the sentences that say withdraw, suspend or no
              longer providing about guidance, and ask the model whether one is the company
              withdrawing guidance that covers the draft's metric and period. The earliest
              wins. It needs the company's fiscal calendar (config.yaml) to know when the
              period closed, and a draft it cannot date is not searched.

Outcomes and withdrawals are looked for in 8-K exhibits only. Acknowledgements are looked
for in every cached filing after the outcome, the 10-Q and the 10-K included.

Nothing is written to data/outcomes/ unless --submit is given and every stage that ran has
finished. A batch still processing when --wait-minutes runs out stops the run with exit 5
and writes nothing; run it again to resume. What is written comes from every request's
answer, this run's and earlier ones.

Nothing the model returns is trusted blindly. Values are converted between units by
code, a value wildly off the guidance is rejected as a unit or row mix-up, and every
outcome carries the verbatim line it came from. "No outcome" always has a reason.

The weekly refresh (a runner has no data/drafts/ and only the new filings in data/raw/) uses four more flags:

  --accessions PATH  the new_filings.json 01_fetch --since-manifest wrote: the document store holds only those filings, so
                     every search is a search of the NEW filings.
  --from-review      the drafts are rebuilt from the review CSVs (05_review.draft_stub, so a number a reviewer corrected is
                     the number searched for): for the outcome stage, rows with no outcome whose reason is "period not
                     closed", "no later 8-K release for that period found" or "outcome search not run"; for the
                     acknowledgement stage those rows plus missed rows with no acknowledgement whose `ack_days` window is
                     still open; for the withdrawal stage rows whose period has not closed. The drafts in data/drafts/ that
                     belong to the listed filings are added. Rows are merged into data/outcomes/ and a row keeps what it had
                     unless this run found it: an outcome, acknowledgement or withdrawal already there is never replaced
                     by "nothing found". The outcome search takes at most `max_docs` releases, earliest first, among the
                     filings it can see, so a row whose release was filed in an earlier week and was skipped is not found.
  --sync, --max-usd  as in 03_structure: one by one whatever `llm.sync_below` says; a ceiling on the run's spend (all three
                     stages together); each stage appends (date, stage, requests, usd) to data/spend_ledger.jsonl.
  --force            write an empty result over a non-empty data/outcomes/{cik}.jsonl. Without it that is refused (exit 6),
                     whatever the cause: a runner that has no data/drafts/ would otherwise empty the tracked files.

`as_of`, the date a period must have closed by to expect an outcome, is the run date (today). The manifest's
`processed_through` is for 01_fetch's scope only; 04 does not read it.

Reads   data/drafts/{cik}.jsonl, data/raw/{cik}/
Writes  data/outcomes/{cik}.jsonl          one row per draft: outcome, acknowledgement, withdrawal, reasons
        data/outcomes/{cik}.rejects.jsonl  model answers that failed a check, with the reason
        data/batches/04_*                  raw model output and pending batch state
"""

from __future__ import annotations

import argparse
import bisect
import importlib
import dataclasses
import functools
import json
import math
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

from pipeline import common, llm
from pipeline.common import (CONFIG_PATH, DRAFTS_DIR, OUTCOMES_DIR, REVIEW_DIR, Company, companies, load_accession_scope, load_config,
                             period_named_by_end_date, read_id_list)
from research_record import polarity, rubric
from research_record.schema import Evidence, Outcome
from research_record.text import html_to_text_and_gaps, sentence_spans

candidates_step = importlib.import_module("pipeline.02_candidates")
structure_step = importlib.import_module("pipeline.03_structure")
review_step = importlib.import_module("pipeline.05_review")

STEP_OUTCOMES = "04_outcomes"
STEP_ACK = "04_acknowledgements"
STEP_WITHDRAWAL = "04_withdrawals"
MAX_TOKENS = 200
EXPECTED_OUTPUT_TOKENS = 30
EXCERPT_LIMIT = 400
UNITS = structure_step.UNITS

_PERIOD = re.compile(r"^(?:Q([1-4]) )?FY(\d{4})$")
_QUARTER_WORDS = {1: "first|1st", 2: "second|2nd", 3: "third|3rd", 4: "fourth|4th"}
_NOT_RESULTS = re.compile(r"conference call|webcast|to (?:announce|report|host)|will (?:announce|report|host)|invites", re.IGNORECASE)
_GUIDANCE_WORDS = re.compile(r"guidance|outlook|expect|target|initiat|raises?\b|forecast|anticipat", re.IGNORECASE)
_UNITS_NOTE = re.compile(r"\bin (?:millions|thousands|billions)\b", re.IGNORECASE)
_STOP_WORDS = {"gaap", "non", "adjusted", "diluted", "full", "year", "growth", "expected", "total", "rate"}

# What one unit is worth in the base of its family. Different families never convert.
_USD = {"USD": 1.0, "USD thousands": 1e3, "USD millions": 1e6, "USD billions": 1e9}
_PERCENT = {"percent": 1.0, "percentage points": 1.0, "basis points": 0.01}


def _outcome_schema() -> dict[str, Any]:
    number_or_null = {"anyOf": [{"type": "number"}, {"type": "null"}]}
    return {
        "type": "object",
        "properties": {
            "index": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
            "value": number_or_null,
            "unit": {"type": "string", "enum": UNITS + ["none"]},
        },
        "required": ["index", "value", "unit"],
        "additionalProperties": False,
    }


OUTCOME_SCHEMA = _outcome_schema()
ACK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"indices": {"type": "array", "items": {"type": "integer"}}},
    "required": ["indices"],
    "additionalProperties": False,
}

OUTCOME_SYSTEM = """\
You find what a company actually reported for a metric and period that it had earlier given guidance on.

You are given the guidance (metric, unit, period, when it was stated) and a numbered list of lines from the company's LATER earnings releases, oldest first. Each line comes with the lines just before it, the heading or table label above it, and a units note found above it if there is one.

Pick the first line that states the ACTUAL result for exactly that metric and exactly that period, and give its value as printed, with the unit it is printed in. If no line does, return index null, value null and unit "none".

Rules
- Actual results only. A line that repeats guidance, or gives an outlook for a later period, is not a result.
- The metric must match. GAAP and non-GAAP are different metrics. Total revenue is not one segment's revenue. Gross margin is not gross profit in dollars. Use the section label and the lines before to tell which row you are looking at.
- The period must match. A quarter's figure is not the full year's. Table rows list the current period first and prior periods after it; take the current period's figure, for the period asked about.
- Units: a line that prints "$96,221" under a note that says "in millions" is 96221 in USD millions. A line that prints "$68.1 billion" is 68.1 in USD billions. Do not convert anything.
- If two lines report the same value, pick the earlier.
"""

ACK_SYSTEM = """\
You decide whether a company admitted, in its own words in a later filing, that its actual result for a metric and period came in on a stated side of its own guidance: worse than guided or better than guided.

You are given the guidance (metric, period, the range the company guided to, what it actually reported, which side of that range the reported value fell on, and whether that side is worse or better for that metric), and a numbered list of lines from the company's later filings, oldest first.

Return the numbers of the lines that say, in the company's own words, that its actual result for exactly that metric and exactly that period fell on the stated side of its guidance or expectations. The company may say it in words about the side (below, above the high end of, lower than, higher than, exceeded) or about the outcome (short of, did not meet, better than, or otherwise missed or beat it). Return an empty list if none do.

Rules
- The line must be about the same metric and the same period. A gap on another metric or another period does not count.
- It must reference the result against guidance or expectations. A line that only states the result, or only gives a new outlook, does not count. A line that says guidance was revised because the result came in on the stated side does.
- Do not count a line that describes the other side of the range: a line saying results were on the other side of guidance, or in line with it, does not count.
"""

WITHDRAWAL_SYSTEM = """\
You decide whether a company withdrew or suspended its own guidance.

You are given the guidance (metric, period, the range the company guided to, when it was stated, and the day the period ends) and a numbered list of lines from the company's later 8-K exhibits, all filed before the period ended, oldest first.

Return the numbers of the lines that say, in the company's own words, that it is withdrawing, suspending or no longer providing guidance that covers that metric and that period: its guidance as a whole, its guidance for that period, or its guidance for that metric. Return an empty list if none do.

Rules
- It must be the company's own financial guidance or outlook. Suspending a dividend, a share repurchase program, an operation or a service does not count, nor does withdrawing an offer or a filing.
- It must cover this metric and this period. Withdrawing guidance for another metric, or only for another period, does not count.
- Raising, lowering, updating or reaffirming guidance is not withdrawing it.
- A line that only says the company may withdraw or update guidance in future, or that describes a risk, does not count.
"""


# --- periods and metrics ---------------------------------------------------


def parse_period(period: str) -> tuple[int | None, int] | None:
    m = _PERIOD.match(period)
    return (int(m.group(1)) if m.group(1) else None, int(m.group(2))) if m else None


def _quarter_regex(quarter: int) -> re.Pattern[str]:
    return re.compile(rf"\b(?:(?:{_QUARTER_WORDS[quarter]})[- ]quarter|Q{quarter}|{quarter}Q)\b", re.IGNORECASE)


def _year_regex(year: int) -> re.Pattern[str]:
    yy = year % 100
    return re.compile(rf"\b(?:fiscal(?: year)? ?(?:{year}|{yy:02d})|FY ?(?:{year}|{yy:02d})|{year})\b", re.IGNORECASE)


def period_signal(text: str, quarter: int | None, year: int) -> int:
    """0 if a line does not name the period, 2 if it names the quarter (or the fiscal year), 3 if it names the year too.

    Used only to rank lines. A results release rarely repeats the year on every line, so the quarter
    word alone counts for most of it.
    """
    if quarter is None:
        word = re.search(r"full[- ]year|fiscal year|for the year|\bFY ?\d{2,4}\b|fiscal 20\d\d", text, re.IGNORECASE)
    else:
        word = _quarter_regex(quarter).search(text)
    if not word:
        return 0
    return 3 if _year_regex(year).search(text) else 2


# Any year a sentence names: a four-digit year, or "fiscal 24" / "FY24" / "fiscal year 2024".
_ANY_YEAR = re.compile(r"\b20\d\d\b|\b(?:fiscal(?: year)? ?|FY ?)\d{2}\b", re.IGNORECASE)


def names_period(sentence: str, period: tuple[int | None, int]) -> bool:
    """Does this sentence name the row's own period, by the same quarter and year tests the outcome matcher ranks
    lines with (`period_signal`)? It must name the quarter (or the fiscal year), and a year too if it names any:
    "fourth quarter revenue ... below our guidance" with no year at all is read as this period's, while one that
    names fiscal 2023 under a row for FY2024 is about another period and does not count."""
    signal = period_signal(sentence, *period)
    return signal == 3 or (signal == 2 and not _ANY_YEAR.search(sentence))


def title_names_period(title: str, head: str, quarter: int | None, year: int, derived_year: int | None = None) -> bool:
    """Is this headline a results release for the period?

    The quarter must be in the headline; fiscal-year results ride with the fourth quarter.
    A headline that names a year must name this one. One that names none (Target's interim
    releases say only "Reports Second Quarter Earnings", Lowe's "Reports Fourth Quarter Sales and
    Earnings Results") reports the fiscal year the company's own calendar puts that quarter in on
    the filing date (`derived_year`, from `Company.fiscal_year_reported`); where there is no
    calendar to work it out from, it is checked against the start of the release. The period is never taken from the body alone: Salesforce opens with guidance
    bullets, so a Q3 release names "fourth quarter FY26" without reporting it.
    """
    if _NOT_RESULTS.search(title):
        return False
    named = {int(y) for y in re.findall(r"\b(20\d\d)\b", _OUTLOOK_YEAR.sub("", title))} | {2000 + int(y) for y in re.findall(r"\bFY ?(\d\d)\b", title, re.IGNORECASE)}
    if quarter in (None, 4) and not _NAMES_A_QUARTER.search(title):
        # "UnitedHealth Group Reports 2024 Results": a fiscal year's results carry the fourth quarter's, and the headline
        # may name the year and no quarter. It must name this year and no other (a "2026 Outlook" is not a result).
        return named == {year} and bool(_REPORTS_RESULTS.search(title))
    if not _quarter_regex(4 if quarter is None else quarter).search(title):
        return False
    if named:
        return year in named
    return year == derived_year if derived_year is not None else bool(_year_regex(year).search(head))


def release_names_period(doc: "DocText", period: tuple[int | None, int], head_chars: int, company: Company | None = None) -> bool:
    """Does this release's headline (wrapped over up to three lines) name the period? `company` supplies the fiscal
    calendar for a headline that names a quarter and no year."""
    derived = company.fiscal_year_reported(period[0], date.fromisoformat(doc.meta["filed_at"])) if company else None
    titles = [structure_step.document_title(doc.text, wrap=True), top_headline(doc.text)]
    return any(title_names_period(t, doc.text[:head_chars], *period, derived_year=derived) for t in dict.fromkeys(titles) if t)


_OUTLOOK_YEAR = re.compile(r"\b20\d\d\s+(?:performance\s+)?(?:outlook|guidance|expectations|targets?)\b", re.IGNORECASE)
_REPORTS_RESULTS = re.compile(r"\breports?\b.*\b(?:results|performance|earnings)\b", re.IGNORECASE)
# A first line that reads as a results headline, and not as a deck, a stress test or a contact block.
_TOP_RESULTS = re.compile(r"\b(?:reports?|results|net sales|comp sales|revenue|EPS|earnings)\b", re.IGNORECASE)
_TOP_NOT_RESULTS = re.compile(r"presentation|slides?\b|investor update|stress test|prepared remarks|supplement|contact", re.IGNORECASE)
_NAMES_A_QUARTER = structure_step._NAMES_A_QUARTER
_TOP_SKIP = re.compile(
    r"^(?:EX-99(?:\.\d+)?|\d+|(?:PRESS|EARNINGS|NEWS) RELEASE|FOR IMMEDIATE RELEASE|\S+\.html?"
    r"|(?:[A-Z][a-z]+\.? \d{1,2}, 20\d\d)|(?:\(?[A-Z][a-z]+\.? \d{1,2}, 20\d\d\)?))$", re.IGNORECASE)
_PAGE_PREFIX = re.compile(r"^(?:Page \d+ of \d+|\d+)\s+(?=[A-Za-z])", re.IGNORECASE)


def top_headline(text: str) -> str | None:
    """The first real line of the release, for a release whose headline `document_title` cannot find: it has none of the
    words that function looks for (Walmart's "Walmart Inc. net sales exceed $150 billion in Q4"), is shorter than four
    words ("Walmart Reports", continued on the next line), or shares its line with the bullets under it (UnitedHealth's
    2026 "Page 1 of 21 UnitedHealth Group Reports 2025 Results ... \u2022 ..."). Exhibit labels, file names and page numbers
    are skipped, the line is cut at its first bullet, and it is continued over up to two more lines until it names a
    quarter. Only this first line is read, never the bullets under it, so a third-quarter release that mentions the
    fourth quarter's outlook further down is not taken for the fourth quarter's."""
    lines = text.split("\n")
    for i, raw in enumerate(lines[:12]):
        line = _PAGE_PREFIX.sub("", raw.strip()).split("\u2022")[0].strip()
        if not line or _TOP_SKIP.match(line):
            continue
        headline = line
        for extra in lines[i + 1 : i + structure_step.HEADLINE_LINES]:
            extra = extra.strip()
            if _NAMES_A_QUARTER.search(headline) or not extra or extra.startswith("\u2022"):
                break
            headline = f"{headline} {extra.split(chr(0x2022))[0].strip()}"
        headline = headline[: structure_step.HEADLINE_CHARS]
        return headline if _TOP_RESULTS.search(headline) and not _TOP_NOT_RESULTS.search(headline) else None
    return None


def metric_pattern(metric: str, groups: list[dict[str, str]]) -> re.Pattern[str] | None:
    """Words a line must contain to be about this metric, or None if the metric is not recognised."""
    parts = [g["match"] for g in groups if re.search(g["when"], metric, re.IGNORECASE)]
    if not parts:
        words = [w for w in re.findall(r"[a-z]{4,}", metric.lower()) if w not in _STOP_WORDS]
        parts = [re.escape(w) for w in words]
    return re.compile("|".join(f"(?:{p})" for p in parts), re.IGNORECASE) if parts else None


def convert(value: float, from_unit: str, to_unit: str) -> float:
    """Between units of the same family. ValueError across families, or for 'none'."""
    if from_unit == to_unit:
        return value
    for family in (_USD, _PERCENT):
        if from_unit in family and to_unit in family:
            return round(value * family[from_unit] / family[to_unit], 6)
    raise ValueError(f"cannot convert {from_unit!r} to {to_unit!r}")


# --- documents -------------------------------------------------------------


@dataclass(frozen=True)
class DocText:
    meta: dict[str, Any]
    text: str
    lines: list[str]
    line_starts: list[int]
    spans: list[tuple[int, int]]
    outlook_lines: frozenset[int] = frozenset()  # lines inside a block under an Outlook, Guidance or Targets heading

    def line_of(self, char_start: int) -> int:
        return bisect.bisect_right(self.line_starts, char_start) - 1


@functools.lru_cache(maxsize=1)
def _outlook_settings() -> Any:
    settings = candidates_step.Settings.from_config(load_config()["candidates"])
    return dataclasses.replace(settings, heading=_OUTLOOK_HEADING)


class DocStore:
    """One company's cached filings, read and split once.

    `metas` is the 8-K exhibits, and is what the outcome and withdrawal searches read. `all_metas` is every intact
    cached filing, the 10-Q and the 10-K included, and is what the acknowledgement search reads. Both are oldest first."""

    def __init__(self, company: Company, accessions: set[str] | None = None) -> None:
        """`accessions`, when given, are the only filings this store can see (the weekly refresh's new filings)."""
        folder = common.RAW_DIR / company.cik
        metas = [common.read_meta(p) for p in folder.glob("*.meta.json")] if folder.exists() else []
        self.company = company
        self.folder = folder
        self.all_metas = sorted(
            (m for m in metas if m and m.get("http_status") == 200 and (folder / f"{m['accession']}.html").exists()
             and (accessions is None or m["accession"] in accessions)),
            key=lambda m: (m["filed_at"], m["accession"]),
        )
        self.metas = [m for m in self.all_metas if m["filing_type"] == "8-K"]
        self._docs: dict[str, DocText] = {}

    def get(self, meta: dict[str, Any]) -> DocText:
        acc = meta["accession"]
        if acc not in self._docs:
            text, gaps = html_to_text_and_gaps((self.folder / f"{acc}.html").read_bytes())
            lines = text.split("\n")
            starts, offset = [], 0
            for line in lines:
                starts.append(offset)
                offset += len(line) + 1
            # The same section capture 02_candidates uses, with the heading widened to the words an outcome must avoid.
            blocks = candidates_step.find_blocks(lines, gaps, _outlook_settings())
            outlook = frozenset(i for b in blocks for i in range(b.start, b.end))
            self._docs[acc] = DocText(meta, text, lines, starts, sentence_spans(text), outlook)
        return self._docs[acc]


@dataclass(frozen=True)
class Line:
    accession: str
    filed_at: str
    sentence: str
    char_start: int
    before: list[str]
    section: str | None
    units_note: str | None
    score: int
    pick: float | None = None  # the figure code took from a parallel list ("Full Year, Fourth Quarter ... $15.51, $5.98"); it overrides the model's value


# "Full Year and Fourth Quarter Net Earnings were $12.19 Per Share and $3.10 Per Share", "... Per Share of $16.88 and $2.52":
# period labels in one order, then figures in the same order. Only an EPS row reads these, and only with the same count of each.
_PARALLEL_LABEL = re.compile(r"full[- ]year|fiscal year|(?:first|second|third|fourth) quarter|\bQ[1-4]\b", re.IGNORECASE)
_PARALLEL_SEP = re.compile(r"^[\s,;&]*(?:and)?[\s,;&]*$", re.IGNORECASE)
_PARALLEL_METRIC = re.compile(r"\b(adjusted\s+)?(?:net\s+)?earnings\b", re.IGNORECASE)
_PARALLEL_LEAD = re.compile(r"(?:\s|,|:|-|\b(?:per share|were|was|of|is|are)\b)*", re.IGNORECASE)
_QUARTER_NAME = {1: "first quarter", 2: "second quarter", 3: "third quarter", 4: "fourth quarter"}


def parallel_eps(sentence: str, period: tuple[int | None, int] | None, metric: str) -> float | None:
    """The EPS figure a headline in parallel order prints for this period, or None. The sentence must be nothing but
    period labels (then `Net Earnings` or `Adjusted Net Earnings`) followed by dollar figures, with as many figures as
    labels, and name this row's label ("full year" for a fiscal-year row, "fourth quarter" for Q4). The first label takes the
    first figure, the second the second. `Adjusted` is the non-GAAP row's and its absence the GAAP row's; any other wording
    between labels and figures ("Reflect", "Include") refuses the sentence, as does a label count that differs from the figure count."""
    if period is None or not metric.startswith("EPS"):
        return None
    text = sentence.lstrip(" \u2022-*\t")
    m = _PARALLEL_METRIC.search(text)
    if not m or "per share" not in text.lower():
        return None
    prefix = text[: m.start()]
    labels = [(x.group(0).lower().replace("-", " "), x.span()) for x in _PARALLEL_LABEL.finditer(prefix)]
    if len(labels) < 2:
        return None
    gaps = [prefix[: labels[0][1][0]]] + [prefix[labels[i][1][1] : labels[i + 1][1][0]] for i in range(len(labels) - 1)] + [prefix[labels[-1][1][1] :]]
    if not all(_PARALLEL_SEP.match(g) for g in gaps):
        return None
    first = _DOLLAR_NUMBER.search(text, m.end())
    if first is None or not _PARALLEL_LEAD.fullmatch(text[m.end() : first.start()]):
        return None
    figures = _DOLLAR_NUMBER.findall(text[m.end() :])
    if len(figures) != len(labels) or _ANY_YEAR.search(text):
        return None
    if bool(m.group(1)) != ("non-gaap" in metric.lower()):
        return None
    want = "full year" if period[0] is None else _QUARTER_NAME[period[0]]
    names = ["full year" if n in ("fiscal year", "full year") else _QUARTER_NAME[int(n[1])] if re.fullmatch(r"q[1-4]", n) else n
             for n, _ in labels]
    if want not in names or names.count(want) != 1:
        return None
    try:
        return float(figures[names.index(want)].replace("$", "").replace(",", "").replace("(", "-").replace(")", "").strip())
    except ValueError:
        return None


def _section_label(doc: DocText, line_index: int, lookback: int) -> str | None:
    for j in range(line_index - 1, max(-1, line_index - 1 - lookback), -1):
        line = doc.lines[j]
        if 0 < len(line.split()) <= 6 and not re.search(r"\d", line) and not line.endswith((".", ":")):
            return line
    return None


def _units_note(doc: DocText, line_index: int, lookback: int) -> str | None:
    for j in range(line_index, max(-1, line_index - 1 - lookback), -1):
        if _UNITS_NOTE.search(doc.lines[j]):
            return doc.lines[j][:160]
    return None


# What a full-year result line says about its period: "fiscal 2022", "full year", "for the year", "for fiscal ...".
_FULL_YEAR_LINE = re.compile(r"\bfiscal (?:year )?20\d\d\b|\bfull[- ]year\b|\bfor the year\b|\bfor fiscal\b", re.IGNORECASE)


def select_lines(
    store: DocStore,
    metas: list[dict[str, Any]],
    matcher: re.Pattern[str],
    extra: re.Pattern[str] | None,
    metric: str,
    period: tuple[int | None, int] | None,
    cfg: dict[str, Any],
    number: re.Pattern[str],
    *,
    require_number: bool = True,
    reference: re.Pattern[str] | None = None,
    require_period: bool = False,
    reject: Callable[[DocText, int, str], bool] | None = None,
) -> list[Line]:
    """Lines in these releases that name the metric next to a number (and `extra`, if given). Best first, then in
    order. An outcome line must state a value, so `require_number` defaults on; an acknowledgement line often
    just narrates the gap ("below our guidance") with the value itself in a sentence nearby, so the
    acknowledgement search turns it off. The acknowledgement search also asks, of the sentence that holds the
    direction word, that it hold a `reference` to what was guided and (`require_period`) that it name this
    row's period: without the first it describes results and does not admit a gap, and without the second it
    is about some other quarter or year. `reject(doc, char_start, sentence)` drops a sentence before it is scored."""
    lowered = metric.lower()  # canonical names are mixed case: "gross margin non-GAAP"
    wants_non_gaap = "non-gaap" in lowered
    wants_gaap = "gaap" in lowered and not wants_non_gaap
    scored: list[tuple[int, int, Line]] = []
    order = 0
    for meta in metas:
        doc = store.get(meta)
        for k, (start, end) in enumerate(doc.spans):
            sentence = doc.text[start:end]
            li = doc.line_of(start)
            if len(sentence) > EXCERPT_LIMIT or (require_number and not number.search(sentence)):
                continue
            if reject is not None and reject(doc, start, sentence):
                continue
            pick = parallel_eps(sentence, period, metric)
            if pick is None and not matcher.search(sentence):
                # A full-year row also takes a line that states the year and a number when the line above it is what
                # names the metric: the label and the figure are on separate lines in a results table.
                label = doc.lines[li - 1] if li > 0 else ""
                if not (period and period[0] is None and extra is None and _FULL_YEAR_LINE.search(sentence)
                        and number.search(sentence) and matcher.search(label)):
                    continue
            if extra is not None and not extra.search(sentence):
                continue
            if reference is not None and not reference.search(sentence):
                continue
            if require_period and (period is None or not names_period(sentence, period)):
                continue
            section = _section_label(doc, li, int(cfg["section_lookback_lines"]))
            tag = f"{section or ''} {sentence}".lower()
            score = 2
            if period:
                score += period_signal(sentence, *period)
            if _GUIDANCE_WORDS.search(sentence):
                score -= 3  # an actual result does not read like guidance
            if wants_non_gaap and "non-gaap" in tag:
                score += 1
            if wants_gaap and "gaap" in tag and "non-gaap" not in tag:
                score += 1
            scored.append((-score, order, Line(
                accession=meta["accession"], filed_at=meta["filed_at"], sentence=sentence, char_start=start,
                before=[doc.text[a:b][:300] for a, b in doc.spans[max(0, k - 2) : k]],
                section=section, units_note=_units_note(doc, li, int(cfg["units_lookback_lines"])), score=score, pick=pick)))
            order += 1
    best = sorted(scored)[: int(cfg["max_lines"])]
    return [line for _, _, line in sorted(best, key=lambda t: (t[2].filed_at, t[2].accession, t[2].char_start))]


def render_lines(lines: list[Line]) -> str:
    out = []
    for n, line in enumerate(lines, 1):
        head = f"[{n}] filed {line.filed_at} | {line.accession}"
        if line.section:
            head += f" | section: {line.section}"
        if line.units_note:
            head += f" | units note: {line.units_note}"
        body = [head] + [f"    before: {b}" for b in line.before] + [f"    LINE: {line.sentence}"]
        out.append("\n".join(body))
    return "\n".join(out)


def _range_text(a: dict[str, Any]) -> str:
    low, high = a["target_low"], a["target_high"]
    if low is not None and high is not None:
        return f"{low:g}" if low == high else f"{low:g} to {high:g}"
    return f"at least {low:g}" if low is not None else f"no more than {high:g}"


def _evidence(meta: dict[str, Any], excerpt: str) -> Evidence:
    return Evidence(
        source_url=meta["final_url"], accession_number=meta["accession"], filing_type=meta["filing_type"],
        filed_at=date.fromisoformat(meta["filed_at"]), fetched_at=datetime.fromisoformat(meta["fetched_at"]),
        content_sha256=meta["content_sha256"], excerpt=excerpt,
    )


# --- guards on an outcome line ----------------------------------------------
#
# A reported value is a single number, stated in a results release for a period that has closed. A line that prints
# a range, reads as a forecast, sits under an outlook heading, is on the wrong basis or names a later period is not one.

_OUTLOOK_HEADING = re.compile(r"\b(?:Outlook|Guidance|Targets?)\b", re.IGNORECASE)
_FORWARD_WORDS = re.compile(
    r"\b(?:expects?|expected|anticipates?|forecast|forecasted|guidance|outlook|targets?|projected|projections?|will be|to be in the range)\b", re.IGNORECASE)
_AMOUNT = r"\$?\s?\d[\d,]*(?:\.\d+)?\s?(?:%|percent\b|(?:million|billion|thousand)\b|[BMK]\b)?"
_RANGE = re.compile(rf"(?<![\d.,])(?<!\d-)({_AMOUNT})\s*(?:to|through|-|\u2013|\u2014)\s*({_AMOUNT})(?![\d]|-\d)", re.IGNORECASE)
_BETWEEN = re.compile(rf"\bbetween\s+{_AMOUNT}\s+and\s+{_AMOUNT}", re.IGNORECASE)
_PLUS_MINUS = re.compile(r"plus or minus|\u00b1", re.IGNORECASE)
_FROM_BEFORE = re.compile(r"\bfrom\s+(?:about\s+|approximately\s+)?$", re.IGNORECASE)
_YEAR_ONLY = re.compile(r"(?:19|20)\d\d")
_BASIS_MARK = re.compile(r"non[- ]?gaap|adjusted", re.IGNORECASE)
_GAAP_MARK = re.compile(r"(?<!non)(?<!non-)(?<!non )\bgaap\b", re.IGNORECASE)
_FISCAL_YEAR_NAMED = re.compile(r"\b(?:fiscal(?: year)? ?|FY ?)(20\d\d|\d\d)\b", re.IGNORECASE)
_QUARTER_YEAR_NAMED = re.compile(
    r"\b(?:(?:first|second|third|fourth|1st|2nd|3rd|4th)[- ]quarter(?: of)?(?: fiscal)? ?|Q[1-4] ?(?:fiscal |FY ?)?)(20\d\d)\b", re.IGNORECASE)
_NUMBER_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?")


def has_range(sentence: str) -> bool:
    """A range around a number: "$2.55 - $2.75", "3.1% to 3.4%", "11-12", "between 5 and 6", "plus or minus". "from $1.2 billion to
    $1.4 billion" is a change that ended on one number, and two years or a date's parts are not an amount."""
    if _BETWEEN.search(sentence) or _PLUS_MINUS.search(sentence):
        return True
    for m in _RANGE.finditer(sentence):
        if _FROM_BEFORE.search(sentence[: m.start()]):
            continue
        if all(_YEAR_ONLY.fullmatch(g.strip()) for g in m.groups()):
            continue
        return True
    return False


def forward_looking(sentence: str, company: Company) -> bool:
    """A forecast word, whole and in any case. A company named for one of them (Target) is not forecasting by naming itself."""
    for word in re.findall(r"[A-Za-z]+", company.name):
        if _FORWARD_WORDS.fullmatch(word):
            sentence = re.sub(rf"\b{word}\b", " ", sentence)
    return bool(_FORWARD_WORDS.search(sentence))


def value_in_evidence(value: float, sentence: str) -> bool:
    """The reported value is printed in the sentence, at up to 2 decimals and with or without thousands separators, as is or
    in the unit next to it (billions printed as millions or thousands, or the other way round). One footnote digit stuck to
    the end of a 3-decimal figure ("$1.331" for 1.33) is dropped. Signs are the sentence's own business."""
    base = abs(value)
    wanted = [base, base * 1e3, base * 1e6, base / 1e3, base / 1e6]
    for m in _NUMBER_TOKEN.finditer(sentence):
        token = m.group().replace(",", "")
        decimals = len(token.split(".")[1]) if "." in token else 0
        forms = [token] if decimals <= 2 else [token[:-1]] if decimals == 3 else []
        if any(math.isclose(float(f), w, rel_tol=1e-9, abs_tol=1e-9) for f in forms for w in wanted):
            return True
    return False


def names_later_period(sentence: str, period: tuple[int | None, int]) -> bool:
    """Does the sentence name a fiscal year after the row's, or (for a quarter row, with no earlier year named) a later quarter?"""
    quarter, year = period
    years = {int(y) if len(y) == 4 else 2000 + int(y) for y in _FISCAL_YEAR_NAMED.findall(sentence)}
    years |= {int(y) for y in _QUARTER_YEAR_NAMED.findall(sentence)}
    if any(y > year for y in years):
        return True
    if quarter is None or any(y < year for y in years):
        return False
    return any(_quarter_regex(q).search(sentence) for q in range(quarter + 1, 5))


def outcome_line_rejection(
    store: "DocStore", doc: DocText, char_start: int, sentence: str, a: dict[str, Any], matcher: re.Pattern[str] | None
) -> str | None:
    """Why this sentence cannot be an outcome for the draft, or None. Everything here is the line's own: the value
    check, which needs the model's answer, is `value_in_evidence`."""
    period = parse_period(a["target_period"])
    li = doc.line_of(char_start)
    if has_range(sentence):
        return "the sentence prints a range, and a reported value is a single number"
    if forward_looking(sentence, store.company):
        return "the sentence is forward-looking"
    if li in doc.outlook_lines:
        return "the sentence sits in an outlook, guidance or targets section"
    if period is not None:
        close = store.company.period_end(period[1], period[0])
        if close is not None and date.fromisoformat(doc.meta["filed_at"]) <= close:
            return "the filing is dated on or before the end of the period"
        if names_later_period(sentence, period):
            return "the sentence names a later period than the row's"
    lowered = a["metric"].lower()
    if "gaap" in lowered or "adjusted" in lowered:
        if _BASIS_MARK.search(sentence) and _GAAP_MARK.search(sentence):
            return None  # "GAAP and Adjusted EPS were ...": the sentence names both bases, so it serves a row of either
        marked = bool(_BASIS_MARK.search(sentence) or _BASIS_MARK.search(_section_label(doc, li, 25) or ""))
        if "non-gaap" in lowered or "adjusted" in lowered:
            if not marked and matcher is not None and _prints_both_bases(doc, matcher):
                return "a non-GAAP row matched a sentence with no non-GAAP or adjusted label, and the release prints both"
        elif _BASIS_MARK.search(sentence):
            return "a GAAP row matched a sentence labelled adjusted or non-GAAP"
    return None


def _prints_both_bases(doc: DocText, matcher: re.Pattern[str]) -> bool:
    cache = doc.__dict__.setdefault("_both", {})
    key = matcher.pattern
    if key not in cache:
        hits = [doc.text[a:b] for a, b in doc.spans if matcher.search(doc.text[a:b])]
        cache[key] = any(_BASIS_MARK.search(h) for h in hits) and any(not _BASIS_MARK.search(h) for h in hits)
    return cache[key]


# --- stage 1: outcomes -----------------------------------------------------


def load_drafts(company: Company) -> list[dict[str, Any]]:
    path = DRAFTS_DIR / f"{company.cik}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def prefer_prose(lines: list[Line], store: DocStore, period: tuple[int | None, int] | None, unit: str, lookback: int) -> list[Line]:
    """Where a release offers a prose sentence for the metric, drop that release's multi-column table lines that
    `column_confirmed` would hold back. A prose line is one that is not an ambiguous multi-column row and has a
    reporting verb ("Revenues of $400.3 Billion Grew 8%", "Net Earnings were $15.51"): it states the figure without
    the model having to pick a column. A table row whose header names the period is confirmed and is kept, and a
    release with no prose line keeps every line, so this never leaves a release without a candidate."""
    def is_prose(line: Line) -> bool:
        return not is_ambiguous_multi_column(unit, line.sentence) and bool(_REPORTING_VERB.search(line.sentence))

    with_prose = {ln.accession for ln in lines if is_prose(ln)}
    if not with_prose:
        return lines
    metas = {m["accession"]: m for m in store.metas}
    return [ln for ln in lines if ln.accession not in with_prose or is_prose(ln)
            or column_confirmed(store, metas[ln.accession], ln, period, unit, lookback)]


def build_outcome_requests(
    drafts_by_company: dict[str, list[dict[str, Any]]],
    stores: dict[str, DocStore],
    cfg: dict[str, Any],
    number: re.Pattern[str],
    *,
    today: date,
    as_of: date | None = None,
) -> tuple[list[llm.LlmRequest], dict[str, tuple[dict[str, Any], list[Line]]], dict[str, str]]:
    """(requests, custom_id -> (draft, lines), draft_id -> why there is no request). `as_of` is the date a period must
    have closed by to expect an outcome (the last filing date in config.yaml); a draft whose period closes after it
    is "period not closed", not "no release found". Defaults to `today`."""
    requests: list[llm.LlmRequest] = []
    index: dict[str, tuple[dict[str, Any], list[Line]]] = {}
    reasons: dict[str, str] = {}
    for cik, drafts in drafts_by_company.items():
        store = stores[cik]
        for d in drafts:
            a = d["assumption"]
            period = parse_period(a["target_period"])
            matcher = metric_pattern(a["metric"], cfg["metric_terms"])
            if period is None:
                reasons[d["draft_id"]] = f"period {a['target_period']!r} not understood"
                continue
            if matcher is None:
                reasons[d["draft_id"]] = f"metric {a['metric']!r} not recognised"
                continue
            stated = date.fromisoformat(a["stated_at"])
            horizon = min(today, stated + timedelta(days=int(cfg["search_days"]))).isoformat()
            later = [m for m in store.metas if a["stated_at"] < m["filed_at"] <= horizon]
            named = [m for m in later if release_names_period(store.get(m), period, int(cfg["head_chars"]), store.company)]
            if not named:
                close = store.company.period_end(period[1], period[0])
                reasons[d["draft_id"]] = ("period not closed" if close is not None and close > (as_of or today)
                                          else "no later 8-K release for that period found")
                continue
            def reject(doc: DocText, start: int, sentence: str, a=a, matcher=matcher, store=store) -> bool:
                return outcome_line_rejection(store, doc, start, sentence, a, matcher) is not None

            lines = select_lines(store, named[: int(cfg["max_docs"])], matcher, None, a["metric"], period, cfg, number, reject=reject)
            lines = prefer_prose(lines, store, period, a["unit"], int(cfg["section_lookback_lines"]))
            if not lines:
                reasons[d["draft_id"]] = "the matching release has no line naming the metric with a number"
                continue
            user = (
                f"Guidance: {d['company']} guided {a['metric']} for {a['target_period']} "
                f"(stated {a['stated_at']}): {_range_text(a)} {a['unit']}.\n"
                f"Guidance sentence: {a['text']}\n\nLines from later filings (oldest first):\n{render_lines(lines)}"
            )
            cid = f"o-{d['draft_id']}"
            requests.append(llm.LlmRequest(cid, OUTCOME_SYSTEM, user, MAX_TOKENS, OUTCOME_SCHEMA))
            index[cid] = (d, lines)
    return requests, index, reasons


_FORWARD_LOOKING_WORDS = re.compile(r"\b(?:expected|expects|guidance|outlook|anticipates)\b", re.IGNORECASE)
_REPORTING_VERB = re.compile(
    r"\b(?:was|were|had|did|used|delivered|reported|returned|grew|increased|decreased|declined|rose|fell|"
    r"totaled|totalled|generated|achieved|completed|recorded|posted|reached|earned|gained|lost|exceeded|"
    r"repurchased|paid|came in)\b",
    re.IGNORECASE,
)


def excerpt_reports_a_result(excerpt: str) -> bool:
    """False when the excerpt carries forward-looking wording (expected, expects, guidance, outlook,
    anticipates) with no past-tense reporting verb to anchor it to something that already happened. "was
    above the high end of the Company's guidance range" passes: it has "was". "is expected to be
    approximately 72%" does not: nothing in it says this already happened."""
    return not _FORWARD_LOOKING_WORDS.search(excerpt) or bool(_REPORTING_VERB.search(excerpt))


# A table row with the current period's value squashed against prior periods' ("Revenue $4,726 $3,866
# $3,014 Up 22% Up 57%") cannot be trusted to have had its current-period cell picked out correctly by the
# model alone. Comparison language ("compared with $3.21 billion a year earlier") makes a sentence with
# several same-unit numbers unambiguous despite the count, so it is not one of these rows.
_DOLLAR_NUMBER = re.compile(r"\$\s?-?\(?\d[\d,]*(?:\.\d+)?\)?")
_PERCENT_NUMBER = re.compile(r"-?\(?\d[\d,]*(?:\.\d+)?\)?\s?%")
_COMPARISON_LANGUAGE = re.compile(r"compared (?:with|to)|a year (?:ago|earlier)|\bsequentially\b|\bversus\b|\bvs\.?\b", re.IGNORECASE)
_HEADER_SENTENCE_END = (".", "!", "?")


def is_ambiguous_multi_column(unit: str, sentence: str) -> bool:
    """Three or more numbers of the metric's own kind (percent-signed for a percent metric, dollar-signed
    otherwise: a dollar amount and a per-share figure both print with "$"), and nothing that says which is
    which. A growth-rate annotation next to the value ("up 18% Y/Y") is a different unit and does not count."""
    same_unit = _PERCENT_NUMBER if unit == "percent" else _DOLLAR_NUMBER
    return len(same_unit.findall(sentence)) >= 3 and not _COMPARISON_LANGUAGE.search(sentence)


def find_result_header(lines: list[str], row_index: int, lookback: int) -> str | None:
    """The nearest line above a results-table row that names two or more periods. Unlike a guidance block's
    header (`02_candidates.find_table_header`), a results header often carries Q/Q, Y/Y or a % change column
    next to the period labels ("Q3 FY21 Q2 FY21 Q3 FY20 Q/Q Y/Y"), so those are not excluded here."""
    window = range(row_index - 1, max(-1, row_index - 1 - lookback), -1)
    for j in window:
        periods = candidates_step.parse_periods(lines[j])
        if len(periods) >= 2 and not lines[j].rstrip().endswith(_HEADER_SENTENCE_END):
            return lines[j]
    return None


def column_confirmed(
    store: DocStore, meta: dict[str, Any], line: Line, period: tuple[int | None, int] | None, unit: str, lookback: int
) -> bool:
    """True unless the picked line is an ambiguous multi-column table row with no header above it naming the
    period asked about."""
    if not is_ambiguous_multi_column(unit, line.sentence):
        return True
    doc = store.get(meta)
    header = find_result_header(doc.lines, doc.line_of(line.char_start), lookback)
    return bool(header and period is not None and period in candidates_step.parse_periods(header))


_DOWN = re.compile(r"\b(?:decreased|decreases|declined|declines|fell|down|lower)\b", re.IGNORECASE)
_UP = re.compile(r"\b(?:increased|increases|grew|rose|up|higher)\b", re.IGNORECASE)


def comparable_sales_sign(metric: str, value: float, sentence: str) -> float:
    """Comparable sales are reported as a change ("comparable sales decreased 0.7 percent", "U.S. Comparable Sales
    Decreased 0.7%"), so the sign is in the verb and the number is unsigned. A positive value on a line that only says
    the sales went down, or a negative one on a line that only says they went up, is turned round by code, whatever the
    model made of the verb. A line with both words, or neither, is left as the model read it."""
    if metric != "comparable sales" or value == 0:
        return value
    down, up = bool(_DOWN.search(sentence)), bool(_UP.search(sentence))
    if down and not up:
        return -abs(value)
    if up and not down:
        return abs(value)
    return value


def derive_outcomes(
    index: dict[str, tuple[dict[str, Any], list[Line]]],
    results: dict[str, llm.Result],
    stores: dict[str, DocStore],
    cfg: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    """(outcome per draft_id, rejects, reasons for drafts with no outcome). Pure: no model call."""
    outcomes: dict[str, dict[str, Any]] = {}
    rejects: list[dict[str, Any]] = []
    reasons: dict[str, str] = {}
    ratio = float(cfg["plausible_ratio"])
    lookback = int(cfg["section_lookback_lines"])
    for cid, (d, lines) in index.items():
        res = results.get(cid)
        if res is None or not res.ok:
            reasons[d["draft_id"]] = "outcome search not completed"
            continue
        payload = llm.parse_json_text(res.text)
        if not isinstance(payload, dict) or not {"index", "value", "unit"} <= set(payload):
            rejects.append({"draft_id": d["draft_id"], "reason": "output did not parse as {index, value, unit}"})
            reasons[d["draft_id"]] = "model output rejected"
            continue
        if payload["index"] is None:
            reasons[d["draft_id"]] = "the model found no line reporting that metric for that period"
            continue
        try:
            n = int(payload["index"])
            if not 1 <= n <= len(lines) or payload["value"] is None:
                raise ValueError(f"index {payload['index']!r} or value {payload['value']!r} is not usable")
            line = lines[n - 1]
            if not excerpt_reports_a_result(line.sentence):
                raise ValueError("the excerpt has forward-looking wording (expected, expects, guidance, outlook "
                                  "or anticipates) with no past-tense reporting verb, so it does not report a result")
            a = d["assumption"]
            store = stores[d["cik"]]
            meta = next(m for m in store.metas if m["accession"] == line.accession)
            why = outcome_line_rejection(store, store.get(meta), line.char_start, line.sentence, a, metric_pattern(a["metric"], cfg["metric_terms"]))
            if why:
                raise ValueError(why)
            value = (convert(line.pick, "USD per share", a["unit"]) if line.pick is not None
                     else convert(float(payload["value"]), payload["unit"], a["unit"]))
            value = comparable_sales_sign(a["metric"], value, line.sentence)
            if not value_in_evidence(value, line.sentence):
                raise ValueError(f"the value {value:g} is not printed in the sentence")
            mid = [x for x in (a["target_low"], a["target_high"]) if x is not None]
            centre = sum(mid) / len(mid)
            if centre > 0 and value > 0 and not (1 / ratio <= value / centre <= ratio):
                raise ValueError(f"reported {value:g} is {value / centre:.1f}x the guidance {centre:g}: likely a unit or row mix-up")
        except (ValueError, TypeError, StopIteration, ValidationError) as exc:
            rejects.append({"draft_id": d["draft_id"], "output": payload, "reason": str(exc)[:300]})
            reasons[d["draft_id"]] = "model output rejected"
            continue
        if not column_confirmed(stores[d["cik"]], meta, line, parse_period(a["target_period"]), a["unit"], lookback):
            reasons[d["draft_id"]] = "multi-column, unconfirmed"
            continue
        outcome = Outcome(reported_value=value, reported_at=date.fromisoformat(line.filed_at), evidence=_evidence(meta, line.sentence))
        outcomes[d["draft_id"]] = outcome.model_dump(mode="json")
    return outcomes, rejects, reasons


# --- stage 2: acknowledgements ---------------------------------------------


def missed(draft: dict[str, Any], outcome: dict[str, Any]) -> bool:
    a = draft["assumption"]
    return rubric.resolve(a["target_low"], a["target_high"], outcome["reported_value"], reviewed=True) == "missed"


def build_ack_requests(
    drafts_by_company: dict[str, list[dict[str, Any]]],
    outcomes: dict[str, dict[str, Any]],
    stores: dict[str, DocStore],
    cfg: dict[str, Any],
    number: re.Pattern[str],
    *,
    today: date,
) -> tuple[list[llm.LlmRequest], dict[str, tuple[dict[str, Any], list[Line]]]]:
    # Vocabulary depends on the side the value fell on and on whether that side was good or bad for the
    # metric: "below" or "higher than" name the side, "short of" or "better than" name the outcome. A row is
    # searched with its side's words plus its direction's words (config.yaml, acknowledgement_phrases).
    phrases = cfg["acknowledgement_phrases"]
    phrase_patterns = {
        (side, direction): re.compile("|".join(phrases[side] + phrases[direction]), re.IGNORECASE)
        for side in ("below", "above")
        for direction in ("worse", "better")
    }
    reference = re.compile("|".join(cfg["acknowledgement_guidance_reference"]), re.IGNORECASE)
    requests: list[llm.LlmRequest] = []
    index: dict[str, tuple[dict[str, Any], list[Line]]] = {}
    for cik, drafts in drafts_by_company.items():
        store = stores[cik]
        for d in drafts:
            outcome = outcomes.get(d["draft_id"])
            if outcome is None or not missed(d, outcome):
                continue
            a = d["assumption"]
            side = rubric.side(a["target_low"], a["target_high"], outcome["reported_value"])
            if side is None:
                continue
            direction = rubric.direction(a["target_low"], a["target_high"], outcome["reported_value"], polarity.higher_is_better(a["metric"]))
            matcher = metric_pattern(a["metric"], cfg["metric_terms"])
            if matcher is None:
                continue
            reported = outcome["reported_at"]
            horizon = min(today, date.fromisoformat(reported) + timedelta(days=int(cfg["ack_days"]))).isoformat()
            metas = [m for m in store.all_metas if reported <= m["filed_at"] <= horizon]  # every filing: the 10-Q and 10-K too
            # No number is required here: the line that admits the gap often just narrates it ("below our
            # guidance") with the value itself already given in the user message, or in a sentence just before.
            lines = select_lines(store, metas, matcher, phrase_patterns[(side, direction)], a["metric"], parse_period(a["target_period"]),
                                  cfg, number, require_number=False, reference=reference, require_period=True)
            if not lines:
                continue
            side_words = "above the high end of" if side == "above" else "below the low end of"
            user = (
                f"Guidance: {d['company']} guided {a['metric']} for {a['target_period']} to {_range_text(a)} {a['unit']}.\n"
                f"Actually reported: {outcome['reported_value']:g} {a['unit']}, on {reported}, {side_words} the guided range (that is {direction} than guided for {a['metric']}).\n\n"
                f"Lines from the company's filings from {reported} on (oldest first):\n{render_lines(lines)}"
            )
            cid = f"k-{d['draft_id']}"
            requests.append(llm.LlmRequest(cid, ACK_SYSTEM, user, MAX_TOKENS, ACK_SCHEMA))
            index[cid] = (d, lines)
    return requests, index


def derive_acknowledgements(
    index: dict[str, tuple[dict[str, Any], list[Line]]],
    results: dict[str, llm.Result],
    stores: dict[str, DocStore],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """(acknowledgement per draft_id, rejects). The earliest line the model confirms wins."""
    found: dict[str, dict[str, Any]] = {}
    rejects: list[dict[str, Any]] = []
    for cid, (d, lines) in index.items():
        res = results.get(cid)
        if res is None or not res.ok:
            continue
        payload = llm.parse_json_text(res.text)
        if not isinstance(payload, dict) or not isinstance(payload.get("indices"), list):
            rejects.append({"draft_id": d["draft_id"], "reason": "output did not parse as {indices: [...]}"})
            continue
        valid = sorted(int(i) for i in payload["indices"] if isinstance(i, int) and 1 <= i <= len(lines))
        if not valid:
            continue
        line = lines[valid[0] - 1]  # lines are already oldest first
        try:
            meta = next(m for m in stores[d["cik"]].all_metas if m["accession"] == line.accession)
            found[d["draft_id"]] = {
                "acknowledged_at": line.filed_at,
                "evidence": _evidence(meta, line.sentence).model_dump(mode="json"),
            }
        except (StopIteration, ValidationError) as exc:
            rejects.append({"draft_id": d["draft_id"], "reason": str(exc)[:300]})
    return found, rejects


# --- stage 3: withdrawals ---------------------------------------------------


def select_withdrawal_lines(
    store: DocStore, metas: list[dict[str, Any]], phrases: re.Pattern[str], subjects: re.Pattern[str], cfg: dict[str, Any]
) -> list[Line]:
    """Sentences that say withdraw, suspend or no longer providing, about guidance, oldest first. No number is needed:
    "The Company is withdrawing its guidance." has none, and nor does it name the metric."""
    found: list[Line] = []
    for meta in metas:
        doc = store.get(meta)
        for k, (start, end) in enumerate(doc.spans):
            sentence = doc.text[start:end]
            if len(sentence) > EXCERPT_LIMIT or not phrases.search(sentence) or not subjects.search(sentence):
                continue
            li = doc.line_of(start)
            found.append(Line(
                accession=meta["accession"], filed_at=meta["filed_at"], sentence=sentence, char_start=start,
                before=[doc.text[a:b][:300] for a, b in doc.spans[max(0, k - 2) : k]],
                section=_section_label(doc, li, int(cfg["section_lookback_lines"])), units_note=None, score=0))
    found.sort(key=lambda line: (line.filed_at, line.accession, line.char_start))
    return found[: int(cfg["max_lines"])]


def build_withdrawal_requests(
    drafts_by_company: dict[str, list[dict[str, Any]]],
    companies_by_cik: dict[str, Company],
    stores: dict[str, DocStore],
    cfg: dict[str, Any],
    *,
    today: date,
) -> tuple[list[llm.LlmRequest], dict[str, tuple[dict[str, Any], list[Line], date]]]:
    """A request per draft that has candidate sentences in the 8-K exhibits filed after the guidance and on or before
    the day its period closed. A draft whose period cannot be dated (no fiscal calendar, or a period not understood)
    is not searched: without the close there is no "before the period closed" to test."""
    phrases = re.compile("|".join(f"(?:{p})" for p in cfg["withdrawal_phrases"]), re.IGNORECASE)
    subjects = re.compile(cfg["withdrawal_subjects"], re.IGNORECASE)
    requests: list[llm.LlmRequest] = []
    index: dict[str, tuple[dict[str, Any], list[Line], date]] = {}
    for cik, drafts in drafts_by_company.items():
        store, company = stores[cik], companies_by_cik[cik]
        for d in drafts:
            a = d["assumption"]
            period = parse_period(a["target_period"])
            close = company.period_end(period[1], period[0]) if period else None
            if close is None:
                continue
            named = period_named_by_end_date(company, a["text"], after=date.fromisoformat(a["stated_at"]))
            if named is not None and named.startswith("Q") == a["target_period"].startswith("Q") and named != a["target_period"]:
                continue  # the sentence's own end date puts the guidance in another period: the label is wrong, so no withdrawal is proposed against it
            horizon = min(today, close).isoformat()
            metas = [m for m in store.metas if a["stated_at"] < m["filed_at"] <= horizon]
            lines = select_withdrawal_lines(store, metas, phrases, subjects, cfg)
            if not lines:
                continue
            user = (
                f"Guidance: {d['company']} guided {a['metric']} for {a['target_period']} to {_range_text(a)} {a['unit']} "
                f"(stated {a['stated_at']}). The period ends {close.isoformat()}.\n\n"
                f"Lines from the company's 8-K exhibits filed after that and before the period ended (oldest first):\n{render_lines(lines)}"
            )
            cid = f"w-{d['draft_id']}"
            requests.append(llm.LlmRequest(cid, WITHDRAWAL_SYSTEM, user, MAX_TOKENS, ACK_SCHEMA))
            index[cid] = (d, lines, close)
    return requests, index


def derive_withdrawals(
    index: dict[str, tuple[dict[str, Any], list[Line], date]],
    results: dict[str, llm.Result],
    stores: dict[str, DocStore],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """(withdrawal per draft_id, rejects). The earliest line the model confirms wins, and it must be dated on or before
    the day the period closed, which is the rubric's own test (`rubric.withdrawn_before_close`)."""
    found: dict[str, dict[str, Any]] = {}
    rejects: list[dict[str, Any]] = []
    for cid, (d, lines, close) in index.items():
        res = results.get(cid)
        if res is None or not res.ok:
            continue
        payload = llm.parse_json_text(res.text)
        if not isinstance(payload, dict) or not isinstance(payload.get("indices"), list):
            rejects.append({"draft_id": d["draft_id"], "reason": "output did not parse as {indices: [...]}"})
            continue
        valid = sorted(int(i) for i in payload["indices"] if isinstance(i, int) and 1 <= i <= len(lines))
        for n in valid:  # oldest first
            line = lines[n - 1]
            if not rubric.withdrawn_before_close(date.fromisoformat(line.filed_at), close):
                continue
            try:
                meta = next(m for m in stores[d["cik"]].metas if m["accession"] == line.accession)
                found[d["draft_id"]] = {
                    "withdrawn_at": line.filed_at,
                    "period_close": close.isoformat(),
                    "evidence": _evidence(meta, line.sentence).model_dump(mode="json"),
                }
            except (StopIteration, ValidationError) as exc:
                rejects.append({"draft_id": d["draft_id"], "reason": str(exc)[:300]})
            break
    return found, rejects


# --- running ---------------------------------------------------------------


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.exists() else []


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows and not path.exists():  # a dry run must not leave empty files that look like output
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def _json_check(*keys: str):
    def check(text: str) -> None:
        payload = llm.parse_json_text(text)
        if not isinstance(payload, dict) or not set(keys) <= set(payload):
            raise RuntimeError(f"canary output lacks {keys}: {text[:200]!r}")
    return check


class Stopped(Exception):
    """A stage stopped the run, and nothing is written: no credentials or over the budget (3), a batch still processing (5)."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


def _stage(client, cfg, ledger, step, requests, *, submit: bool, wait_minutes: float, check,
           force_sync: bool = False, cap: llm.RunCap | None = None) -> dict[str, llm.Result]:
    """Project, print, and (with --submit) run one stage. The answers to every request, this run's and earlier ones.
    Raises Stopped when the run cannot go on."""
    archive = llm.RawArchive(step)
    have = llm.current_results(archive.load(), requests, cfg.model)
    todo = [r for r in requests if not (r.custom_id in have and have[r.custom_id].ok)]
    print(f"{step}: {len(requests):,} requests ({len(requests) - len(todo):,} already answered, {len(todo):,} to send)")
    exact = llm.credentials_available(client, cfg.model)
    counts = llm.count_input_tokens(client, cfg, todo) if exact and todo else llm.estimate_tokens(todo)
    projection = llm.project(todo, counts, exact=exact, llm=cfg, expected_output_per_request=EXPECTED_OUTPUT_TOKENS)
    print(projection.describe(step, cap=cfg.budget_usd, committed=ledger.committed_usd()))
    if not (submit and todo):
        return have
    if not exact:
        print("STOPPED: no API credentials found. Set ANTHROPIC_API_KEY (or put it in .env at the repo root) "
              "and run again. Nothing was submitted.", file=sys.stderr)
        raise Stopped(3)
    try:
        outcome = llm.run_batch(client, cfg, step, todo, projection=projection, canary_check=check,
                                ledger=ledger, archive=archive, wait_seconds=wait_minutes * 60,
                                force_sync=force_sync, cap=cap, spend_log=common.SPEND_LEDGER_PATH)
    except llm.BudgetExceeded as exc:
        print(f"STOPPED: {exc}", file=sys.stderr)
        raise Stopped(3) from exc
    for note in outcome.notes:
        print(f"note: {note}")
    if outcome.pending_batch:
        print(f"STOPPED: batch {outcome.pending_batch} ({step}) has not ended, so nothing was written to data/outcomes/. "
              "Run the same command again to resume it; nothing will be resubmitted.", file=sys.stderr)
        raise Stopped(5)
    return llm.current_results(archive.load(), requests, cfg.model)  # run_batch answers only the requests it was given


#: The reasons a row has no outcome that a later filing can change. "period not closed": the results are not out yet. "no
#: later 8-K release...": none had been fetched when it was last looked at. "outcome search not run": 04 has not seen the row.
UNRESOLVED_REASONS = ("period not closed", "no later 8-K release", "outcome search not run")


def select_from_review(
    company: Company, rows: list[dict[str, str]], *, as_of: date, ack_days: int
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    """({"outcome": stubs, "ack": stubs, "withdrawal": stubs}, outcomes already on the missed rows chosen for "ack").

    outcome     no outcome yet, for a reason a later filing can change (UNRESOLVED_REASONS).
    ack         those rows too (an outcome found this run may be a miss), and every row with an outcome the rubric calls
                missed, no acknowledgement, and a window (`ack_days` from the outcome) that is still open on `as_of`.
    withdrawal  a period that has not closed on `as_of` and no withdrawal found yet.
    The second value is the outcome of each "ack" row that has one, in the shape build_ack_requests reads."""
    picks: dict[str, list[dict[str, Any]]] = {"outcome": [], "ack": [], "withdrawal": []}
    known: dict[str, dict[str, Any]] = {}
    for row in rows:
        stub = review_step.draft_stub(row)
        if stub is None:
            continue
        a = stub["assumption"]
        if not row.get("outcome.reported_value"):
            if (row.get("aid_outcome_note") or "").startswith(UNRESOLVED_REASONS):
                picks["outcome"].append(stub)
                picks["ack"].append(stub)
        elif not row.get("acknowledged_at") and row.get("outcome.reported_at"):
            reported = date.fromisoformat(row["outcome.reported_at"])
            value = float(row["outcome.reported_value"])
            if reported + timedelta(days=ack_days) >= as_of and rubric.resolve(a["target_low"], a["target_high"], value, reviewed=True) == "missed":
                picks["ack"].append(stub)
                known[stub["draft_id"]] = {"reported_value": value, "reported_at": row["outcome.reported_at"]}
        period = parse_period(a["target_period"])
        close = company.period_end(period[1], period[0]) if period else None
        if close is not None and close > as_of and not row.get("aid_withdrawal_note"):
            picks["withdrawal"].append(stub)
    return picks, known


def _merge_in_place(old: list[dict[str, Any]], new: list[dict[str, Any]], only_ids: set[str]) -> list[dict[str, Any]]:
    """`old` with the rows of `only_ids` replaced by `new`. A row that is still there keeps its place in the file (so a weekly
    diff shows only what changed, not every re-checked row moved to the end); rows that are new go last, rows that are gone drop."""
    by_id: dict[str, list[dict[str, Any]]] = {}
    for r in new:
        by_id.setdefault(r["draft_id"], []).append(r)
    out: list[dict[str, Any]] = []
    for r in old:
        if r["draft_id"] not in only_ids:
            out.append(r)
        elif by_id.get(r["draft_id"]):
            out.append(by_id[r["draft_id"]].pop(0))
    return out + [r for rows in by_id.values() for r in rows]


def _today() -> date:
    return date.today()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Find outcomes, acknowledgements and withdrawals for draft assumptions.")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--company", action="append", metavar="TICKER")
    parser.add_argument("--stage", choices=["outcomes", "ack", "withdrawal", "all"], default="all")
    parser.add_argument("--submit", action="store_true", help="really call the API and write data/outcomes/; without it this is a dry run")
    parser.add_argument("--only", type=Path, metavar="PATH",
                        help="a text file of draft ids, one per line: look at those drafts and nothing else; their rows are "
                             "merged into data/outcomes/ and every other row is left as it is")
    parser.add_argument("--from-review", action="store_true",
                        help="build the drafts from the review CSVs (the unresolved rows, the missed rows with no acknowledgement, the "
                             "open-period rows) plus the new drafts; merge the rows into data/outcomes/")
    parser.add_argument("--accessions", type=Path, metavar="PATH",
                        help="a new_filings.json: search only the filings it lists, and add the drafts of those filings")
    parser.add_argument("--sync", action="store_true", help="send every request one by one, whatever llm.sync_below says")
    parser.add_argument("--max-usd", type=float, metavar="USD", help="ceiling on this run's spend, all stages together")
    parser.add_argument("--force", action="store_true", help="allow an empty result to replace a non-empty data/outcomes/{cik}.jsonl")
    parser.add_argument("--wait-minutes", type=float, default=60)
    args = parser.parse_args(argv)

    config = load_config(args.config)
    cfg, ocfg = llm.LlmConfig.from_config(config["llm"]), config["outcomes"]
    number = candidates_step.Settings.from_config(config["candidates"]).number
    targets = companies(config, args.company)
    by_cik = {c.cik: c for c in targets}
    today = _today()
    as_of = today  # the run date; the manifest's processed_through says what to fetch, not what has closed
    scope = load_accession_scope(args.accessions) if args.accessions else None
    cap = llm.RunCap(args.max_usd) if args.max_usd is not None else None

    drafts = {c.cik: load_drafts(c) for c in targets}
    only_ids = read_id_list(args.only) if args.only else None
    if scope is not None:  # the drafts of the new filings only: an old draft's filing is not in the store, and is not "new"
        drafts = {cik: [d for d in v if d["assumption"]["evidence"]["accession_number"] in scope.get(cik, set())] for cik, v in drafts.items()}
    if only_ids is not None:
        drafts = {cik: [d for d in v if d["draft_id"] in only_ids] for cik, v in drafts.items()}
        found = {d["draft_id"] for v in drafts.values() for d in v}
        print(f"04_outcomes: --only: {len(found):,} of {len(only_ids):,} ids found in the drafts of the selected companies"
              + (f"; not found, skipped: {', '.join(sorted(only_ids - found))}" if only_ids - found else ""))
    stage_drafts = {"outcome": drafts, "ack": drafts, "withdrawal": drafts}
    known_outcomes: dict[str, dict[str, Any]] = {}
    previous: dict[str, dict[str, dict[str, Any]]] = {}
    if args.from_review:
        picked = {"outcome": {}, "ack": {}, "withdrawal": {}}
        for c in targets:
            path = REVIEW_DIR / f"{c.cik}.csv"
            picks, known = select_from_review(c, review_step.read_csv(path) if path.exists() else [], as_of=as_of, ack_days=int(ocfg["ack_days"]))
            known_outcomes.update(known)
            new = {d["draft_id"]: d for d in drafts[c.cik]}  # a draft in data/drafts/ is fresher than its stub
            for stage in picked:
                merged = {s["draft_id"]: s for s in picks[stage]}
                merged.update(new)
                picked[stage][c.cik] = list(merged.values())
            previous[c.cik] = {r["draft_id"]: r for r in read_jsonl(OUTCOMES_DIR / f"{c.cik}.jsonl")}
        stage_drafts = picked
        drafts = {c.cik: list({d["draft_id"]: d for st in picked for d in picked[st][c.cik]}.values()) for c in targets}
        only_ids = {d["draft_id"] for v in drafts.values() for d in v}
    stores = {c.cik: DocStore(c, scope.get(c.cik, set()) if scope is not None else None) for c in targets}
    print(f"04_outcomes: {sum(len(v) for v in drafts.values()):,} drafts to look at")

    client, ledger = llm.make_client(), llm.Ledger()
    if args.sync:
        cfg = dataclasses.replace(cfg, sync_below=sys.maxsize)
    requests, o_index, reasons = build_outcome_requests(stage_drafts["outcome"], stores, ocfg, number, today=today, as_of=as_of)
    print(f"  {len(requests):,} drafts have a matching later release with candidate lines; "
          f"{len(reasons):,} have no request, with a reason recorded")

    rejects: list[dict[str, Any]] = []

    def run(stage: str, step: str, reqs: list[llm.LlmRequest], check) -> dict[str, llm.Result]:
        if args.stage in (stage, "all"):
            return _stage(client, cfg, ledger, step, reqs, submit=args.submit, wait_minutes=args.wait_minutes, check=check,
                          force_sync=args.sync, cap=cap)
        return llm.current_results(llm.RawArchive(step).load(), reqs, cfg.model)

    try:
        results = run("outcomes", STEP_OUTCOMES, requests, _json_check("index", "value", "unit"))
        outcomes, o_rejects, o_reasons = derive_outcomes(o_index, results, stores, ocfg)
        rejects += o_rejects
        reasons.update(o_reasons)

        ack_requests, k_index = build_ack_requests(stage_drafts["ack"], {**known_outcomes, **outcomes}, stores, ocfg, number, today=today)
        if args.stage in ("ack", "all"):
            print(f"  {len(outcomes):,} outcomes found, {len(ack_requests):,} of them misses with candidate acknowledgement lines")
        k_results = run("ack", STEP_ACK, ack_requests, _json_check("indices"))
        acks, k_rejects = derive_acknowledgements(k_index, k_results, stores)
        rejects += k_rejects

        w_requests, w_index = build_withdrawal_requests(stage_drafts["withdrawal"], by_cik, stores, ocfg, today=today)
        if args.stage in ("withdrawal", "all"):
            print(f"  {len(w_requests):,} drafts have a candidate withdrawal sentence in an 8-K exhibit before their period closed")
        w_results = run("withdrawal", STEP_WITHDRAWAL, w_requests, _json_check("indices"))
        withdrawals, w_rejects = derive_withdrawals(w_index, w_results, stores)
        rejects += w_rejects
    except Stopped as stop:
        return stop.code

    plan: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}
    for company in targets:
        rows = []
        for d in drafts[company.cik]:
            did = d["draft_id"]
            row = {
                "draft_id": did,
                "outcome": outcomes.get(did),
                "outcome_reason": None if did in outcomes else reasons.get(did, "outcome search not run"),
                "acknowledgement": acks.get(did),
                "withdrawal": withdrawals.get(did),
            }
            if args.from_review and did in previous[company.cik]:  # keep what the row had; this run adds, it never takes away
                old = previous[company.cik][did]
                row["outcome"] = row["outcome"] or old.get("outcome")
                row["outcome_reason"] = None if row["outcome"] else (reasons[did] if did in reasons else old.get("outcome_reason"))
                row["acknowledgement"] = row["acknowledgement"] or old.get("acknowledgement")
                row["withdrawal"] = row["withdrawal"] or old.get("withdrawal")
            rows.append(row)
        mine = [r for r in rejects if any(r["draft_id"] == d["draft_id"] for d in drafts[company.cik])]
        out_rows, out_rejects = rows, mine
        if only_ids is not None:  # merge: the other drafts' rows and rejects stay exactly as they are
            out_rows = _merge_in_place(read_jsonl(OUTCOMES_DIR / f"{company.cik}.jsonl"), rows, only_ids)
            out_rejects = _merge_in_place(read_jsonl(OUTCOMES_DIR / f"{company.cik}.rejects.jsonl"), mine, only_ids)
        plan[company.cik] = (out_rows, out_rejects)
        print(f"  {company.ticker}: {len(rows):,} drafts, {sum(1 for r in rows if r['outcome'])} with an outcome, "
              f"{sum(1 for r in rows if r['acknowledgement'])} acknowledged, {sum(1 for r in rows if r['withdrawal'])} withdrawn")

    if args.submit:  # a dry run never writes to data/outcomes/: with stale or missing answers it would empty what is there
        refused = [c.ticker for c in targets if not plan[c.cik][0] and read_jsonl(OUTCOMES_DIR / f"{c.cik}.jsonl")]
        if refused and not args.force:
            print(f"REFUSED: the result for {', '.join(refused)} is empty and data/outcomes/ already holds rows for "
                  f"{'it' if len(refused) == 1 else 'them'}. Writing it would empty the file. Nothing was written. This usually means "
                  "data/drafts/ is missing (a fresh checkout): use --from-review or --only. If an empty result is what you want, add --force.",
                  file=sys.stderr)
            return 6
        for company in targets:
            out_rows, out_rejects = plan[company.cik]
            write_jsonl(OUTCOMES_DIR / f"{company.cik}.jsonl", out_rows)
            write_jsonl(OUTCOMES_DIR / f"{company.cik}.rejects.jsonl", out_rejects)
    if not args.submit:
        print("dry run: nothing was submitted, and nothing was written to data/outcomes/. Add --submit to run the batches.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
