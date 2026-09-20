"""Phase 1, step 2: candidate sentences from the cached filings.

    python -m pipeline.02_candidates [--config PATH] [--company TICKER ...]

Two ways in, recorded on every candidate as `capture_method`:

  sentence  a sentence with at least one forward term AND at least one number
            with a unit (both lists live in config.yaml). A recall pass: it keeps
            history like "revenue for fiscal 2024 was $60.9 billion" and lets the
            Phase 2 model discard it.
  section   8-K exhibits only. The whole block under a heading such as "Q4 FY26
            Guidance" or "Outlook", from the line after the heading to the next heading,
            section break or `section_max_lines` lines, as ONE candidate. Tables put the
            label and the number on separate lines, so a bare value line such as
            "$915 million" means nothing alone; the block is what makes it readable.
            `sentence` holds the block's lines joined by newlines, and `block_lines` and
            `block_end` say how long it is and why it stopped. Sentence candidates inside
            a block are still their own candidates. A block also carries `table_header`:
            the header row that names the period of each column, found above it (see
            `find_table_header`), or null. It is context for the model, not part of `sentence`.

Every candidate also carries its context: the nearest preceding lead-in line (a
line ending in a colon), the heading it sits under (8-K only), and the sentences
either side. NVIDIA's "outlook for the third quarter of fiscal 2027 is as
follows:" is where the period lives for the bullets beneath it.

Reads   data/raw/{cik}/*.meta.json and the .html beside each
Writes  data/candidates/{cik}.jsonl, one candidate per line, rewritten each run

`char_start` and `char_end` index into research_record.text.html_to_text of the
cached file, so text[char_start:char_end] == sentence. `text_version` records
which version of that extractor produced them.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from pipeline.common import (
    CANDIDATES_DIR,
    CONFIG_PATH,
    RAW_DIR,
    Company,
    companies,
    load_config,
    read_meta,
)
from research_record.text import TEXT_VERSION, html_to_text_and_gaps, sentence_spans

_NUMBER = r"\d[\d,]*(?:\.\d+)?"
_SENTENCE_END = (".", "!", "?")


@dataclass(frozen=True)
class Settings:
    forward: re.Pattern[str]
    number: re.Pattern[str]
    heading: re.Pattern[str]
    end_heading: re.Pattern[str]
    max_chars: int
    section_max_lines: int
    section_gap_lines: int
    section_heading_gap: int
    heading_max_words: int
    lead_in_lookback: int
    lead_in_max_chars: int
    table_header_lookback: int
    table_header_max_words: int
    context_sentences: int
    context_max_chars: int

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "Settings":
        forward, number = compile_matchers(cfg)
        return cls(
            forward=forward,
            number=number,
            heading=re.compile(cfg["section_heading"], re.IGNORECASE),
            end_heading=re.compile("|".join(f"(?:{p})" for p in cfg["section_end_headings"]), re.IGNORECASE),
            max_chars=int(cfg["max_sentence_chars"]),
            section_max_lines=int(cfg["section_max_lines"]),
            section_gap_lines=int(cfg["section_gap_lines"]),
            section_heading_gap=int(cfg["section_heading_gap"]),
            heading_max_words=int(cfg["heading_max_words"]),
            lead_in_lookback=int(cfg["lead_in_lookback_lines"]),
            lead_in_max_chars=int(cfg["lead_in_max_chars"]),
            table_header_lookback=int(cfg["table_header_lookback_lines"]),
            table_header_max_words=int(cfg["table_header_max_words"]),
            context_sentences=int(cfg["context_sentences"]),
            context_max_chars=int(cfg["context_max_chars"]),
        )


def compile_matchers(cfg: dict[str, Any]) -> tuple[re.Pattern[str], re.Pattern[str]]:
    """(forward term matcher, number-with-unit matcher), both case-insensitive."""
    terms = "|".join(f"(?:{t})" for t in cfg["forward_terms"])
    units = "|".join(f"(?:{u})" for u in cfg["number_units"])
    forward = re.compile(rf"(?<![A-Za-z0-9])(?:{terms})(?![A-Za-z0-9])", re.IGNORECASE)
    number = re.compile(
        rf"[$\u20ac\u00a3]\s?{_NUMBER}"  # a currency amount
        rf"|{_NUMBER}\s?%"  # a percentage
        rf"|{_NUMBER}\s?(?:{units})(?![A-Za-z])",  # a number then a unit word
        re.IGNORECASE,
    )
    return forward, number


def is_candidate(sentence: str, forward: re.Pattern[str], number: re.Pattern[str]) -> bool:
    return bool(forward.search(sentence) and number.search(sentence))


def is_heading(line: str, settings: Settings) -> bool:
    """A short line that matches the section pattern and does not read as a sentence."""
    words = line.split()
    return (
        0 < len(words) <= settings.heading_max_words
        and not line.endswith(_SENTENCE_END)
        and settings.heading.search(line) is not None
    )


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def _lead_in(lines: list[str], line_index: int, settings: Settings) -> str | None:
    """Nearest line above this one that ends in a colon. The candidate's own line never counts."""
    stop = max(-1, line_index - 1 - settings.lead_in_lookback)
    for j in range(line_index - 1, stop, -1):
        if lines[j].endswith(":") and len(lines[j]) <= settings.lead_in_max_chars:
            return lines[j]
    return None


