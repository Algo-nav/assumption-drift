# The database

Postgres is the store of record. `db/schema.sql` is the schema; `research_record/store.py` is the only code that
reads or writes it. The CSVs stay as the export and review format.

    STORE=files       the stages read and write the per-CIK files (the default)
    STORE=postgres    the same stages read and write these tables
    DATABASE_URL      the database (the environment, or a line in the git-ignored .env)
    STORE_SCHEMA      the schema inside it (default public)

    python scripts/load_published.py          # files -> database, then reads everything back and compares
    python scripts/export_release.py          # database -> release.jsonl, must be byte-identical to the published file
    python -m pipeline.job run --cik CIK --kind build|refresh [--dry-run]

Rules the store keeps:

- An empty CSV cell is `NULL`; `NULL` is an empty cell on the way out. Nothing else is rewritten.
- Excerpts, sentences and model answers are stored exactly as given. No trimming, no Unicode normalisation.
- A cell that its column could not give back unchanged (`1e3` in a number column, `2024-2-1` in a date column) is
  refused, with the row and the column named. It is never rounded or reformatted to fit.
- A CSV column or a sidecar or archive field with no column below is refused, not dropped.

## Tables

| Table | One row is | Key |
|---|---|---|
| `companies` | a company, its fiscal calendar, its build status and how far its refresh has read | `cik` |
| `filings` | a cached SEC document (the sidecar; the HTML itself stays in `data/raw/`) | `accession` |
| `candidates` | a sentence or block stage 02 picked out, verbatim | `candidate_id` |
| `statements` | a guidance statement: the review-queue row, with stage 03's draft and what stage 04 found | `record_id` |
| `llm_calls` | a model call: its fingerprint, answer, tokens and cost; also a cached token count | `id` |
| `jobs` | one build or refresh of one company | `id` |
| `flags` | a reader's report about a published row | `id` |
| `stage_diagnostics` | a by-product row of stage 03 or 04 (a reject, a skip, an empty outlook block) | `cik, source, seq` |

`stage_diagnostics` is not one of the seven objects the schema was asked for. It is there because four by-product files
have no place in the seven and stage 05 reads one of them back.

## Column map

Every column of every file the pipeline writes, and where it is in the database.

### `data/review/{cik}.csv` -> `statements`

One row per CSV row; `queue_seq` is the row's position in the file. Read back in that order.

