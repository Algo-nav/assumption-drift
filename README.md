# assumption-drift

Two things in one repo.

**`assumption-drift`** is a dataset. Each row is a numeric forward guidance
statement a public company made in its own SEC filing, paired with what the
company later reported for that same metric and period, and whether the company
ever acknowledged the gap.

**`research_record`** is a small Python package. It holds a schema for what a
written down research conclusion must contain to be checkable later, plus a
validator. The dataset rows are the schema's example corpus.

This is a problem statement in data form. It is not a product and not a model.

## Status

Phases 0 and 1 are done. Phase 2 is built and tested against a fake API but has not yet run
against the real one: it needs an Anthropic API key (see below).

## Running what exists

```
.venv/bin/python -m pipeline.01_fetch        # filings into data/raw/ (gitignored)
.venv/bin/python -m pipeline.02_candidates   # candidate sentences into data/candidates/

# Phase 2. Each is a dry run until --submit: it counts tokens and prints the projected cost.
.venv/bin/python -m pipeline.03_structure            # Haiku: candidates -> draft assumptions (8-K only)
.venv/bin/python -m pipeline.04_outcomes             # Haiku: later outcomes and acknowledgements
.venv/bin/python -m pipeline.05_review               # data/review/{cik}.csv for a human to check
.venv/bin/python -m pipeline.03b_verify              # Haiku: a second opinion on each review row, written back to the CSV

.venv/bin/rr review data/review/{cik}.csv            # review the queue one row at a time in the terminal
```

Companies, date range and the SEC User-Agent live in `pipeline/config.yaml`. SEC requests
are capped at 8 per second, under the SEC limit of 10.

The model steps use claude-haiku-4-5 through the Message Batches API and nothing else. They need
`ANTHROPIC_API_KEY` in the environment, or in a git-ignored `.env` at the repo root. One budget
(`llm.budget_usd`) covers every step, and is checked against the worst case before anything is
submitted. Raw model output is archived in `data/batches/`, so no request is ever paid for twice.

## Layout

```
research_record/   schema and resolution rules
pipeline/          EDGAR fetch, candidate extraction, review, publish
data/              cached filings, review queues, released files
card/              dataset card for Hugging Face
tests/             schema, rubric, validator, provenance
```

## Development

```
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest tests/ -q
```

## Licences

Code MIT, see `LICENSE`. Dataset CC-BY-4.0.
