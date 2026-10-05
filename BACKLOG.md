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

## 5. Outcome lines for full-year results in Lowe's and Home Depot releases: pair the label with its value line

Found on the fourteen-company run (`aid_outcome_note` "the model found no line reporting that metric for that
period", 40 Lowe's rows and 20 Home Depot rows). In both companies' fourth quarter releases the full-year
results sit in table rows with the label on one line and the figures on the next ("Operating margin (3)", then
"10.1 % 11.3 % 12.7 % 13.5 %"; Lowe's "Operating income 1,687 9.07 1,704 7.59 11,557 13.38 10,159 10.47" under a
"Three Months Ended Fiscal Year Ended" header). `04_outcomes.select_lines` needs the metric and a number in one
sentence, so it never selects such a row. The only lines it finds are the next year's guidance bullets
("Tax rate of approximately 24.5 percent"), and the model correctly answers that none reports the result.
The fix `select_lines` got for full-year rows (a line naming the year and a number under a line that names the
metric) does not reach these either: the figure line names no year.
Fix: give the outcome selector the same label-plus-value pairing the section capture already has
(`02_candidates.find_blocks`, and `03_structure` reading a label line with its value line as one item). A figure
line with no metric of its own is paired with the label line above it, and the pair is sent to the model as one
line with the table's column header (`Three Months Ended | Fiscal Year Ended`, found the way
`04_outcomes.find_result_header` already finds one) so it can pick the fiscal-year column. Fixtures are the ten
rows of the diagnostic (five Lowe's, five Home Depot) and the Home Depot FY2022 and FY2025 and Lowe's FY2022
releases above. Expect it to change the 04 requests for every company, so it costs a re-run of 04.

## 7. A later filing that restates a metric and period at a different number, or prints the earlier guidance beside preliminary results, acknowledges the earlier row

A later filing can name the same metric and the same period as an earlier guidance row and give a different
number, or print the earlier guidance next to preliminary results. Either way the company is saying the
earlier figure no longer stands, and that is an acknowledgement of the earlier row. Today neither shape
reaches the acknowledgement stage: `04_outcomes` looks for a direction word near the metric, and a restated
number or a side-by-side table has none.
Fix: in the candidate filter, treat a later filing's line as an acknowledgement candidate when it holds the
row's metric and period (matched the way the outcome matcher matches them) and either (a) states a different
number than the row's range, or (b) prints the row's own guidance range next to preliminary results for that
period. The earliest such filing wins, as it does now. Fixture: AMD `0000002488-22-000163` (cached under
`data/raw/0000002488/`).

## 8. A row stated after its period closed but before the results filing is a preliminary estimate: flag it, keep it, count it nowhere

A guidance row whose `stated_at` is after the day its period closed and before the filing that reports the
results is not guidance for a future period: the company is estimating a result it has not yet reported
(AMD's preannouncement releases are the shape). It is a real statement and worth keeping, but it is not a
forecast that a later result can meet or miss.
Fix: flag such a row `preliminary=true` (the period's close comes from the company's fiscal calendar in
`config.yaml`, the way `04_outcomes` dates a withdrawal, and the results filing is the outcome's own
evidence filing). Keep it in the data, exclude it from the met, missed and worse counts on the card and on
the Space, and show it as preliminary wherever its row is listed. This is different from
`PERIOD_BEFORE_STATED` in `03_structure`, which catches a period the model mislabelled; here the period is
right and the statement is late.

## 9. A sentence printing a dollar range and a growth range is guidance, and a range can have a negative low end

Two range-parsing defects, both seen in Amazon's guidance sentences. Fixtures: AMZN accession
`0001018724-22-000011`; META accessions `0001628280-25-036719` and `0001628280-25-047114`.

- A sentence that prints both a dollar range and a growth range ("between $116.0 billion and $121.0 billion,
  or to grow between 3% and 7%") is guidance. The change-word guard (the one that treats "grow", "increase"
  and the like as marking a change rather than a level) must not fire when a dollar range is printed in the
  same sentence: the dollar range is the guided value and the growth range is its restatement.
  META shows the same guard failing without a growth range printed beside the dollar range: "full year 2025
  total expenses to be in the range of $114-118 billion" (`0001628280-25-036719`) and the narrowed "$116-118
  billion" (`0001628280-25-047114`) were rejected because a later clause of the sentence mentions growth. The
  guard should look only at the clause that carries the dollar range, not the whole sentence.
- "$(1.0) billion and $3.0 billion" is a range with a negative low end (-1.0 to 3.0), not an endpoint-only
  value. The parenthesised figure is a negative number, so the range parser must read it as the low end
  rather than dropping it and keeping only the 3.0.

Fix: both in the range parser and guard. No pipeline code changed yet; this entry is the record.

## 10. Outcomes missing where the filing is cached and the figure is in it

Rows that should have an outcome and have none, although the results filing is in the cache and prints the
figure. Known rows:

- META revenue Q4 FY2021: reported in `0001326801-22-000008`, $33.67B.
- META revenue Q4 FY2025: reported 2026-01-28, $59.89B.

The other FY2025 META quarters resolved, so the suspect is the Q4 release layout: a full-year column printed
next to the quarter column, so the extractor either takes the wrong column or finds two candidates and gives
up. Check the Q4 table handling first.

Also under this item, the same symptom in bulk: about 25 recovered AMD rows and about 159 MU rows have no
outcome. Work out whether they share the Q4 / full-year layout cause or have separate ones before fixing.

Fix: not yet investigated. No pipeline code changed; this entry is the record.