| CSV column | `statements` column | Type |
|---|---|---|
| `record_id` | `record_id` | text, primary key |
| `company` | `company` | text |
| `ticker` | `ticker` | text |
| `cik` | `cik` | text |
| `claim` | `claim` | text |
| `assumption.text` | `assumption_text` | text |
| `assumption.metric` | `metric` | text |
| `assumption.target_low` | `target_low` | numeric |
| `assumption.target_high` | `target_high` | numeric |
| `assumption.unit` | `unit` | text |
| `assumption.target_period` | `target_period` | text |
| `assumption.stated_at` | `stated_at` | date |
| `assumption.evidence.source_url` | `assumption_source_url` | text |
| `assumption.evidence.accession_number` | `assumption_accession` | text |
| `assumption.evidence.filing_type` | `assumption_filing_type` | text |
| `assumption.evidence.filed_at` | `assumption_filed_at` | date |
| `assumption.evidence.fetched_at` | `assumption_fetched_at` | timestamptz |
| `assumption.evidence.content_sha256` | `assumption_content_sha256` | text |
| `assumption.evidence.excerpt` | `assumption_excerpt` | text, verbatim |
| `outcome.reported_value` | `outcome_reported_value` | numeric |
| `outcome.reported_at` | `outcome_reported_at` | date |
| `outcome.evidence.source_url` | `outcome_source_url` | text |
| `outcome.evidence.accession_number` | `outcome_accession` | text |
| `outcome.evidence.filing_type` | `outcome_filing_type` | text |
| `outcome.evidence.filed_at` | `outcome_filed_at` | date |
| `outcome.evidence.fetched_at` | `outcome_fetched_at` | timestamptz |
| `outcome.evidence.content_sha256` | `outcome_content_sha256` | text |
| `outcome.evidence.excerpt` | `outcome_excerpt` | text, verbatim |
| `invalidation_condition` | `invalidation_condition` | text |
| `status` | `status` | text (always `open` in the queue) |
| `acknowledged_at` | `acknowledged_at` | date |
| `acknowledgement_evidence.source_url` | `ack_source_url` | text |
| `acknowledgement_evidence.accession_number` | `ack_accession` | text |
| `acknowledgement_evidence.filing_type` | `ack_filing_type` | text |
| `acknowledgement_evidence.filed_at` | `ack_filed_at` | date |
| `acknowledgement_evidence.fetched_at` | `ack_fetched_at` | timestamptz |
| `acknowledgement_evidence.content_sha256` | `ack_content_sha256` | text |
| `acknowledgement_evidence.excerpt` | `ack_excerpt` | text, verbatim |
| `days_to_falsifiable` | `days_to_falsifiable` | integer |
| `days_to_acknowledged` | `days_to_acknowledged` | integer |
| `last_reviewed_at` | `last_reviewed_at` | date |
| `reviewer` | `reviewer` | text |
| `approved` | `approved` | boolean |
| `hand_verified` | `hand_verified` | boolean |
| `reviewer_note` | `reviewer_note` | text |
| `conflict` | `conflict` | boolean |
| `empty_block` | `empty_block` | boolean |
| `ack_pending_review` | `ack_pending_review` | boolean (empty, `false` and `true` are three values) |
| `change_pending_review` | `change_pending_review` | boolean (the same) |
| `aid_proposed_status` | `aid_proposed_status` | text |
| `aid_capture_method` | `aid_capture_method` | text |
| `aid_heading` | `aid_heading` | text |
| `aid_lead_in` | `aid_lead_in` | text |
| `aid_table_header` | `aid_table_header` | text |
| `aid_value_column` | `aid_value_column` | text |
| `aid_outcome_note` | `aid_outcome_note` | text |
| `aid_flag_note` | `aid_flag_note` | text |
| `aid_withdrawal_note` | `aid_withdrawal_note` | text |
| `aid_verify` | `aid_verify` | text |
| `aid_verify_reason` | `aid_verify_reason` | text |
| `aid_verify_class` | `aid_verify_class` | text |
| `aid_suggested_note` | `aid_suggested_note` | text |
| `aid_ack_proposed_at` | `aid_ack_proposed_at` | text |
| `aid_ack_proposed_excerpt` | `aid_ack_proposed_excerpt` | text |
| `aid_ack_proposed_url` | `aid_ack_proposed_url` | text |
| `aid_ack_proposed_evidence` | `aid_ack_proposed_evidence` | text (JSON, as written) |
| `aid_proposed_change` | `aid_proposed_change` | text (JSON, as written) |
| `aid_derived_snapshot` | `aid_derived_snapshot` | text (JSON, as written) |
| `aid_context` | `aid_context` | text |

Four `statements` columns have no CSV column. They are worked out, and this is how:

| Column | Where it comes from |
|---|---|
| `review_decision` | From the row: `v` approved and hand-verified, `y` approved, `n` not approved with a reviewer note. `s` (held) leaves no mark in the CSV: the loader sets it on a row that is in a published company's queue, is not an outlook-block flag, is not approved and has no note; a later write keeps it while the row stays undecided. An outlook-block flag has no decision. |
| `review_tier` | `hand` on every row with a decision. Nothing in the files records a machine-tier decision, so nothing is `machine` yet. |
| `published_status` | For an approved row, `rubric.resolve_record` with `reviewed=True`: the status the release carries. The same call `06_publish` makes; `export_release.py` fails if the two disagree. |
| `change_direction` | **No source.** Nothing in any file says whether a statement initiated, raised, lowered, maintained or withdrew guidance. `NULL` on every row. |

`reviewed_at` in the brief is `last_reviewed_at`, the published row's own field. There is no separate column.

### `data/release/assumption_drift.jsonl` (the site's `data/release.jsonl`) -> `statements`

The release is not stored a second time. It is the approved rows of `statements`, built by `06_publish.build_records`.
Each nested field is the review column of the same dotted name above (`assumption.evidence.excerpt` ->
`assumption_excerpt`), with one exception: the release's `status` is `published_status`, not `status`.

### `data/candidates/{cik}.jsonl` -> `candidates`