# One period as a column header names it: a quarter ("Q2 2023", "Q4 FY21", "2Q23"), a full year
# ("Full Year 2023", "FY26", "Fiscal 2025") or a spelled-out quarter with its year. The year is part
# of the label, so "for the second quarter" in a sentence is not one.
_PERIOD_LABEL = re.compile(
    r"\b(?:Q(?P<q>[1-4])|(?P<q2>[1-4])Q)\s*(?:FY\s*)?'?(?P<y>(?:20)?\d\d)\b"
    r"|\b(?:Full[- ]Year|FY|Fiscal(?:\s+Year)?)\s*(?:FY\s*)?'?(?P<fy>(?:20)?\d\d)\b"
    r"|\b(?P<w>first|second|third|fourth)\s+quarter\s+(?:of\s+)?(?:fiscal\s+)?(?P<wy>(?:20)?\d\d)\b",
    re.IGNORECASE,
)
_QUARTER_WORDS = {"first": 1, "second": 2, "third": 3, "fourth": 4}
# A line that compares periods or shows a rate is the header of a results table, not of a guidance table.
_NOT_A_HEADER = re.compile(r"Y/Y|\bvs\b|\bchange\b|%", re.IGNORECASE)


def parse_periods(line: str) -> list[tuple[int | None, int]]:
    """(quarter or None, four-digit year) for each period the line names, in order."""
    periods: list[tuple[int | None, int]] = []
    for m in _PERIOD_LABEL.finditer(line):
        if m["y"]:
            quarter, year = int(m["q"] or m["q2"]), int(m["y"])
        elif m["fy"]:
            quarter, year = None, int(m["fy"])
        else:
            quarter, year = _QUARTER_WORDS[m["w"].lower()], int(m["wy"])
        periods.append((quarter, year + 2000 if year < 100 else year))
    return periods


def period_labels(line: str) -> int:
    return len(parse_periods(line))


# What may sit beside a period label in a header cell: "Q1 FY22 Guidance", "Previous Q4 Fiscal 2019 Guidance". A press
# release title, a bullet, a URL or "FY25 Results" has other words in it, and is not a cell of a table's header.
_HEADER_FILLER = frozenset(
    "guidance outlook forecast previous updated prior revised initial current estimated full year fiscal quarter q1 q2 q3 q4 fy".split()
)
_WORD = re.compile(r"[A-Za-z0-9']+")


def _header_cell(line: str, settings: Settings) -> bool:
    """A short line that does not end like a sentence, does not compare periods or show a rate, and is nothing
    but period labels and words like "Guidance"."""
    if (
        len(line.split()) > settings.table_header_max_words
        or line.endswith(_SENTENCE_END + (":",))
        or _NOT_A_HEADER.search(line) is not None
    ):
        return False
    return all(word.lower() in _HEADER_FILLER for word in _WORD.findall(_PERIOD_LABEL.sub(" ", line)))


def _already_over(periods: list[tuple[int | None, int]], filed_at: date | None, company: Company | None) -> bool:
    """True when every period ended before the filing date. A guidance table names periods still to come, so a
    header that names only finished ones belongs to a table of results. Unknown without a fiscal calendar."""
    if filed_at is None or company is None or company.fiscal_year_end_month is None or not periods:
        return False
    return all((company.period_end(year, quarter) or filed_at) < filed_at for quarter, year in periods)


