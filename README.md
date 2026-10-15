# assumption-drift

Write-up: how it was built and what it found, in [WRITEUP.md](WRITEUP.md).

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

Phases 0 through 3 are done, on the three-company pilot in `pipeline/config.yaml`. The 30-company
run is not started; see `BACKLOG.md` for extractor fixes to do first.

## Running what exists

```
.venv/bin/python -m pipeline.01_fetch        # filings into data/raw/ (gitignored)
.venv/bin/python -m pipeline.02_candidates   # candidate sentences into data/candidates/

# Phase 2. Each is a dry run until --submit: it counts tokens and prints the projected cost.
.venv/bin/python -m pipeline.03_structure            # Haiku: candidates -> draft assumptions (8-K only)
.venv/bin/python -m pipeline.04_outcomes             # Haiku: later outcomes and acknowledgements
.venv/bin/python -m pipeline.05_review               # data/review/{cik}.csv for a human to check
.venv/bin/python -m pipeline.03b_verify              # Haiku: a second opinion on each review row, written back to the CSV
.venv/bin/python -m pipeline.03c_suggest             # code only: a suggested note for each row 03b_verify flagged "no"

.venv/bin/rr review data/review/{cik}.csv            # review the queue one row at a time in the terminal

# Phase 3. Dry run by default; --live also needs hf.user set in config.yaml.
.venv/bin/rr validate data/release/assumption_drift.jsonl   # every check a published row must pass
.venv/bin/rr stats data/release/assumption_drift.jsonl      # row counts, resolution, acknowledgement, per company
.venv/bin/python -m pipeline.06_publish --dry-run           # parquet, jsonl, card/README.md, card/figures/*.png
```

Companies, date range and the SEC User-Agent live in `pipeline/config.yaml`. SEC requests
are capped at 8 per second, under the SEC limit of 10.

The model steps use claude-haiku-4-5 through the Message Batches API and nothing else. They need
`ANTHROPIC_API_KEY` in the environment, or in a git-ignored `.env` at the repo root. One budget
(`llm.budget_usd`) covers every step, and is checked against the worst case before anything is
submitted. Raw model output is archived in `data/batches/`, so no request is ever paid for twice.

## Weekly refresh

`.github/workflows/weekly-refresh.yml` runs `scripts/weekly_refresh.sh` every Saturday at 06:00 UTC (and on demand, with a
spend cap and an optional `back_days` for a test). It reads each company's new EDGAR filings, drafts the new guidance, has the
checker look at the new rows, attaches outcomes, acknowledgements and withdrawals to the rows already in the queue, and advances
`data/manifest.json` only if every stage succeeded. Model spend is capped for the whole run (`MAX_USD`, default 1.00) and every
stage's spend is appended to `data/spend_ledger.jsonl`.

Nothing is published without review. The job never pushes to `main` and never runs `06_publish`: it pushes a `refresh/<date>`
branch and opens an issue with the counts, the spend and the branch name. Ack and withdrawal findings on a row someone has
already reviewed arrive as proposals, never as overwrites.

To review a refresh, read the branch as it is; there is nothing to unzip. `data/refresh/<date>/summary.json` is the summary, and
`<ticker>_new.txt` and `<ticker>_pending.txt` are the cards for the new rows and the new proposals. Either review in the terminal
(`rr review data/review/<cik>.csv --filter no-note`, on the branch checked out) or read the dump and write a
`record_id,decision,note` CSV and apply it with `rr review data/review/<cik>.csv --apply decisions.csv`; the issue lists the
command for each dump.

To publish: merge the reviewed branch into `main`, then run `python -m pipeline.06_publish` (a dry run; `--live` as before),
review the diff to `data/release/`, `card/` and `space/`, and commit it.

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