| Key | Column |
|---|---|
| (none: it is `{c or b}-{cik}-{accession without dashes}-{char_start}`, stage 03's id for the candidate) | `candidate_id` |
| `cik` | `cik` |
| `accession` | `accession` |
| (position in the file) | `seq` |
| `filing_type` | `filing_type` |
| `filed_at` | `filed_at` |
| `char_start` | `char_start` |
| `char_end` | `char_end` |
| `sentence` | `text`, verbatim |
| `text_version` | `text_version` |
| `capture_method` | `capture_method` |
| `heading`, `lead_in`, `context_before`, `context_after`, `block_lines`, `block_end`, `table_header` | `meta` (json), under the same keys |

### `data/drafts/{cik}.jsonl` -> `statements.draft`

The whole row, as stage 03 wrote it, in `draft` (json); `draft_seq` is its position in the file. `draft_id` is also
`record_id`.

| Key | Where |
|---|---|
| `draft_id` | `record_id`, and `draft->>'draft_id'` |
| `cik` | `cik`, and `draft->>'cik'` |
| `ticker`, `company`, `custom_id`, `capture_method`, `char_start`, `heading`, `lead_in`, `table_header`, `value_column`, `item_index`, `conflict`, `parens_note`, `evidence_lines` | `draft`, under the same key |
| `assumption` (`text`, `metric`, `target_low`, `target_high`, `unit`, `target_period`, `stated_at`, `evidence.*`) | `draft->'assumption'` |

The typed assumption columns of `statements` hold the queue's values, which a reviewer may have corrected; `draft`
holds what the model and the guards produced.

### `data/outcomes/{cik}.jsonl` -> `statements.outcome_found`

The whole row in `outcome_found` (json); `outcome_seq` is its position.

| Key | Where |
|---|---|
| `draft_id` | `record_id`, and `outcome_found->>'draft_id'` |
| `outcome` (`reported_value`, `reported_at`, `evidence.*`) | `outcome_found->'outcome'` |
| `outcome_reason` | `outcome_found->>'outcome_reason'` |
| `acknowledgement` (`acknowledged_at`, `evidence.*`) | `outcome_found->'acknowledgement'` |
| `withdrawal` (`withdrawn_at`, `period_close`, ...) | `outcome_found->'withdrawal'` |

### The by-product files -> `stage_diagnostics`

One row per line, the whole line in `body` (json), `seq` its position.

| File | `source` | `custom_id` | `record_id` | `reason` | Other keys, in `body` |
|---|---|---|---|---|---|
| `data/drafts/{cik}.rejects.jsonl` | `drafts.rejects` | `custom_id` | | `reason` | `item` |
| `data/drafts/{cik}.skipped.jsonl` | `drafts.skipped` | `custom_id` | | `reason` | `kept`, `dropped`, `printed`, `conflict`, `also_kept` |
| `data/drafts/{cik}.empty_blocks.jsonl` | `drafts.empty_blocks` | `custom_id` | `block_id` | `reason` | `cik`, `ticker`, `company`, `accession`, `filed_at`, `char_start`, `heading`, `lead_in`, `table_header`, `block_lines`, `evidence` |
| `data/outcomes/{cik}.rejects.jsonl` | `outcomes.rejects` | | `draft_id` | `reason` | `output` |

### `data/raw/{cik}/{accession}.meta.json` -> `filings`

| Sidecar field | Column |
|---|---|
| `accession` | `accession` |
| `cik` | `cik` |
| `filing_type` | `form` |
| `filed_at` | `filed_at` |
| `final_url` | `url` |
| `fetched_at` | `fetched_at` |
| `requested_url` | `requested_url` |
| `doc_type` | `doc_type` |
| `items` | `items` (json) |
| `http_status` | `http_status` |
| `content_type` | `content_type` |
| `bytes` | `bytes` |
| `raw_sha256` | `raw_sha256` |
| `content_sha256` | `content_sha256` |

The document itself (`{accession}.html`) is not in the database. It stays in `data/raw/`, which is a re-fetchable
cache; `raw_sha256` says whether the copy on disk is the one that was fetched.

### `data/batches/{step}.raw.jsonl` -> `llm_calls`

One row per line; `archive_seq` is the line number, which is what makes loading the file twice a no-op.

| Field | Column |
|---|---|
| (the file name) | `step` |
| `custom_id` | `custom_id` |
| `status` | `status` |
| `text` | `response_text`, verbatim; `response_sha256` is its sha256 |
| `input_tokens` | `input_tokens` |
| `output_tokens` | `output_tokens` |
| `error` | `error` |
| `batch_id` | `batch_id`; `kind` is `sync` or `canary` when it says so, else `batch` |
| `fingerprint` | `fingerprint`, and `request_sha256` (the fingerprint is the hash of the request; there is no second one) |

Not in the file, filled in by the loader: `model` (config.yaml's `llm.model`), `usd` (the tokens at config.yaml's
prices, batch or full by `kind`), `created_at` (from `ledger.jsonl`, below), `cik` (from the `custom_id`).

### `data/batches/ledger.jsonl` -> `llm_calls.usd`, `llm_calls.created_at`

Not stored as lines. Its money is the `usd` on each call, and `pipeline.llm.Ledger` reads the ledger back from
`llm_calls` under `STORE=postgres`.

| Line | Where |
|---|---|
| `kind` = `canary` or `sync`: `step`, `usd`, `ts` | the call's own row: `usd`, `created_at` (the n-th such line of a step is the n-th such answer in its archive) |
| `kind` = `ended`: `batch_id`, `usd`, `ts` | the batch's rows: their `usd` sums to it, `created_at` is `ts` |
| `kind` = `submitted`: `usd`, `requests` | while a batch is pending: `usd` on its `pending` rows. For a batch that has ended: **not kept** (see below) |
| `succeeded` | the count of the batch's rows with `status` = `succeeded` |
| `note` | **not kept** (see below) |

### `data/batches/token_cache.json` and `{step}.state.json` -> `llm_calls`

| File | Rows |
|---|---|
| `token_cache.json` (fingerprint -> input tokens) | `kind` = `count`, `status` = `counted`: `fingerprint`, `input_tokens` |
| `{step}.state.json` (`batch_id`, `requests`, `fingerprints`) | `status` = `pending`: one row per request of the batch, with its `custom_id`, `fingerprint` and `batch_id` |

### `data/manifest.json` and `pipeline/config.yaml` -> `companies`

| Source | Column |
|---|---|
| config `cik`, `ticker`, `name`, `sector` | `cik`, `ticker`, `name`, `sector` |
| config `fiscal_year_end_month`, `fiscal_year_named_for` | the same names (the fiscal year end rule) |
| config `tax_rate_parens_negative`, `publish`, `unpublished_reason` | the same names |
| manifest `companies.{cik}.last_filing_date` | `last_filing_date` |
| manifest `companies.{cik}.accessions_on_last_date` | `accessions_on_last_date` (json) |
| manifest `processed_through` | `last_refreshed_at`, on every company in the manifest |

`status` is `built` for a published company that has a review queue and `config` for the rest. `first_built_at` is the
earliest `last_reviewed_at` in the company's queue, the day stage 05 first queued a row for it.

## What does not map

Reported, not dropped: each of these is still in its file.

| What | Why it has no column | Count |
|---|---|---|
| `data/spend_ledger.jsonl`, the lines for 2026-10-15 and 2026-10-28 | They are the weekly runner's spend. The runner's archive is not on this machine, so there are no calls to put the money on. The eight lines for 2026-10-17 are the sum of that day's `llm_calls` by step, to the cent, and need no table. | 6 lines, $0.147 |
| `data/batches/ledger.jsonl`, `submitted` lines of batches that have ended | A reservation at the worst case, replaced by the actual cost when the batch ended. | 44 lines |
| `data/batches/ledger.jsonl`, `note` | Free text on two lines about one batch cancelled by hand. | 2 lines |
| `data/audit_allowlist.csv` (`record_id`, `reason`) | Read only by `scripts/audit_outcomes.py`. It would fit as one text column on `statements`. | 9 rows |
| An archive line with no `fingerprint` key at all | Loaded with `fingerprint` `NULL`, which is what the pipeline already reads it as. A line with the key missing and a line with it null are the same row. | 30 lines |

Not rows, and not in the database by design: the cached HTML in `data/raw/`, `data/release/assumption_drift.parquet`
(built from the same records as the jsonl), `data/_work/new_filings.json` (one run's work file), the review dumps
(`data/review/*.txt`, `data/refresh/`).

## What the loader assumes

- Every archived answer came from config.yaml's `llm.model`. The archive does not record the model.
- A call's cost is its tokens at config.yaml's current prices. For the archive on this machine that reproduces
  `ledger.jsonl` exactly: every batch, and every canary and synchronous call in order.
- The 15 published companies are loaded by default. `--all` adds the unpublished ones that have files (JPM, MU and the
  three with candidates only); until then their review queues are only in `data/review/`.
