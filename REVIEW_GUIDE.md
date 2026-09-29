# Review Guide: assumption-drift review queue

This is the manual for reviewing `data/review/*.csv` with `rr review`. Keep it open beside the terminal. Nothing publishes without your approval, so this step is the dataset's quality.

---

## 0. Before you start

```
cd /Users/macintoshhd/Desktop/Renaissance/assumption-drift
git pull --ff-only
git log --oneline -1          # expect 4e3dddb or later
.venv/bin/rr review data/review/0000027419.csv
```

If `rr` is not found, use `.venv/bin/python -m research_record.cli review data/review/0000027419.csv`.

Order: Target (89 rows), NVIDIA (212), Salesforce (322). Commit and push after each file.

Keep one browser tab open on `https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany` for hand checks.

---

## 1. What a row is

One row = one numeric promise a company made in a filing, plus what happened to it.

Three parts, left to right:

| Part | Question it answers | Who fills it |
|---|---|---|
| **Assumption** | What did the company say would happen, for which metric, which period, stated when, and where is the sentence? | Pipeline. You correct it if wrong. |
| **Outcome** | What did the company later report for that same metric and period, and where is that sentence? | Pipeline. You clear it if it matched the wrong thing. You never fill it. |
| **Acknowledgement** | Did the company ever say, in a later filing, that it landed better or worse than that guidance? | Pipeline. You clear it if wrong. You never fill it. |

Plus three columns that are yours: `approved`, `hand_verified`, `reviewer_note`.

The status is computed from the numbers by the rubric, never typed by anyone:

- `met`: reported value inside the range
- `missed`: reported value outside the range (on either side; `better` or `worse` than guided is shown separately)
- `withdrawn`: company pulled the guidance before the period ended
- `unresolved`: period not closed yet, or no report found
- `open`: what every row says until it is published; ignore it

---

## 2. Rule zero

**You fill three columns: `approved`, `hand_verified`, `reviewer_note`. Nothing else, ever.**

- You **correct** an assumption cell when the excerpt proves it wrong (metric, number, unit, period).
- You **clear** an outcome or acknowledgement when it matched the wrong thing.
- You **never fill** an empty outcome or acknowledgement by hand, even if you know the answer. If the pipeline did not find it, the row is `unresolved` or "never acknowledged". That is a true statement about the pipeline's reach, and it goes in the dataset card under limitations. Filling it by hand would make the dataset claim something the pipeline cannot reproduce.
- You **never build** a row from scratch. If you spot guidance the pipeline missed, write it in `reviewer_note` on the nearest row (`missed guidance: revenue 5.0-5.2B Q3 FY24`) and move on. Those notes become the pipeline fixes for the 30-company run.

---

## 3. What the screen shows

`rr review` prints one row at a time:

```
[12/89]  TGT  01F...   sentence   aid_verify: yes   proposed: missed (worse)

ASSUMPTION
  metric:   EPS GAAP           unit: USD per share
  target:   1.55 to 1.75       period: Q1 FY2020     stated: 2020-03-03
  heading:  Guidance
  lead-in:  For the first quarter of 2020, the company expects:
  header:   (none)
  excerpt:  "...GAAP EPS from continuing operations of [1.55] to [1.75]..."
  url:      https://www.sec.gov/Archives/edgar/data/27419/...

OUTCOME
  reported: 0.56 on 2020-05-20
  excerpt:  "First quarter GAAP EPS from continuing operations were $0.56..."
  note:     (none)

ACKNOWLEDGEMENT
  (none)

flags: conflict=false  empty_block=false  verify_class=(none)
keys: y approve  n reject  e edit  s skip  v hand-verify  q quit
```

Numbers in `[brackets]` are the target values found in the excerpt. The `lead-in` and `header` lines are where the period was read from when it is not in the excerpt itself.

---

## 4. The keys

| Key | What it does | When |
|---|---|---|
| `y` | Sets `approved=true`, moves on | The row is right |
| `n` | Asks for a note, sets `approved=false`, moves on | The row should not exist |
| `e` | Asks which field, then the new value; stays on the row | A pipeline cell is wrong; fix it, then `y` |
| `s` | Skips, leaves the row untouched | Empty-block rows, or you want to come back |
| `v` | Sets `hand_verified=true` | Only after you opened EDGAR yourself (section 8) |
| `q` | Saves and quits | Any time; re-running resumes where you left off |

Everything is written to disk after each keypress. Closing the terminal loses nothing.

---

## 5. Which rows get how much attention

The reviewer sorts rows for you: verifier `no` first, then section-captured, then the rest. Depth depends on the row:

