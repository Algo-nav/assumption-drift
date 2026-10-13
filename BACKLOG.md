# Backlog

Extractor fixes found while reviewing the three-company pilot, needed before the 30-company run.
Not implemented here: each one needs a real example from a fourth company to write a test against
first, and none of the three pilot companies' data is wrong badly enough to hold up Phase 3 for it.

## Rejection notes in use

Keep the wording of review rejection notes consistent. The forms in use:

- `not guidance: <reason>`
- `duplicate: <where the figure lives>`
- `conflict loser`
- `wrong period`
- `change in basis points, no level printed` (margin guidance given as an increase or decrease in basis points)

Standing reasons for `not guidance: <reason>`, new this week:

- component figure (impact, reduction, headwind, attributable to other items, segment)
- qualitative outlook with no number
- boilerplate preamble or disclaimer
- FASB guidance
- fixed non-GAAP tax rate as accounting convention
- adjusted EBITDA not a canonical metric
- number with no target period

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

Also under this item: in two-figure headline sentences ("GAAP EPS: $2.46, Non-GAAP EPS: $2.75", "GAAP EPS:
$2.83, Record Non-GAAP EPS: $3.41") the GAAP row resolves and the non-GAAP row does not, inconsistently. QCOM
rows: Q3 FY2021, Q1 FY2024, Q1 FY2025 non-GAAP.

Third cause, derived metrics: Home Depot's releases do not print the effective tax rate, operating margin or
gross margin as a reported line. They are in the 10-K and derivable from two stated lines (provision over
pre-tax earnings; operating income over net sales; gross profit over net sales). Rows affected: every HD tax
rate, operating margin and gross margin row with "outcome: none". Lowe's will have the same.

Also under this item: sentences of the form "X and Y, respectively" list the current and prior period in order.
The matcher took the second figure for CRM FY2022 tax rate ("GAAP tax rate of 22.0% and 21.5%, respectively").
The matcher should take the figure whose position matches the row's period in the sentence's period list.

## 11. Outcome matcher accepted a forward-looking range as a reported value (top priority, fix before publish)

A reported value is a single number. Any candidate that prints a range, or sits in an outlook or guidance
table, must be rejected as an outcome. Fixtures: QCOM Q4 FY2025 EPS GAAP and EPS non-GAAP rows (guidance
accession `0000804328-25-000044`), whose outcomes were taken from the Q1 FY2026 guidance lines in the
2025-11-05 release ("GAAP diluted EPS $2.55 - $2.75", "Non-GAAP diluted EPS $3.30 - $3.50").

Second rule: a forward-looking candidate must be rejected as an outcome. Any outcome candidate containing
"expect", "forecast", "guidance" or "outlook" is forward-looking. Fixture: AVGO Q3 FY2026 operating margin
non-GAAP took its outcome from the CFO quote in the 2026-09-02 release ("we expect to maintain our non-GAAP
operating margin at 66%"), which is Q4 outlook, not the Q3 result.

Third rule, Home Depot: nearly every HD row whose period's Q4 release also carries the next year's guide took
its outcome from that guide. Rule: a candidate sentence of the form "<metric> of approximately <number>"
inside a Business Outlook or Guidance section is a guide, not a result, and must be rejected. Fixtures, all
CIK 0000354950:

- FY2022 tax rate row (stated 2022-11-15): outcome "Tax rate of approximately 24.5 percent", from the
  2023-02-21 release.
- FY2023 operating margin rows: outcome "Operating margin of approximately 14.1%", from the 2024-02-20
  release.
- FY2024 gross margin and operating margin rows: outcomes "Gross margin of approximately 33.4%" and
  "Operating margin of approximately 13.0%", from the 2025-02-25 release.
- FY2025 rows: outcomes "Gross margin of approximately 33.1%", "Adjusted operating margin of approximately
  12.8% to 13.0%" and "Effective tax rate of approximately 24.3%", from the 2026-02-24 release.

Separate defect: one of these was proposed "met" with 24.5 against a 24.6 point target. Something is applying
a tolerance to point targets, and none should exist. A point target is met only on an exact match at the
printed precision. Find where the tolerance comes from.

Fourth rule, Lowe's fixtures (CIK 0000060667), two shapes:

- Twelve tax rate rows matched the next fiscal year's "Effective income tax rate of approximately X%", the
  same forward-looking guide shape as the Home Depot rule above.
- Outcomes whose value is not in the matched sentence at all: 12.15 from a net earnings paragraph; 11.73,
  11.93 and 11.8 from a quarterly "Adjusted diluted earnings per share $1.93" line; 12.4 and 170 from a CEO
  quote. The value-in-evidence guard that exists for guidance must also run on outcomes.

Fix: not yet investigated. No pipeline code changed; this entry is the record.

## 12. Qualcomm and Lowe's outlook tables: rows missing from two-column layouts (split 12a, 12b, 12c)

The original entry blamed a prior-year results column being taken as the evidence. Phase 1 (read-only) found that is
not what happened: the model chose the estimates cell every time (Q2 FY19 revenue 4.4 to 5.2), and the range guard
rejected it for another reason. Four separate causes sit under this item.

**12a. Dash after a unit suffix in `has_range_language` (done).** `_DASH` needed a digit or `%` before the dash, so
"$4.4B - $5.2B" and "145M - 165M" read as having no range language and the block was rejected ("a range, but the
evidence has no 'to', 'between', dash..."). Now a dash may follow `B`, `M`, `K`, `bn` or `mm` after a digit. A lone dash
in prose, a negative sign after a comma and two bare figures ("$ 7.53 $ 7.63") are still not range language. Fixtures:
QCOM `0001728949-19-000011`, `-000034`, `-000053`, `-000071`, plus the negatives in tests/test_backlog_fixes.py.
Re-derived from the archive (no model call for 03): only QCOM changed, 29 new revenue drafts from Q2 FY2019 to Q4 FY2026
(32 rejected answers passed the guard; the rest were duplicates).

**12b. Block stop rule in 02_candidates (next).** A section block ends at the first gap of `section_gap_lines` blank
lines. Two consequences:
- a sub-heading such as "Fiscal Year 2020 (comparisons to fiscal year 2019)" after 8 blank lines terminates the block
  empty, so the bullets under it are outside any block;
- a table whose rows are separated by a gap after the first row loses every row after it.
Fixtures: QCOM `0001728949-19-000011`, `-000034`, `-000053`, `-000071` (GAAP EPS, non-GAAP EPS, MSM shipments and QTL
revenue rows were never offered; the block holds only the revenue row), and Lowe's `0000060667-20-000029` (February 26,
2020 fiscal 2020 outlook bullets, including EPS $6.38 to $6.58 and adjusted $6.45 to $6.65, outside any block; the only
candidate was the "Target leverage ratio" sentence, and the model's EPS read from its context was rejected for lack of
range language in the one-sentence evidence).
Plan: test on QCOM and LOW only, diff the candidate set before and after, and report the diff before running 03. It
shifts `char_start`s and so custom ids and draft ids, and misses the answer cache (item 18), so 03 would need new calls.

**12c. Evidence trimmed to the lines holding both figures (deferred).** When the model returns a range, the evidence
for a block row could be cut to the lines that contain both figures, so a prior-year cell in the same block is not in
the excerpt. This changes `excerpt` and `content_sha256` on published rows and needs a migration.

Also under this item, Lowe's December 2020 investor update (`0000060667-20-000177`): the reconciliation table prints
forecasted EPS as a low-end and high-end column pair ("Forecasted diluted earnings per share $ 7.53 $ 7.63"). The guard
still rejects it, deliberately; no low-end/high-end pair rule has been added. The same figures are carried by the
sentence row from that release.

## 13. Component figures are being structured as the metric

When the clause containing the number carries "impact", "reduction", "headwind", "attributable to other
items", "reduce our", or names a segment or program, the number is a component of the metric, not the metric,
and must be rejected. Fixtures:

- META "reduce our overall operating profit in 2021 by approximately $10 billion" (`0001326801-21-000062`)
- QCOM "approximate $0.20 reduction to EPS" (`0001728949-22-000039`)
- QCOM "estimated impact of greater than ($0.30) to EPS" (Q3 FY2020 release)
- QCOM "EPS attributable to other items ... $3.10 to $3.20" (`0001728949-19-000034`)
- QCOM "revenues attributable to other items ... approximately $1.8 billion" (Q4 FY2020 release)

Also reject a "fixed estimated Non-GAAP tax rate": it is an accounting convention, not a forecast.

Fix: not yet investigated. No pipeline code changed; this entry is the record.

Also under this item: a segment figure structured as total revenue. Fixture: AVGO `0001730168-24-000095`,
"revenue from AI to be $12 billion" (AI is a segment of revenue, not total revenue).

Also under this item, Home Depot fixtures:

- "Interest expense of approximately $1.8 billion" and "Net interest expense of approximately $2.2 billion"
  structured as other income and expense (`0000354950-23-000110`, `0000354950-25-000030`).
- "approximately 40 basis point impact from acquired intangible asset amortization" structured as operating
  margin non-GAAP (`0000354950-25-000030`, `0000354950-25-000238`).

Also under this item: the word "guidance" in the accounting sense ("FASB guidance", "accounting guidance",
"guidance issued") must not open an outlook block. Fixture: AVGO `0001730168-20-000126`.

Also under this item, Lowe's fixtures:

- "53rd week expected to increase total sales by approximately $1.0 billion to $1.5 billion" structured as
  revenue (five rows, 2021-12-15 to 2022-11-16). The 53rd week is a component of sales.
- "approximately 60 basis points of dilution on the consolidated full year 2022 operating margin outlook"
  structured as operating margin (`0000060667-22-000162`, `0000060667-22-000166`). A dilution amount is not
  the margin level; reject as "change in basis points, no level printed".

## 14. Printed plus-or-minus repair fails when the sentence ends "; and" or "; or" (low priority)

The printed plus-or-minus repair does not fire when the sentence ends with "; and" or "; or". Fixtures: AVGO
`0001730168-20-000099` and `0001730168-20-000154`. The table row for the same figure was captured, so the
loss is a duplicate, not a missing record.

Fix: strip the trailing "; and" or "; or" before the repair. No pipeline code changed; this entry is the record.

## 15. Item returned with line_first 0 and line_last 0 is rejected by the section-line guard (low priority)

When the model omits line numbers, the item comes back with `line_first` 0 and `line_last` 0 and the
section-line guard rejects it. Fixture: AVGO `0001730168-26-000051`. The table row carried the figure, so
again the loss is a duplicate, not a missing record.

Fix: fall back to text match when the model omits line numbers. No pipeline code changed; this entry is the record.

## 16. Block finder flags results paragraphs as outlook blocks with no items (low priority)

Home Depot 2019 to 2021 releases put guidance in prose ("The Company expects its fiscal 2019 sales to grow by
approximately 3.3 percent and comp sales ... up approximately 5.0 percent") after a results paragraph. The
results paragraph was flagged as an outlook block with no items, while the prose guidance was captured
separately. Harmless, but the flagged empty blocks are noise in review.

Fix: not yet investigated. No pipeline code changed; this entry is the record.

The Lowe's February 2020 outlook (`0000060667-20-000029`) is the same failure: prose bullets under a heading, with the
guidance verb in only one bullet, so the section block is lost and the other bullets are not candidates (see 12b).

## 17. Period derivation from "fiscal year ending <date>" ignores fiscal_year_named_for (high priority, fix before publish)

Period derivation from "fiscal year ending <date>" uses the calendar year of the end date and ignores
`fiscal_year_named_for`. For a fiscal year ending in January or early February, that names the wrong year.
Fixtures, CIK 0000060667 (Lowe's):

- Fiscal 2019 ("fiscal year ending Jan. 31, 2020") was written FY2020. Finding no FY2020 release in range, it
  was proposed withdrawn. 11 rows in `0000060667.csv` from the 2019 releases.
- Fiscal 2020 ("fiscal year ending Jan 29, 2021", `0000060667-20-000177`) was written FY2021 and matched
  fiscal 2021 outcomes (EPS $12.04 against a $7.53 to 7.63 guide).
- The genuinely withdrawn fiscal 2020 guidance (February 2020, withdrawn May 2020) is absent.

Fix: derive the period using `fiscal_year_named_for`, not the calendar year of the end date. Then refresh
untouched LOW rows with `05_review --refresh-pipeline-fields` and re-run outcomes for them. Skipped rows carry
the note "period mislabelled". No pipeline code changed; this entry is the record.

Status of 17: code fix landed (`fix_end_date_period` in 03_structure, `period_named_by_end_date` in common, a withdrawal-search
guard in 04). It relabels only a sentence, lead-in or heading that carries the end date itself: 8 Lowe's EPS rows. The Lowe's
2019 releases print "Fiscal Year 2019" as a subheading 02 does not capture (lead_in and heading are null) and put the end date
in the EPS bullet only, so the tax rate and operating margin bullets under it are still written FY2020 by the model and still
proposed withdrawn (5 rows). Open: capture that subheading as the heading in 02, or carry the date from a sibling bullet.
Also: the relabelled drafts have new draft_ids (the period is in the hash), so `05_review --refresh-pipeline-fields` leaves
the old FY2020 review rows as "no longer drafted" and does not append the new ones; a plain 05 run is needed, and the stale rows
need retiring by hand.

Also under 17: the February 26, 2020 release's fiscal 2020 outlook (sales growth, comparable sales, operating margin change,
EPS $6.45 to $6.65) produced no draft, so the genuinely withdrawn fiscal 2020 guidance is still absent. And the "Fiscal Year
2019" subheading above a bullet list is not captured by 02_candidates, which leaves the 2019 tax rate and operating margin
rows labelled FY2020 (and proposed withdrawn). After `05_review --company LOW` appended the 8 relabelled EPS rows, the old
FY2020 EPS rows remain in the queue next to them and need retiring by hand.

## 18. 03_structure's answer cache no longer matches its requests for LOW (fix before the next full regeneration)

`03_structure --company LOW` projects 0 of 309 requests answered, although `--from-archive` finds all 309 archived answers
(fingerprints are compared by `llm.current_results`, which drops an answer whose request text, prompt or schema changed).
Something changed the request text after the archive was written; not yet identified. Find what (diff the system prompt, schema
and per-candidate user text against the archived fingerprints), and check whether the other companies are affected before any full
regeneration: a plain `--submit` would re-send everything (309 requests for LOW, about $0.61). 03b_verify showed the same
0-of-127 for LOW, so the cause may be shared. No pipeline code changed; this entry is the record.

## 19. Adobe targets table is never a candidate

Adobe `0000796343-24-000250` prints its fiscal 2025 and first quarter fiscal 2025 targets as a label and value table
(Total revenue "$23.30 billion to $23.55 billion", EPS "GAAP: $15.80 to $16.10", "Non-GAAP: $20.20 to $20.50"). The
lead-in "The following table summarizes Adobe's fiscal year 2025 targets1:" ends in a colon, so it is not treated as a
heading and opens no section block, and the label and value rows carry no guidance verb, so no sentence candidate is made
either. Only the footnote sentences ("Targets assume non-GAAP operating margin of ~46 percent...") were captured. This is a
single-column table with no results column, so it is not a two-column problem. Fix: not yet investigated. No pipeline
code changed; this entry is the record.

## 20. Section-versus-sentence dedupe dropped the section number when the numbers differed (fixed)

`dedupe_drafts` kept the sentence-captured draft over any section draft on the same (cik, metric, period, stated date), even
when the numbers differed. QCOM Q3 FY19 (`0001728949-19-000034`) lost its real revenue range, 9.2 to 10.2 USD billions
(section draft `01D9RB1Q00R15TGGJ8NRWVN21C`, block `b-0000804328-000172894919000034-11387`), to a $150 million sentence
about QTL revenues from Huawei (`01D9RB1Q00RZQ1FNHXT7CVWGP5`), which is a component figure (see item 13) and was rejected in
review. 64 section drafts were dropped this way across six companies, 53 of them NVDA, mostly the same figure printed in
USD billions and USD millions.

Fix: drafts on one key are equal when low, high and unit agree after unit normalisation, within half a unit of the last
printed digit of the coarser figure (1.52 USD bn = 1515 USD m, 4.0 bn = 3950 m, 4.1e9 = 4100e6); in one unit they must match
exactly. Equal drafts dedupe (sentence kept, else earliest), and the skip record carries both printed figures. Drafts that
are not equal are all kept with `conflict: true`, sentence against section and sentence against sentence, and the pair is
logged in `{cik}.skipped.jsonl` with both ids (`conflict_log`). The range-kept-over-point rule is unchanged: it drops 57
points across NVDA (55) and CRM (2), all table midpoints inside the sentence's range, all with an approved kept draft.
Re-derived from the archive: 54 newly kept drafts across 11 companies. Tests in tests/test_backlog_fixes.py; the old
"sentence wins over a different section" tests were rewritten to the new rule.
Review of the new conflict rows is open; 03b and 04 have not been run for them.

Follow-through on 20: the range-over-point rule was reviewed and stays as it is. It drops 57 points (NVDA 55, CRM 2), every
one a table midpoint inside the sentence's plus-or-minus range, and every kept range draft is approved.
`03b_verify` and `04_outcomes` take `--only PATH` (a text file of draft ids, one per line; ids not found are reported and
skipped; 04 merges the listed drafts' rows into data/outcomes/ and leaves the rest as they are). The 54 newly kept ids are in
data/_work/new_drafts_item20.txt (data/_work/ is gitignored). `rr review --filter conflict` lists every row with
conflict=true. New conflict rows are in the review CSVs; the dumps are data/review/<ticker>_conflicts.txt.
Note: `05_review --refresh-pipeline-fields` does not rewrite the `conflict` column on rows a person has touched, so an
approved row whose draft is now in conflict keeps conflict=false and carries a pending change proposal instead.
