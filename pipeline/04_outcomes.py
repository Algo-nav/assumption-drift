"""Phase 2, step 2: what the company later reported, and whether it ever admitted a miss.

    python -m pipeline.04_outcomes [--company TICKER ...] [--stage outcomes|ack|all]
                                   [--submit] [--wait-minutes M]

A dry run unless --submit is given. Two stages, each a Haiku batch behind the same
cost gate as 03_structure (one budget across all of them):

  outcomes  For each draft, find the company's later 8-K earnings releases whose
            headline names the draft's period, pick the lines that name the metric
            next to a number, and ask the model which line states the ACTUAL result.
  ack       For each draft whose outcome resolves to "missed" by the rubric, find later
            lines that mention the metric next to words like "below" or "short of",
            and ask the model whether one admits the shortfall. The earliest wins.

Only 8-K exhibits are searched, in both stages. That is a scope choice: acknowledgements
in a later 10-Q or 10-K are not looked for, so "never acknowledged" is an upper bound.

Nothing the model returns is trusted blindly. Values are converted between units by
code, a value wildly off the guidance is rejected as a unit or row mix-up, and every
outcome carries the verbatim line it came from. "No outcome" always has a reason.

Reads   data/drafts/{cik}.jsonl, data/raw/{cik}/
Writes  data/outcomes/{cik}.jsonl          one row per draft: outcome, acknowledgement, reasons
        data/outcomes/{cik}.rejects.jsonl  model answers that failed a check, with the reason
        data/batches/04_*                  raw model output and pending batch state
"""

from __future__ import annotations

import argparse
import bisect
import importlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from pipeline import common, llm
from pipeline.common import CONFIG_PATH, DRAFTS_DIR, OUTCOMES_DIR, Company, companies, load_config
from research_record import rubric
from research_record.schema import Evidence, Outcome
from research_record.text import html_to_text, sentence_spans

candidates_step = importlib.import_module("pipeline.02_candidates")
structure_step = importlib.import_module("pipeline.03_structure")

STEP_OUTCOMES = "04_outcomes"
STEP_ACK = "04_acknowledgements"
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
You decide whether a company admitted that it fell short of its own guidance.

You are given the guidance (metric, period, the range the company guided to, and what it actually reported) and a numbered list of lines from the company's later filings, oldest first.

Return the numbers of the lines that say, in the company's own words, that its actual result for exactly that metric and exactly that period was below, short of, lower than, or otherwise missed what it had guided or expected. Return an empty list if none do.

Rules
- The line must be about the same metric and the same period. A miss on another metric or another period does not count.
- It must reference the shortfall against guidance or expectations. A line that only states the result, or only gives a new outlook, does not count. A line that says guidance was revised because the result came in lower does.
- Do not count a line that says results were above or in line with guidance.
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


def title_names_period(title: str, head: str, quarter: int | None, year: int) -> bool:
    """Is this headline a results release for the period?

    The quarter must be in the headline; fiscal-year results ride with the fourth quarter.
    A headline that names a year must name this one. One that names none (Target's interim
    releases say only "Reports Second Quarter Earnings") is checked against the start of the
    release. The period is never taken from the body alone: Salesforce opens with guidance
    bullets, so a Q3 release names "fourth quarter FY26" without reporting it.
    """
    if _NOT_RESULTS.search(title) or not _quarter_regex(4 if quarter is None else quarter).search(title):
        return False
    named = {int(y) for y in re.findall(r"\b(20\d\d)\b", title)} | {2000 + int(y) for y in re.findall(r"\bFY ?(\d\d)\b", title, re.IGNORECASE)}
    return year in named if named else bool(_year_regex(year).search(head))


def release_names_period(doc: "DocText", period: tuple[int | None, int], head_chars: int) -> bool:
    title = structure_step.document_title(doc.text)
    return bool(title) and title_names_period(title, doc.text[:head_chars], *period)


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

    def line_of(self, char_start: int) -> int:
        return bisect.bisect_right(self.line_starts, char_start) - 1


class DocStore:
    """One company's cached 8-K exhibits, read and split once."""

    def __init__(self, company: Company) -> None:
        folder = common.RAW_DIR / company.cik
        metas = [common.read_meta(p) for p in folder.glob("*.meta.json")] if folder.exists() else []
        self.folder = folder
        self.metas = sorted(
            (m for m in metas if m and m.get("http_status") == 200 and m["filing_type"] == "8-K"),
            key=lambda m: (m["filed_at"], m["accession"]),
        )
        self._docs: dict[str, DocText] = {}

    def get(self, meta: dict[str, Any]) -> DocText:
        acc = meta["accession"]
        if acc not in self._docs:
            text = html_to_text((self.folder / f"{acc}.html").read_bytes())
            lines = text.split("\n")
            starts, offset = [], 0
            for line in lines:
                starts.append(offset)
                offset += len(line) + 1
            self._docs[acc] = DocText(meta, text, lines, starts, sentence_spans(text))
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