| Tier | Rows | What you do | Time per row |
|---|---|---|---|
| **Full** | `aid_verify=no`; proposed `worse`; proposed `withdrawn`; `conflict=true`; any Salesforce section row; any long-range target (period more than 2 years after stated date) | All of section 6, every step | 1 to 2 min |
| **Medium** | Section-captured `yes` rows (NVIDIA, Target) | Excerpt, header, metric, period, glance at numbers | 20 sec |
| **Fast** | Sentence-captured `yes` rows, proposed `met`, `better` or `unresolved` | Read excerpt once, confirm metric and period match, `y` | 10 sec |

Why worse-than-guided rows get the full check: they are the rows that carry the dataset's story, and the rows a PM will click on first. A wrong worse-than-guided row costs more credibility than ten wrong better-than-guided rows.

Why fast rows are safe to move through: the code already guarantees the numbers came from the excerpt, the units match the metric, and the ± was expanded. What code cannot check is the metric label and the period. Those two are your job on every row.

---

## 6. The per-row checklist

Work top to bottom. Stop at the first failure and act on it.

### Step 1: Is it guidance?

Read the excerpt. It must be the company saying what a number **will** be, for a **named** quarter or fiscal year.

Reject (`n`) if it is:
- A result: "revenue was", "grew 5 percent", "returned $1.9 billion"
- A growth rate on a dollar metric: "expected to grow 20 percent" (no absolute level)
- A half year or "back half", "remainder of the year"
- A long-run aspiration with no period: "we target 30 percent margins over time"
- A relative statement: "in line with last year", "50 bps above the 2023 rate", with no absolute figure printed
- Capital return, buybacks, dividends

Note vocabulary: `not guidance: result`, `not guidance: growth rate`, `not guidance: half year`, `not guidance: relative`, `not guidance: capital return`.

### Step 2: Metric

The excerpt's own label must match `metric`.

- "GAAP diluted EPS" is `EPS GAAP`; "adjusted EPS" or "non-GAAP EPS" is `EPS non-GAAP`
- "Operating income margin" is `operating margin`, not `operating income`
- "Comparable sales" is `comparable sales`, "total sales" is `revenue`
- NVIDIA's "other income and expense" excerpt saying "expense of" means the target should be **negative**

For section rows: if the excerpt is a table line with only a value, the metric came from the label line or the block heading. Check `heading`.

Wrong: `e`, field `assumption.metric`, pick from the canonical list (revenue, gross margin GAAP, gross margin non-GAAP, operating expenses GAAP, operating expenses non-GAAP, operating income, operating margin GAAP, operating margin non-GAAP, EPS GAAP, EPS non-GAAP, tax rate, other income and expense, comparable sales, free cash flow). Then `y`. Note: `metric fixed: was X`.

### Step 3: Numbers and unit

The `[bracketed]` figures in the excerpt are the targets. Check:

- **± expansion.** "$2.20 billion, plus or minus 2%" must show 2.156 to 2.244. "58.8 percent, plus or minus 50 basis points" must show 58.3 to 59.3. "10 percent, plus or minus 1 percent" on a rate must show 9 to 11 (absolute), not 9.9 to 10.1.
- **Unit.** Billions stored as billions, millions as millions, never mixed. Percent metrics say `percent`. EPS says `USD per share`.
- **Range language.** "$1.30 to $1.70", "between", "$11.13 - $11.23" are ranges. Two numbers joined by "and" or "respectively" are two separate rows, not a range.
- **Floors.** "$1.30+", "at least", "or more", "or better" means `target_low` is set and `target_high` is blank. If the excerpt has none of those words and `target_high` is blank, the floor is false: `e` and set `target_high` equal to `target_low`.
- **Sign.** Parentheses on EPS mean negative: "$(0.04) to $(0.03)" is -0.04 to -0.03. Parentheses on a tax rate or margin do **not** mean negative unless the text says "benefit" or "loss".

Wrong: `e`, fix `assumption.target_low` and/or `assumption.target_high` and/or `assumption.unit`. Note: `numbers fixed: <what>`.

### Step 4: Period

The single most common error. Where the period lives:

- **Sentence rows.** In the excerpt itself ("for the third quarter of fiscal 2027") or in `lead-in` ("NVIDIA's outlook for the first quarter of fiscal 2020 is as follows:").
- **Section rows.** In `header`. The header is the table's column labels in order, joined by `|`, for example `Q4 FY21 | FY21 | Q1 FY22 | FY22`. The row's value is one column of that table. Count which column the value sits in and match it to the header. If the header shows four periods and the pipeline took the value from the third column, the period must be the third label.

