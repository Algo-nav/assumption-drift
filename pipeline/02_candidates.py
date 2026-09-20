"""Phase 1, step 2: candidate sentences from the cached filings.

    python -m pipeline.02_candidates [--config PATH] [--company TICKER ...]

Two ways in, recorded on every candidate as `capture_method`:

  sentence  a sentence with at least one forward term AND at least one number
            with a unit (both lists live in config.yaml). A recall pass: it keeps
            history like "revenue for fiscal 2024 was $60.9 billion" and lets the
            Phase 2 model discard it.
  section   8-K exhibits only. A number line within `section_lines` lines under a
            heading such as "Q4 FY26 Guidance" or "Outlook". Tables put the label
            and the number on separate lines, so no single sentence qualifies.
            A line that also qualifies as a sentence stays "sentence".

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
from research_record.text import TEXT_VERSION, html_to_text, sentence_spans

_NUMBER = r"\d[\d,]*(?:\.\d+)?"
_SENTENCE_END = (".", "!", "?")


@dataclass(frozen=True)
class Settings:
    forward: re.Pattern[str]
    number: re.Pattern[str]
    heading: re.Pattern[str]
    max_chars: int
    section_lines: int
    heading_max_words: int
    lead_in_lookback: int
    lead_in_max_chars: int
    context_sentences: int
    context_max_chars: int

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "Settings":
        forward, number = compile_matchers(cfg)
        return cls(
            forward=forward,
            number=number,
            heading=re.compile(cfg["section_heading"], re.IGNORECASE),
            max_chars=int(cfg["max_sentence_chars"]),
            section_lines=int(cfg["section_lines"]),
            heading_max_words=int(cfg["heading_max_words"]),
            lead_in_lookback=int(cfg["lead_in_lookback_lines"]),
            lead_in_max_chars=int(cfg["lead_in_max_chars"]),
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


def _headings_above(lines: list[str], settings: Settings) -> list[str | None]:
    """For each line, the nearest heading at most `section_lines` lines above it, else None."""
    above: list[str | None] = []
    heading_at: int | None = None
    for i, line in enumerate(lines):
        if heading_at is not None and 0 < i - heading_at <= settings.section_lines:
            above.append(lines[heading_at])
        else:
            above.append(None)
        if is_heading(line, settings):
            heading_at = i
    return above


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
    meta: dict[str, Any], content: bytes, settings: Settings
) -> tuple[list[dict[str, Any]], int]:
    """Candidate rows for one document, and how many matches were dropped for length."""
    text = html_to_text(content)
    lines = text.split("\n")
    line_starts: list[int] = []
    offset = 0
    for line in lines:
        line_starts.append(offset)
        offset += len(line) + 1
    spans = sentence_spans(text)
    is_8k = meta["filing_type"] == "8-K"
    headings = _headings_above(lines, settings) if is_8k else [None] * len(lines)
    n = settings.context_sentences

    rows: list[dict[str, Any]] = []
    too_long = 0
    for k, (start, end) in enumerate(spans):
        sentence = text[start:end]
        line_index = bisect.bisect_right(line_starts, start) - 1
        heading = headings[line_index]
        if is_candidate(sentence, settings.forward, settings.number):
            method = "sentence"
        elif heading is not None and settings.number.search(sentence):
            method = "section"
        else:
            continue
        if len(sentence) > settings.max_chars:
            too_long += 1
            continue
        rows.append(
            {
                "cik": meta["cik"],
                "accession": meta["accession"],
                "filing_type": meta["filing_type"],
                "filed_at": meta["filed_at"],
                "char_start": start,
                "char_end": end,
                "sentence": sentence,
                "text_version": TEXT_VERSION,
                "capture_method": method,
                "heading": heading,
                "lead_in": _lead_in(lines, line_index, settings),
                "context_before": [_clip(text[a:b], settings.context_max_chars) for a, b in spans[max(0, k - n) : k]],
                "context_after": [_clip(text[a:b], settings.context_max_chars) for a, b in spans[k + 1 : k + 1 + n]],
            }
        )
    return rows, too_long


def build_company(
    company: Company, cfg: dict[str, Any], out_dir: Path = CANDIDATES_DIR
) -> Counter:
    settings = Settings.from_config(cfg)
    stats: Counter = Counter()
    rows: list[dict[str, Any]] = []
    silent: list[str] = []

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
        doc_rows, too_long = candidates_for_document(meta, content, settings)
        rows.extend(doc_rows)
        stats["documents"] += 1
        stats["dropped_too_long"] += too_long
        for row in doc_rows:
            stats[f"candidates {row['filing_type']} {row['capture_method']}"] += 1
            stats["with_lead_in"] += row["lead_in"] is not None
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