def find_table_header(
    lines: list[str], block_start: int, settings: Settings, filed_at: date | None = None, company: Company | None = None
) -> str | None:
    """The header row of the table a block's figures sit in, searched within `table_header_lookback` lines above the block.

    1. The nearest line that names two or more periods: "Q2 2023 Full Year 2023" above "$1.30 - $1.70 $7.75 - $8.75".
    2. Failing that, a table whose header cells were broken onto separate lines (Salesforce: "Q4 FY21", "Guidance",
       "Full Year FY21", ...): the lines in the window that name a period, in order, joined with " | ", if together
       they name two or more.

    Checks keep out the header of a table of results, and things that only mention a period. A cell must be short,
    must not end like a sentence, and must be nothing but period labels and words like "Guidance" (so a title, a
    bullet or a URL is not one). A line that says Y/Y, vs, change or shows a % is never part of a header. And a
    header that names only periods already over on the filing date is rejected (see `_already_over`).
    """
    window = range(block_start - 1, max(-1, block_start - 1 - settings.table_header_lookback), -1)
    for j in window:
        periods = parse_periods(lines[j])
        if len(periods) >= 2 and _header_cell(lines[j], settings) and not _already_over(periods, filed_at, company):
            return lines[j]
    cells = sorted(j for j in window if parse_periods(lines[j]) and _header_cell(lines[j], settings))
    periods = [p for j in cells for p in parse_periods(lines[j])]
    if len(periods) >= 2 and not _already_over(periods, filed_at, company):
        return " | ".join(lines[j] for j in cells)
    return None


_VALUE_START = re.compile(r"[~$\d]|\(\$|(?:approximately|approx\.?|slightly|about|n/?a)\b", re.IGNORECASE)


def _is_value(line: str, settings: Settings) -> bool:
    """A table value ("$11.13 - $11.23 billion", "Approximately 15%"), not prose that happens to hold a figure."""
    if _VALUE_START.match(line):
        return True
    return bool(settings.number.search(line)) and len(line.split()) <= 6 and not line.endswith(_SENTENCE_END)


def _is_title_like(line: str, next_line: str, settings: Settings) -> bool:
    """A short, unpunctuated line that starts something new. A table label is not one: it is followed by a value."""
    return (
        0 < len(line.split()) <= settings.heading_max_words
        and not line.endswith(_SENTENCE_END + (":",))
        and not _is_value(line, settings)
        and not _is_value(next_line, settings)
    )


@dataclass(frozen=True)
class Block:
    heading: int  # line index of the heading
    start: int  # first line of the block
    end: int  # one past its last line
    reason: str  # why it stopped: heading, end-heading, gap, title, max or eof


def find_blocks(lines: list[str], gaps: list[int], settings: Settings) -> list[Block]:
    """The block under every heading. `gaps[i]` is the number of blank lines that came before line i."""
    blocks: list[Block] = []
    for h, line in enumerate(lines):
        if not is_heading(line, settings):
            continue
        end, reason = len(lines), "eof"
        for j in range(h + 1, len(lines)):
            following = lines[j + 1] if j + 1 < len(lines) else ""
            if j - h - 1 >= settings.section_max_lines:
                end, reason = j, "max"
            elif is_heading(lines[j], settings):
                end, reason = j, "heading"
            elif settings.end_heading.fullmatch(lines[j]):
                end, reason = j, "end-heading"
            elif gaps[j] >= settings.section_gap_lines:
                end, reason = j, "gap"
            elif gaps[j] >= settings.section_heading_gap and _is_title_like(lines[j], following, settings):
                end, reason = j, "title"
            else:
                continue
            break
        if end > h + 1:
            blocks.append(Block(h, h + 1, end, reason))
    return blocks


def cached_documents(company: Company) -> list[tuple[dict[str, Any], Path]]:
    """(meta, html path) for every intact cached document, oldest filing first."""
    folder = RAW_DIR / company.cik
    found = []
    for meta_path in folder.glob("*.meta.json"):
        meta = read_meta(meta_path)
        if not meta or meta.get("http_status") != 200:
            continue
        found.append((meta, folder / f"{meta['accession']}.html"))
    return sorted(found, key=lambda pair: (pair[0]["filed_at"], pair[0]["accession"]))


