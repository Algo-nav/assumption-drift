"""Deterministic HTML to text, and sentence spans. Standard library only.

Two things depend on this module giving the same answer every time. The
candidate pass stores character offsets into the text it returns, and the
Phase 3 validator has to find a stored excerpt verbatim in that same text.
That is why it lives in the package and not under `pipeline/`.

Changing anything here moves offsets and can orphan excerpts already stored.
If you must, bump TEXT_VERSION; candidate rows record the version they used.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

__all__ = ["TEXT_VERSION", "html_to_text", "sentence_spans"]

TEXT_VERSION = 1

_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
     "source", "track", "wbr"}
)
_BLOCK = frozenset(
    {"address", "article", "aside", "blockquote", "body", "br", "dd", "div", "dl", "dt",
     "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
     "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section", "table", "tbody",
     "tfoot", "thead", "tr", "ul"}
)
_CELL = frozenset({"td", "th"})
# Never content. `ix:header` is the hidden inline XBRL block at the top of modern filings.
_SKIP = frozenset({"script", "style", "title", "noscript", "template", "ix:header"})
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none", re.IGNORECASE)


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_tag: str | None = None
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_tag is not None:
            if tag == self._skip_tag:
                self._skip_depth += 1
            return
        if tag not in _VOID and (tag in _SKIP or _HIDDEN_STYLE.search(dict(attrs).get("style") or "")):
            self._skip_tag, self._skip_depth = tag, 1
            return
        if tag in _BLOCK:
            self.parts.append("\n")
        elif tag in _CELL:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if self._skip_tag is not None:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if self._skip_depth == 0:
                    self._skip_tag = None
            return
        if tag in _BLOCK:
            self.parts.append("\n")
        elif tag in _CELL:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._skip_tag is None:
            self.parts.append(data)


_ZERO_WIDTH = re.compile("[\u200b\u200c\u200d\u2060\ufeff]")
_H_SPACE = re.compile(r"[^\S\n]+")  # any whitespace except newline, NBSP included


def _decode(content: bytes | str) -> str:
    if isinstance(content, str):
        return content
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return content.decode("cp1252", errors="replace")


def html_to_text(content: bytes | str) -> str:
    """Visible text of an HTML document, one block per line.

    Blocks and table rows end a line. Runs of horizontal whitespace, NBSP
    included, collapse to one space. Blank lines are dropped. Nothing else is
    rewritten, so quotes, dashes and symbols come out as the filer wrote them.
    """
    extractor = _Extractor()
    extractor.feed(_decode(content))
    extractor.close()
    text = _H_SPACE.sub(" ", _ZERO_WIDTH.sub("", "".join(extractor.parts)))
    return "\n".join(line for line in (raw.strip() for raw in text.split("\n")) if line)


# --- sentences -------------------------------------------------------------

_BREAK = re.compile(r"[.!?][\"')\]\u201d\u2019]*\s+")
_TRAILING_WORD = re.compile(r"([A-Za-z][A-Za-z.]*)$")
_LEAD_MARKER = re.compile(r"^(?:[\u2022\u25e6\u25aa\u25ab\u25cf\u25cb\u25a0\u25a1\u00b7]\s*|[-\u2013\u2014*]\s+)+")
_ABBREVIATIONS = frozenset(
    "inc corp co ltd llc lp plc mr mrs ms dr jr sr st vs no nos approx est fig e.g i.e etc "
    "u.s u.k jan feb mar apr jun jul aug sep sept oct nov dec q fy ca pp".split()
)


def _is_abbreviation(word: str) -> bool:
    lowered = word.lower()
    return lowered in _ABBREVIATIONS or (len(word) == 1 and word.isupper()) or bool(
        re.fullmatch(r"(?:[A-Za-z]\.)+[A-Za-z]", word)
    )


def _line_spans(line: str, base: int) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    for match in _BREAK.finditer(line):
        following = line[match.end() : match.end() + 1]
        if following.islower():
            continue
        if match.group()[0] == ".":
            word = _TRAILING_WORD.search(line[start : match.start()])
            if word and _is_abbreviation(word.group(1)):
                continue
        spans.append((start, match.start() + len(match.group().rstrip())))
        start = match.end()
    spans.append((start, len(line)))

    out: list[tuple[int, int]] = []
    for lo, hi in spans:
        marker = _LEAD_MARKER.match(line[lo:hi])
        if marker:
            lo += marker.end()
        if lo < hi:
            out.append((base + lo, base + hi))
    return out


def sentence_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) offsets into `text`, so that `text[start:end]` is one sentence.

    A line break always ends a sentence, which keeps bullets and table rows
    apart. Inside a line, a sentence ends at . ! or ? followed by a space, unless
    the word before it is an abbreviation or the next character is lowercase.
    Leading bullet glyphs are left outside the span.
    """
    spans: list[tuple[int, int]] = []
    base = 0
    for line in text.split("\n"):
        spans.extend(_line_spans(line, base))
        base += len(line) + 1
    return spans
