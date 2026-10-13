# What 12 companies guided in their SEC filings, and what happened next

A dataset of 1,134 numeric guidance statements from SEC filings, each matched to the figure the company later reported and to any later filing that referred back to it. Plus what it took to build it with a small model, a lot of code guards, and a human reading every row.

- Dataset (CC-BY-4.0): [Nav772/assumption-drift](https://huggingface.co/datasets/Nav772/assumption-drift)
- Page with every row: [Space](https://huggingface.co/spaces/Nav772/assumption-drift)
- Code (MIT): [github.com/Algo-nav/assumption-drift](https://github.com/Algo-nav/assumption-drift)

## The question

Companies put numbers in their filings about the future. "We expect fiscal 2025 capital expenditures to be in the range of $60-65 billion." A few quarters later the number is known. Does any later filing ever go back and say what happened to the earlier number?

I wanted the answer as a table, not an anecdote. So for 12 large US companies (NVIDIA, Meta, Amazon, Microsoft, Broadcom, Qualcomm, AMD, Adobe, Salesforce, Target, Home Depot, Lowe's) I took every numeric forward-looking figure in their 8-K earnings releases, 10-Ks and 10-Qs since 2019, matched each to the reported outcome for that metric and period, and then searched every subsequent filing for a sentence that refers back to the guided figure.

## What the table says

| | |
|---|---|
| Guidance statements | 1,134 |
| Resolved (outcome reported) | 649 |
| Met (inside the guided range) | 208 |
| Missed | 441 |
| Missed, worse than guided | 100 |
| Missed, better than guided | 341 |
| Missed and never acknowledged in a later filing | 432 (98%) |
| Acknowledgements, all on beats | 9 |

Zero of the 100 worse-side misses was ever referred to again in a filing. The nine acknowledgements are all beats, and eight of the nine are one company (Target), which writes sentences like "above the high end of the Company's guidance range" when it beats.

One example of the shape the data has. Meta's 2025 capex guidance, as printed:

| Filing | Range |
|---|---|
| January 2025 | $60 to 65 billion |
| April 2025 | $64 to 72 billion, "increased from our prior outlook of $60-65 billion" |
| July 2025 | $66 to 72 billion, "narrowed from our prior outlook of $64-72 billion" |
| October 2025 | $70 to 72 billion, "increased from our prior outlook of $66-72 billion" |
| Reported, January 2026 | $72.22 billion |

Each revision cites the one before it. No filing after the year closed refers back to any of them.

## Why "acknowledged" means something narrow

An acknowledgement, in this dataset, is a sentence written after the outcome was reported that refers to the guided figure. Meta's April sentence above is not one: it was written while the year was open, and it is recorded as a new guidance row. The counts are therefore strict by construction. A later release will record mid-period revisions separately, and the acknowledged numbers will move when it does. The page says so.

A beat also counts as a miss. If a company guides $5 billion and reports $6 billion, the assumption failed; the fact that it failed in the pleasant direction is recorded in a separate field (`direction`), computed from each metric's polarity (revenue up is better, expenses up is worse, tax rate up is worse). The headline uses the worse-side count for exactly this reason.

## How it is built

The pipeline is six scripts and a thin schema. The interesting part is where the model sits and where it doesn't.

**Fetch.** EDGAR's full index, one request at a time under the SEC's rate limit, with the SEC-required User-Agent. Raw HTML is cached; the text is extracted and hashed, and the hash travels with every row so the excerpt can be checked against the source later. (The SEC injects a random script tag into pages, so hashing the HTML is useless; hash the extracted text.)

**Candidates.** Sentences and table rows with a forward-looking verb and a number, plus the heading and lead-in sentence above them, because "Second quarter revenue guidance of approximately $6.6 billion" names no period on its own; the heading "Second Quarter Fiscal Year 2021 Business Outlook" does.

**Structure.** A small model (Claude Haiku 4.5, via the batch API) turns each candidate into metric, period, low, high, unit and basis. Total model spend for 14 companies and seven years of filings was $11.05. The model proposes; it does not decide.

**Guards.** Most of the code is here, and most of it was written after looking at what the model got wrong:

- "$6.7 billion, plus or minus $200 million" is a range of 6.5 to 6.9, not two endpoints.
- "(20%)" in a tax provision footnote is a benefit, so a rate of minus 20.
- "Fiscal year ending Jan. 31, 2020" is Lowe's fiscal 2019, not 2020. Every retailer names its years differently; the config carries it per company.
- "Operating margin is expected to increase 310 to 340 basis points" is a change, not a level, and is not guidance for the metric.
- "An approximate $0.20 reduction to EPS due to macroeconomic headwinds" is a component of the guide, not an EPS floor.
- "Adjusted EBITDA of approximately 61 percent of revenue" is not a metric we track, however confidently the model maps it to operating margin.
- A 2019 outlook table with a prior-year results column next to the estimates column. The cause turned out to be smaller than it looked: the range guard would not accept a dash after a unit letter, so '$4.4B - $5.2B' was rejected as having no range language. The revenue rows are back; the EPS and shipment rows in the same tables are still missing because block capture stops at the first blank-line gap. Both are in the backlog.
- Two drafts for the same metric and period with different numbers. The old rule kept the sentence and silently dropped the table row, which once replaced Qualcomm's $9.2 to 10.2 billion quarterly guide with a $150 million Huawei component mentioned in the same release. Now both are kept, flagged, and the reviewer records which is the guide.
- A sentence in the Business Outlook section of the next release, "Operating margin of approximately 13.0%", is next year's guide, not this year's result, however neatly it matches the metric. The outcome matcher took it anyway for a while.

**Verify.** A second model pass answers yes or no on whether the structured row matches the sentence, with a one-line reason and a class. I overruled it in both directions; the numbers are in the next section. Its value is as a flag, not a judge.

**Review.** Every row that was published was read by me against the filing. The review tool shows the sentence, the EDGAR link, the proposed outcome and the verifier's reason, and takes one key: approve, reject with a note, edit, skip, or hand-verify (which means I opened EDGAR and checked the sentence by URL; 94 of the 1,134 published rows, 8.3%). Rejections carry a prefix the tooling understands ("not guidance: component figure", "duplicate", "wrong period") so the next regeneration doesn't resurrect them. After the first few companies, review moved to a dump-and-apply loop: write every pending row to a text file, decide them all in one sitting, apply the decisions through the same functions the keys use.

**Publish.** Parquet and JSONL to the dataset repo, a card with the counts and limitations, and a static page with a per-company table, an SVG chart of every missed row (hover for the row, click to jump to it), and the row cards themselves with both filings linked.

## What the model got wrong

The model proposes; the guards and a reader decide. These are the counts for the 12 published companies, from the pipeline's own reject, skip and review files.

| Stage | Count |
|---|---|
| Candidate blocks sent to the model | 2,627 |
| Items the model returned | 2,485 |
| Rejected by code guards before anyone read them | 461 (18.6%) |
| Dropped as duplicates with equal numbers | 725 |
| Point dropped because a range for the same item was kept | 57 |
| Pairs with different numbers, both kept and flagged | 52 |
| Drafts that reached review | 1,255 |
| Rejected in review | 121 (9.6%) |
| Published | 1,134 |

The guards' most common reasons: a dollar level whose sentence describes a change (138), a value that is not in the evidence text (99), a range stated with no range language (91), no number at all (48), a unit that does not match the metric's kind (37). The last two columns of that list are the model inventing a figure or a unit; the first three are the model reading a real figure and mislabelling it.

Of the 121 rejected in review, 80 were not guidance (component figures the model took for the guide, changes in basis points, the fixed non-GAAP tax rate convention), 14 were the wrong fiscal year, 14 lost a conflict with another draft for the same metric and period, 6 were duplicates, 7 other. Of the 1,134 approved rows, 8 had a field corrected by hand, 7 of them the period. That figure is a floor: the drafts file is rewritten on each run, so an edit the pipeline later absorbed does not show as an edit.

The outcome matcher made 607 proposals on published rows. 10 were rejected as the wrong line (the next year's guide, the prior year's figure in an "X and Y, respectively" sentence). 60 were later changed, most by a precision fix (7.1 became 7.103) once the guards learned to read the table cell. 95 outcomes were attached by hand or by a later rule with no proposal behind them.

The verifier pass agreed with the reviewer on 89% of rows (1,084 of 1,218). As a predictor of rejection it is weak: it said no on 30 of the 121 rows that were rejected and on 43 that were approved, so a "no" is right 41% of the time and catches a quarter of the real rejections. It earned its keep by pointing at 9 rows whose period or sign was wrong, not by judging.

The residual error rate after review is not measured. 94 published rows (8.3%) were opened on EDGAR and checked by URL; whether a reviewer who read 1,255 rows in five weeks missed things is the question I cannot answer from inside. Errors reported by readers will be listed in the dataset discussions and corrected in the next release.

Model spend: $11.05 in total across 14 companies, of which $5.90 for structuring, $2.97 for verification, $2.11 for outcome matching, and 7 cents for acknowledgements and withdrawals.

## The schema

The dataset's row is a `research_record`: an assumption (metric, period, range, the sentence it came from, the filing it came from), an outcome (reported value, sentence, filing), an optional acknowledgement (sentence, filing, date), and a status resolved by a small rubric: met, missed, withdrawn, unresolved. Every evidence object carries a source URL, accession number, filing date, fetch date and content hash.

It is a thin Pydantic model, deliberately. The thing I was curious about is whether this shape, an assumption with a falsification date and evidence on both ends, is the right unit for keeping a record of what someone believed. Companies turn out not to keep it for their own guidance. That was the finding.

## Limitations

- Filings only. Guidance given on earnings calls is not in EDGAR and is not here. Microsoft, for example, only started putting its outlook table into the 8-K in 2024.
- Point targets with no printed tolerance ("approximately $6.6 billion") are taken as the printed number. Broadcom supplies most of the small misses on that rule: it guides to a point ('approximately $14.0 billion') and reports $14,054 million.
- Floors ("at least $X") can only be met or missed, never beaten.
- Home Depot and Lowe's do not print margins or tax rates as reported lines in their releases; those outcomes have to be derived from two lines in the 10-K and most are still unresolved.
- Preliminary estimates filed after a quarter closes (NVIDIA, August 2022) are guidance under the current rule and will be flagged separately.
- Apple, Alphabet and Costco issue no numeric guidance in filing text at all. That is a finding too. JPMorgan does, in its 8-K earnings presentations (net interest income, adjusted expense), but those metrics are outside this release's schema.

## What I'd like from you

The data card lists the open parser issues with fixtures (accession numbers and the exact sentence). If you work with filings and the fix is obvious to you, a pull request is welcome. If you think the rubric is wrong (beats as misses, the strict acknowledgement test), the dataset discussions tab is the place; I'd rather be corrected there than find out from a column.

I'm a data scientist in Bangkok with a few years of NLP over financial documents. This started as a weekend question. It took five weeks.
