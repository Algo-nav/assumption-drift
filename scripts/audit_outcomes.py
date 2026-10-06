#!/usr/bin/env python3
"""Read-only audit of approved outcomes in data/review/*.csv.

Flags approved rows with a reported value whose evidence excerpt (a) uses
forward-looking language, (b) shows the reported number inside a range, or
(c) does not contain the reported value in any 0/1/2-decimal format, also scaled
by 1000 and 1000000, or with one trailing footnote digit ("$1.331" for 1.33).
"""
import csv
import glob
import os
import re
import sys
from decimal import Decimal, InvalidOperation

csv.field_size_limit(sys.maxsize)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEYWORDS = ("expect", "guidance", "outlook", "forecast", "anticipate", "target")
NUM = r"\d[\d,]*(?:\.\d+)?"
RANGE_RE = re.compile(
    rf"(?P<a>\$?\s*{NUM})\s*(?:%|percent)?\s*(?:to|through|-|–|—)\s*(?P<b>\$?\s*{NUM})",
    re.I,
)


def forms(value):
    """Textual forms of the value: 0/1/2 decimals, with and without commas.

    The sign is dropped: filings say "decreased 1.8%", not "-1.8%". The value
    also appears scaled by 1000 and 1000000 (billions printed as millions or
    thousands).
    """
    try:
        base = abs(Decimal(value.strip().replace(",", "").lstrip("$")))
    except InvalidOperation:
        return set()
    out = set()
    for scale in (1, 1000, 1000000):
        d = base * scale
        for places in (0, 1, 2):
            out.add(f"{d:.{places}f}")
            out.add(f"{d:,.{places}f}")
    return out


def num_pat(form):
    # not embedded in a longer number
    return rf"(?<![\d.,]){re.escape(form)}(?![\d]|[.,]\d)"


def present(forms_, text):
    if any(re.search(num_pat(f), text) for f in forms_):
        return True
    # footnote marker glued on: the excerpt number is a form plus one extra digit
    for m in re.finditer(NUM, text):
        tok = m.group(0)
        if len(tok) > 1 and tok[-1].isdigit() and tok[:-1] in forms_:
            return True
    return False


def in_range(forms_, text):
    for m in RANGE_RE.finditer(text):
        for g in ("a", "b"):
            tok = re.sub(r"[$\s]", "", m.group(g))
            if tok in forms_:
                return True
    return False


def reasons(value, excerpt):
    why = []
    low = excerpt.lower()
    hits = [k for k in KEYWORDS if k in low]
    if hits:
        why.append("keyword:" + ",".join(hits))
    f = forms(value)
    if in_range(f, excerpt):
        why.append("range")
    if not present(f, excerpt):
        why.append("value-missing")
    return why


def main():
    total = 0
    for path in sorted(glob.glob(os.path.join(ROOT, "data", "review", "*.csv"))):
        n = 0
        rows = []
        with open(path, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                val = (r.get("outcome.reported_value") or "").strip()
                if (r.get("approved") or "").strip().lower() != "true" or not val:
                    continue
                excerpt = r.get("outcome.evidence.excerpt") or ""
                why = reasons(val, excerpt)
                if why:
                    n += 1
                    rows.append((r, val, excerpt, why))
        print(f"== {os.path.basename(path)} ==")
        for r, val, excerpt, why in rows:
            snippet = " ".join(excerpt.split())[:100]
            print(
                f"{r['record_id']} | {r['ticker']} | {r['assumption.metric']} | "
                f"{r['assumption.target_period']} | {val} | [{';'.join(why)}] {snippet}"
            )
        print(f"-- {n} flagged")
        total += n
    print(f"TOTAL flagged: {total}")


if __name__ == "__main__":
    main()