def candidates_for_document(
    meta: dict[str, Any], content: bytes, settings: Settings, company: Company | None = None
) -> tuple[list[dict[str, Any]], int]:
    """Candidate rows for one document, and how many matches were dropped for length.
    `company` supplies the fiscal calendar for dating a table header; without one that check is skipped."""
    text, gaps = html_to_text_and_gaps(content)
    lines = text.split("\n")
    line_starts: list[int] = []
    offset = 0
    for line in lines:
        line_starts.append(offset)
        offset += len(line) + 1
    spans = sentence_spans(text)
    span_starts = [a for a, _ in spans]
    is_8k = meta["filing_type"] == "8-K"
    blocks = find_blocks(lines, gaps, settings) if is_8k else []
    heading_of_line: dict[int, str] = {i: lines[b.heading] for b in blocks for i in range(b.start, b.end)}
    n = settings.context_sentences

    def context(k_before: int, k_after: int) -> tuple[list[str], list[str]]:
        clip = lambda a, b: _clip(text[a:b], settings.context_max_chars)
        return (
            [clip(a, b) for a, b in spans[max(0, k_before - n) : k_before]],
            [clip(a, b) for a, b in spans[k_after : k_after + n]],
        )

    def row(start: int, end: int, method: str, heading: str | None, lead_in: str | None, before, after, block: Block | None,
            table_header: str | None = None) -> dict[str, Any]:
        return {
            "cik": meta["cik"],
            "accession": meta["accession"],
            "filing_type": meta["filing_type"],
            "filed_at": meta["filed_at"],
            "char_start": start,
            "char_end": end,
            "sentence": text[start:end],
            "text_version": TEXT_VERSION,
            "capture_method": method,
            "heading": heading,
            "lead_in": lead_in,
            "context_before": before,
            "context_after": after,
            "block_lines": block.end - block.start if block else None,
            "block_end": block.reason if block else None,
            "table_header": table_header,
        }

    rows: list[dict[str, Any]] = []
    too_long = 0
    for k, (start, end) in enumerate(spans):
        sentence = text[start:end]
        if not is_candidate(sentence, settings.forward, settings.number):
            continue
        if len(sentence) > settings.max_chars:
            too_long += 1
            continue
        li = bisect.bisect_right(line_starts, start) - 1
        before, after = context(k, k + 1)
        rows.append(row(start, end, "sentence", heading_of_line.get(li), _lead_in(lines, li, settings), before, after, None))

    for block in blocks:
        if not any(settings.number.search(lines[i]) for i in range(block.start, block.end)):
            continue  # a block with no figure in it has nothing to read
        start = line_starts[block.start]
        end = line_starts[block.end - 1] + len(lines[block.end - 1])
        before, after = context(bisect.bisect_left(span_starts, line_starts[block.heading]), bisect.bisect_left(span_starts, end))
        rows.append(row(start, end, "section", lines[block.heading], _lead_in(lines, block.heading, settings), before, after, block,
                        find_table_header(lines, block.start, settings, date.fromisoformat(meta["filed_at"]), company)))

    rows.sort(key=lambda r: (r["char_start"], r["capture_method"] != "sentence"))
    return rows, too_long


def build_company(
    company: Company, cfg: dict[str, Any], out_dir: Path = CANDIDATES_DIR
) -> Counter:
    settings = Settings.from_config(cfg)
    stats: Counter = Counter()
    rows: list[dict[str, Any]] = []
    silent: list[str] = []
    if company.fiscal_year_end_month is None:
        print(f"  {company.ticker} has no fiscal_year_end_month in config.yaml: table headers are not checked against the filing date")

    for meta, html_path in cached_documents(company):
        try:
            content = html_path.read_bytes()
        except FileNotFoundError:
            stats["missing"] += 1
            continue
        if hashlib.sha256(content).hexdigest() != meta["content_sha256"]:
            stats["hash_mismatch"] += 1
            print(f"  {meta['accession']}: cached bytes do not match the sidecar hash, skipped", file=sys.stderr)
            continue
        doc_rows, too_long = candidates_for_document(meta, content, settings, company)
        rows.extend(doc_rows)
        stats["documents"] += 1
        stats["dropped_too_long"] += too_long
        for row in doc_rows:
            stats[f"candidates {row['filing_type']} {row['capture_method']}"] += 1
            stats["with_lead_in"] += row["lead_in"] is not None
            stats["with_table_header"] += row["table_header"] is not None
            if row["block_end"]:
                stats[f"blocks stopped by {row['block_end']}"] += 1
        if not doc_rows and meta["filing_type"] == "8-K":
            silent.append(meta["accession"])
    stats["candidates"] = len(rows)
    stats["silent_8k_documents"] = len(silent)

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{company.cik}.jsonl"
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    os.replace(tmp, out)
    if silent:
        print(f"  8-K documents with no candidate: {', '.join(silent)}")
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract candidate guidance sentences from data/raw/.")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--company", action="append", metavar="TICKER", help="limit to this ticker; repeatable")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    targets = companies(config, args.company)
    if not targets:
        print("No companies to process. Check `companies:` in config.yaml.", file=sys.stderr)
        return 2
    for company in targets:
        print(f"== {company.ticker} {company.name} ({company.cik})")
        stats = build_company(company, config["candidates"])
        print("   " + "  ".join(f"{k}={v}" for k, v in sorted(stats.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