Fiscal calendars matter. Target's "fiscal 2022" ends January 2023. NVIDIA's "fiscal 2027" runs Feb 2026 to Jan 2027. Salesforce's "FY27" ends January 2027. The pipeline knows this; you are checking the label matches the filing's own words, not the calendar.

When `aid_verify=no` with class `wrong_period`: the verifier tried to map calendar dates to fiscal labels itself and is often wrong at that. **Trust the header and the filing's own words over the verifier's reason.**

Wrong: `e`, field `assumption.target_period`, format `Q3 FY2024` or `FY2024`. Note: `period fixed: was X`.

### Step 5: Stated date and source

`stated` should be the filing date of the release the excerpt is from. It is nearly always right; glance only.

### Step 6: Outcome (only if filled)

Read the outcome excerpt. It must be:

- **Same metric.** Not a neighbour. "GAAP EPS were $0.56 and adjusted EPS were $0.59" reports both; the row must have picked the one matching its metric.
- **Same period.** "Fourth quarter operating margin was 3.7 percent" is Q4, not the full year. A full-year target with a Q4 outcome is a mismatch.
- **A result, not another forecast.** "was", "were", "grew", "declined". If it says "is expected to be", it is guidance, not an outcome.
- **From a later filing.** `reported` date must be after `stated`.
- **Not a multi-column table read blind.** If `note` says `multi-column, unconfirmed`, the pipeline already held it back and the outcome is blank; nothing to do.

Wrong: `e`, clear `outcome.reported_value` and `outcome.reported_at` (enter an empty value). The row becomes `unresolved`, which is the correct claim. Note: `outcome mismatch: <why>`.

Right: nothing to do; the status follows.

### Step 7: Status sanity

`proposed` should follow from the numbers you just checked. If you changed a number or cleared the outcome, the reviewer recomputes it on save.

- `withdrawn`: read the withdrawal note. The withdrawal filing must be dated **before** the period ended. Target 2020-05-20 withdrawing FY2020 guidance (period ends 2021-01-30) is correct. A "withdrawal" after the period closed is not one; the row resolves on the numbers.
- `worse` or `better` are shown next to `missed`. Better than guided is still `missed` in the schema (the assumption was wrong either way); the direction is computed from the metric's polarity (`higher_is_better` in `pipeline/config.yaml`), not stored. Above the range is better for revenue and worse for operating expenses or the tax rate.

### Step 8: Acknowledgement (only if filled)

The excerpt should mention the metric alongside the gap: "below the low end of our guidance", "above the high end of the Company's guidance range", "did not meet". A generic "results were below expectations" still counts. An unrelated sentence does not.

Wrong: `e`, clear `acknowledged_at`. Note: `ack mismatch`.

Blank on a missed row means "never acknowledged in EDGAR text". That is a finding, not a gap. Leave it.

### Step 9: Approve

`y`. Every tenth approval the reviewer stops and prints the EDGAR URL. Do section 8 before pressing `v`.

---

## 7. Special rows

**`conflict=true`.** Two section drafts disagree on the same metric, period and date. Both are shown, neither is trusted. Open the cached filing at `data/raw/{cik}/{accession}.html`, find the table, decide which is right. Fix that row's values if needed and `y`. On the other: `n`, note `conflict loser`.

**`empty_block=true`.** A guidance section produced no draft. All assumption fields are blank. `s` to skip. If curious, open the cached filing and see whether there was numeric guidance the pipeline missed; if so, note it on the next real row from the same filing: `missed guidance in <accession>: <metric> <value> <period>`.

**`aid_verify=no`, by class:**

| Class | What usually happened | What to do |
|---|---|---|
| `wrong_period` | Verifier mapped dates to fiscal labels itself; often the verifier is wrong on Salesforce | Check the header. Trust the filing's own words. Usually `y`. |
| `wrong_value` | ± direction, rounding ("$21B" vs "$21.0B"), or a real extraction error | Recompute the ± by hand. Fix if wrong, `y` if the verifier is being pedantic. |
| `wrong_metric` | GAAP vs non-GAAP, or a neighbouring label | Check the label line and heading. Fix or `y`. |
| `wrong_sign` | Parentheses or "expense of" | Apply the sign rules in step 3. |
| `not_guidance` | Verifier thinks it is a result or relative statement | Re-read with step 1's list. Often the verifier is right. |
| `other` | Read the reason | Judge it. |

**Long-range targets** (Salesforce "FY24 revenue of $34-35B" stated in 2019). Keep them. They are the most interesting drift rows. Full tier because the outcome match is five years away and easy to get wrong.

