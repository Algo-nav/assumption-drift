---
license: cc-by-4.0
language:
- en
tags:
- finance
- sec-filings
- guidance
- provenance
size_categories:
- n<1K
pretty_name: assumption-drift
---
# assumption-drift

## What this is

NVIDIA Corporation, Salesforce, Inc. and Target Corporation made numeric forward guidance statements in their own SEC filings, filed between
January 2019 and September 2026. This dataset pairs each one with what the company later reported for that same metric
and period, and records whether the company ever acknowledged the gap when the guidance was missed.
It is a problem statement in data form, not a model and not a demonstration of one.

## How a row is built

A row starts as a sentence or a table in an 8-K, 10-K or 10-Q filing that names a metric, a number or
range, and a period. A later filing reporting the same metric for the same period supplies the outcome,
when one exists. Every candidate row was drafted by a language model reading the filing text.

## The resolution rubric

A row resolves mechanically from its own numbers, once the period has closed and an outcome exists:

- **met**: the reported value falls inside the guided range, endpoints included (a single point value
  is met within half a percent of itself)
- **missed**: the reported value falls outside the guided range, either above it (a beat: the company
  did better than it said) or below it (a shortfall: the company did worse)
- **withdrawn**: the company explicitly withdrew or suspended the guidance in a later filing, before
  the period closed
- **unresolved**: the period has not closed yet, or no later filing reports the metric

Acknowledged means a later 8-K, 10-K or 10-Q states the gap in so many words: "below", "short of" or
"did not meet" for a shortfall, "exceeded" or "above the high end" for a beat.

As of 2026-09-27, the release holds **614 rows**: 103 met, 288 missed, 221 unresolved, 2 withdrawn.
Of the misses, 217 were a beat and 71 a shortfall; median days from the guidance to the
filing that made it checkable was 92, and
97% of misses were never acknowledged in a later filing.

By company:

| company | rows | met | missed | withdrawn | unresolved | never-acknowledged share |
| --- | --- | --- | --- | --- | --- | --- |
| NVIDIA Corporation | 212 | 58 | 79 | 0 | 75 | 100% |
| Salesforce, Inc. | 320 | 18 | 171 | 0 | 131 | 99% |
| Target Corporation | 82 | 27 | 38 | 2 | 15 | 79% |

![Missed rows: when the gap was acknowledged](figures/falsifiable_vs_acknowledged.png)
![Beats get mentioned. Shortfalls do not.](figures/acknowledgement_by_company.png)
![Resolution by fiscal year](figures/resolution_by_fiscal_year.png)
![Miss magnitude](figures/miss_magnitude.png)

## Provenance guarantee

Every `source_url` in this dataset returned a 200 from `www.sec.gov` or `efts.sec.gov`; no row was
built from a URL that was not actually fetched. Every excerpt is the exact text taken from that
filing. Its provenance hash is the sha256 of the extracted text, not of the raw page: SEC serves a
per-request script tag inside the raw HTML that differs on every fetch of the same document, so only
the extracted text is stable enough to hash and check again later. Every row was independently
checked by a person against the cached filing before it was approved, and at least ten percent of the
approved rows for each company were separately re-found on EDGAR by hand, their URL compared to the
one the pipeline used.

## Known limitations

- This covers three companies. It is a pilot, not a survey of the market.
- The pipeline does not claim to capture every guidance statement a company ever made; it captures
  the ones its patterns and its model caught.
- A beat is recorded as missed, the same as a shortfall, because the underlying assumption (the
  guided range) was wrong either way. Which direction it missed in is reported separately, not folded
  into the status.
- A one-sided floor ("at least X") resolves as met on any reported value above the floor. There is no
  ceiling to beat against, so a floor's beats are not measured; only shortfalls are.
- The search for an acknowledgement covers 8-K, 10-K and 10-Q filing text only. A company that only
  addressed a miss on an earnings call, and never wrote it into a filing, is not found: that text is
  not in EDGAR.
- An outcome that would come from a multi-column table whose header could not be confidently matched
  to a period is held back as unresolved rather than guessed at.

## How to cite

If you use this dataset, please cite it as:

    Navneet (2026). assumption-drift. https://huggingface.co/datasets/Nav772/assumption-drift. Accessed 2026-09-27.

## Licence

The dataset is released under CC-BY-4.0. The code that built it is released separately under MIT.