def select_lines(
    store: DocStore,
    metas: list[dict[str, Any]],
    matcher: re.Pattern[str],
    extra: re.Pattern[str] | None,
    metric: str,
    period: tuple[int | None, int] | None,
    cfg: dict[str, Any],
    number: re.Pattern[str],
) -> list[Line]:
    """Lines in these releases that name the metric next to a number (and `extra`, if given). Best first, then in order."""
    wants_non_gaap = "non-gaap" in metric
    wants_gaap = "gaap" in metric and not wants_non_gaap
    scored: list[tuple[int, int, Line]] = []
    order = 0
    for meta in metas:
        doc = store.get(meta)
        for k, (start, end) in enumerate(doc.spans):
            sentence = doc.text[start:end]
            if len(sentence) > EXCERPT_LIMIT or not number.search(sentence) or not matcher.search(sentence):
                continue
            if extra is not None and not extra.search(sentence):
                continue
            li = doc.line_of(start)
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
                section=section, units_note=_units_note(doc, li, int(cfg["units_lookback_lines"])), score=score)))
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


# --- stage 1: outcomes -----------------------------------------------------


def load_drafts(company: Company) -> list[dict[str, Any]]:
    path = DRAFTS_DIR / f"{company.cik}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def build_outcome_requests(
    drafts_by_company: dict[str, list[dict[str, Any]]],
    stores: dict[str, DocStore],
    cfg: dict[str, Any],
    number: re.Pattern[str],
    *,
    today: date,
) -> tuple[list[llm.LlmRequest], dict[str, tuple[dict[str, Any], list[Line]]], dict[str, str]]:
    """(requests, custom_id -> (draft, lines), draft_id -> why there is no request)."""
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
            named = [m for m in later if release_names_period(store.get(m), period, int(cfg["head_chars"]))]
            if not named:
                reasons[d["draft_id"]] = "no later 8-K release for that period found"
                continue
            lines = select_lines(store, named[: int(cfg["max_docs"])], matcher, None, a["metric"], period, cfg, number)
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
            a = d["assumption"]
            value = convert(float(payload["value"]), payload["unit"], a["unit"])
            mid = [x for x in (a["target_low"], a["target_high"]) if x is not None]
            centre = sum(mid) / len(mid)
            if centre > 0 and value > 0 and not (1 / ratio <= value / centre <= ratio):
                raise ValueError(f"reported {value:g} is {value / centre:.1f}x the guidance {centre:g}: likely a unit or row mix-up")
            meta = next(m for m in stores[d["cik"]].metas if m["accession"] == line.accession)
            outcome = Outcome(reported_value=value, reported_at=date.fromisoformat(line.filed_at), evidence=_evidence(meta, line.sentence))
        except (ValueError, TypeError, StopIteration, ValidationError) as exc:
            rejects.append({"draft_id": d["draft_id"], "output": payload, "reason": str(exc)[:300]})
            reasons[d["draft_id"]] = "model output rejected"
            continue
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
    phrases = re.compile("|".join(cfg["acknowledgement_phrases"]), re.IGNORECASE)
    requests: list[llm.LlmRequest] = []
    index: dict[str, tuple[dict[str, Any], list[Line]]] = {}
    for cik, drafts in drafts_by_company.items():
        store = stores[cik]
        for d in drafts:
            outcome = outcomes.get(d["draft_id"])
            if outcome is None or not missed(d, outcome):
                continue
            a = d["assumption"]
            matcher = metric_pattern(a["metric"], cfg["metric_terms"])
            if matcher is None:
                continue
            reported = outcome["reported_at"]
            horizon = min(today, date.fromisoformat(reported) + timedelta(days=int(cfg["ack_days"]))).isoformat()
            metas = [m for m in store.metas if reported <= m["filed_at"] <= horizon]
            lines = select_lines(store, metas, matcher, phrases, a["metric"], parse_period(a["target_period"]), cfg, number)
            if not lines:
                continue
            user = (
                f"Guidance: {d['company']} guided {a['metric']} for {a['target_period']} to {_range_text(a)} {a['unit']}.\n"
                f"Actually reported: {outcome['reported_value']:g} {a['unit']}, on {reported}.\n\n"
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
            meta = next(m for m in stores[d["cik"]].metas if m["accession"] == line.accession)
            found[d["draft_id"]] = {
                "acknowledged_at": line.filed_at,
                "evidence": _evidence(meta, line.sentence).model_dump(mode="json"),
            }
        except (StopIteration, ValidationError) as exc:
            rejects.append({"draft_id": d["draft_id"], "reason": str(exc)[:300]})
    return found, rejects


# --- running ---------------------------------------------------------------


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


def _stage(client, cfg, ledger, step, requests, *, submit: bool, wait_minutes: float, check) -> dict[str, llm.Result] | None:
    """Project, print, and (with --submit) run one stage. None means it stopped."""
    archive = llm.RawArchive(step)
    have = archive.load()
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
        return None
    try:
        outcome = llm.run_batch(client, cfg, step, todo, projection=projection, canary_check=check,
                                ledger=ledger, archive=archive, wait_seconds=wait_minutes * 60)
    except llm.BudgetExceeded as exc:
        print(f"STOPPED: {exc}", file=sys.stderr)
        return None
    for note in outcome.notes:
        print(f"note: {note}")
    return outcome.results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Find outcomes and acknowledgements for draft assumptions.")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--company", action="append", metavar="TICKER")
    parser.add_argument("--stage", choices=["outcomes", "ack", "all"], default="all")
    parser.add_argument("--submit", action="store_true", help="really call the API; without it this is a dry run")
    parser.add_argument("--wait-minutes", type=float, default=60)
    args = parser.parse_args(argv)

    config = load_config(args.config)
    cfg, ocfg = llm.LlmConfig.from_config(config["llm"]), config["outcomes"]
    number = candidates_step.Settings.from_config(config["candidates"]).number
    targets = companies(config, args.company)
    drafts = {c.cik: load_drafts(c) for c in targets}
    stores = {c.cik: DocStore(c) for c in targets}
    today = date.today()
    print(f"04_outcomes: {sum(len(v) for v in drafts.values()):,} drafts to look at")

    client, ledger = llm.make_client(), llm.Ledger()
    requests, o_index, reasons = build_outcome_requests(drafts, stores, ocfg, number, today=today)
    print(f"  {len(requests):,} drafts have a matching later release with candidate lines; "
          f"{len(reasons):,} have no request, with a reason recorded")

    outcomes: dict[str, dict[str, Any]] = {}
    rejects: list[dict[str, Any]] = []
    acks: dict[str, dict[str, Any]] = {}
    if args.stage in ("outcomes", "all"):
        results = _stage(client, cfg, ledger, STEP_OUTCOMES, requests, submit=args.submit,
                         wait_minutes=args.wait_minutes, check=_json_check("index", "value", "unit"))
        if results is None:
            return 3
    else:
        results = llm.RawArchive(STEP_OUTCOMES).load()
    outcomes, o_rejects, o_reasons = derive_outcomes(o_index, results, stores, ocfg)
    rejects += o_rejects
    reasons.update(o_reasons)

    ack_requests, k_index = build_ack_requests(drafts, outcomes, stores, ocfg, number, today=today)
    if args.stage in ("ack", "all"):
        print(f"  {len(outcomes):,} outcomes found, {len(ack_requests):,} of them misses with candidate acknowledgement lines")
        k_results = _stage(client, cfg, ledger, STEP_ACK, ack_requests, submit=args.submit,
                           wait_minutes=args.wait_minutes, check=_json_check("indices"))
        if k_results is None:
            return 3
    else:
        k_results = llm.RawArchive(STEP_ACK).load()
    acks, k_rejects = derive_acknowledgements(k_index, k_results, stores)
    rejects += k_rejects

    if not args.submit:
        print("dry run: nothing was submitted. Add --submit to run the batches.")
    for company in targets:
        rows = []
        for d in drafts[company.cik]:
            did = d["draft_id"]
            rows.append({
                "draft_id": did,
                "outcome": outcomes.get(did),
                "outcome_reason": None if did in outcomes else reasons.get(did, "outcome search not run"),
                "acknowledgement": acks.get(did),
            })
        write_jsonl(OUTCOMES_DIR / f"{company.cik}.jsonl", rows)
        write_jsonl(OUTCOMES_DIR / f"{company.cik}.rejects.jsonl", [r for r in rejects if any(r["draft_id"] == d["draft_id"] for d in drafts[company.cik])])
        print(f"  {company.ticker}: {len(rows):,} drafts, {sum(1 for r in rows if r['outcome'])} with an outcome, "
              f"{sum(1 for r in rows if r['acknowledgement'])} acknowledged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