**Point targets from reconciliation tables** (NVIDIA gross margin 62.5 exactly, when the bullet in the same filing says "plus or minus 50 bps"). The dedup rule already prefers the range when both were captured. If only the point survived, `y`; it is what that table says. Note: `point from table; bullet has ±50bps`.

---

## 8. Hand verification on EDGAR

Every tenth approved row, per company. This is the provenance guarantee on the dataset card, so it is done on EDGAR itself, not on the cached copy.

1. The reviewer prints the row's `source_url`. Do **not** click it yet.
2. In the EDGAR tab: search the company name or ticker. Click the company.
3. Filter by form type `8-K`. Find the filing whose date matches `stated`.
4. Open the filing index. Click the exhibit (EX-99.1, or EX-99 for Target).
5. Find the sentence in the excerpt. Confirm the numbers.
6. Now compare the URL in your browser's address bar to the `source_url` the reviewer printed. They must match exactly, character for character.
7. Match: press `v`. Note nothing.
8. Mismatch: press `q`. Do not continue that company. Paste both URLs to me. A single mismatch means the pipeline's URL construction is not trustworthy for that company, and every row in the batch is suspect until the cause is found.

---

## 9. Note vocabulary

Use these strings so the notes can be counted later. Add detail after a colon.

```
not guidance: result | growth rate | half year | relative | capital return | aspiration
metric fixed: was <old>
numbers fixed: <what changed>
period fixed: was <old>
false floor
outcome mismatch: wrong period | wrong metric | is guidance | wrong column
ack mismatch
conflict loser
point from table; bullet has ±50bps
missed guidance in <accession>: <metric> <value> <period>
```

---

## 10. Stop and tell me if

- More than 1 in 10 rows in a company are wrong on metric or period. That is a pipeline pattern, not review work.
- The same mistake appears three times.
- A hand-verify URL does not match.
- A worse row's outcome looks better than guided, or vice versa, more than once.
- Anything makes you unsure for more than a minute. `s` it, note `unsure: <why>`, and keep going. Skipped rows are listed at the end.

---

## 11. Commit after each company

```
git add data/review/0000027419.csv
git commit -m "Review: Target"
git push
```

Then NVIDIA (`0001045810`), then Salesforce (`0001108524`). If you stop mid-file, commit as `"Review: Salesforce, partial"`; the reviewer resumes.

---

## 12. Worked examples from your data

**Target, EPS GAAP 1.55 to 1.75, Q1 FY2020, reported 0.56 on 2020-05-20, worse.**
Excerpt is from the March 2020 release, forward-looking, named quarter. Outcome is May, past tense, same metric, same quarter. Real COVID gap. `y`. Acknowledgement blank: Target withdrew rather than acknowledged, which is the finding.

**Target, operating margin GAAP at least 8.0, FY2022, reported 3.7.**
Outcome excerpt: "Fourth quarter operating income margin rate was 3.7 percent". That is Q4, the target is the full year. `e`, clear `outcome.reported_value` and `outcome.reported_at`, note `outcome mismatch: wrong period, Q4 not FY`. Then `y`. Row becomes `unresolved`.

**NVIDIA, other income and expense -55 to -55, Q3 FY2021.**
Excerpt: "expected to be an expense of approximately $55 million". Negative is right. Outcome from "(50)" is -50, inside a ±? No, point target, so 0.5% tolerance; -50 vs -55 is `missed`, direction better (less expense; other income and expense is higher-is-better). `y`.

**Salesforce, EPS GAAP -0.44 to -0.42, FY2022, reported 1.48.**
Both excerpts clean, parentheses on EPS correctly negative. Far better than guided, from investment gains. `y`. It is a `missed` / `better`.

**Salesforce, tax rate 40, Q4 FY2020, outcome "is expected to be approximately 72%".**
After the outcome fix this should already be cleared. If it survives: `e`, clear outcome, note `outcome mismatch: is guidance`.

**Salesforce, section row, header `Q4 FY21 | FY21 | Q1 FY22 | FY22`, value from column 2, period says `Q1 FY22`.**
Column 2 is FY21. `e`, `assumption.target_period` = `FY2021`, note `period fixed: was Q1 FY22`. Then `y`.

**Target, comparable sales 3.4, Q3 FY2019, excerpt "in line with the second quarter's 3.4 percent".**
Relative statement with the absolute figure printed. Borderline. Keep it: the company did name a number for a named quarter. `y`, note `relative but absolute printed`.

**Any row, `empty_block=true`.**
`s`.

---

## 13. What to send back when done

Per company: rows approved, rejected, skipped; hand-verified count; and the note counts by vocabulary string. Plus anything from section 10. That is the input for Phase 3.
