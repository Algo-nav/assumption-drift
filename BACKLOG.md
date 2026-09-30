# Backlog

Extractor fixes found while reviewing the three-company pilot, needed before the 30-company run.
Not implemented here: each one needs a real example from a fourth company to write a test against
first, and none of the three pilot companies' data is wrong badly enough to hold up Phase 3 for it.

## 1. Salesforce "N/A | value" table rows: assign the value to column 2, not column 1

Salesforce's guidance tables sometimes print a quarter column and a full-year column side by side,
and the quarter cell is `N/A` when the company only guided the full year (`Non-GAAP operating margin
N/A ~17.7%`). The candidate/structure pass has to read that as one value in the second column, not
as a bare value with no column to anchor it to. Right now a row like this is exactly the shape that
produces a `wrong_period` verdict from `03b_verify.py`: the model has to work out from the table
header alone that `~17.7%` belongs to the full year, because the row's own text does not say so.
Fix: when a table row has an `N/A` (or blank) cell followed by a value cell, bind the value to the
header column it actually sits under, not to the row's first column by default.

## 2. Parentheses on tax rate figures: per-company sign, not a global rule

`03_structure.py`'s `fix_parens_sign` guard treats a parenthesized number as negative, which is right
for a decline in most metrics but not guaranteed for a tax rate: some filers use parentheses on a tax
rate line for a rate that is unusually low or a benefit rather than an expense, not for "negative" in
the sense the rest of the guard assumes. Target and NVIDIA have not produced a case where this
mattered; Salesforce's tax rate footnotes are the shape where it will, once the company list grows.
Fix: move the parentheses-means-negative rule for tax rate specifically into `config.yaml`, one flag
per company (default: the current global behavior), instead of hardcoding it for every filer alike.

## 3. The tax footnote sentence must never produce a third period

Some tax rate footnotes name exactly two periods in one sentence, a quarter and the fiscal year it
falls in ("our effective tax rate for the third quarter of fiscal 2024 and fiscal year 2024 was..."),
each with its own number. `parse_periods` (`pipeline/02_candidates.py`) is a general-purpose scanner
and has no guard against finding a third period in a sentence shaped like this: a stray year mention
elsewhere in the same sentence, or a footnote reference that happens to look like a period label,
would currently pass straight through as if it were a third guided period. None of the three pilot
companies has triggered this, but the shape is common enough in tax rate footnotes generally that it
will surface at 30 companies. Fix: a footnote-specific check that a sentence naming a quarter-and-its-
year pattern is capped at exactly two periods, and rejects (rather than silently keeps) a third match.

## 4. Acknowledgement candidates need a guidance reference and a period match

The 10-K widening proposed four acknowledgements and all four were rejected in review, each for
failing one of the same two checks. Fix both in the candidate filter, before anything reaches the queue:

- The sentence holding the direction word (higher, lower, above, below, exceeded, ...) must also hold a
  guidance reference: `guidance`, `outlook`, `expected range`, `we had expected`, `our prior`, or
  `compared with our`. A direction word with no reference to what was guided is just a description of
  results, not an acknowledgement of a miss or beat.
- The candidate must match the row's period the way the outcome matcher (`04_outcomes.py`) does, not
  by looser text overlap. A sentence about a different quarter or year is not an acknowledgement of
  this row's assumption.
