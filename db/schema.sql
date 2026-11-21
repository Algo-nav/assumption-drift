-- assumption-drift: Postgres as the store of record.
--
-- Derived from research_record/schema.py (ResearchRecord, Assumption, Outcome, Evidence) and from the files the
-- pipeline writes today. db/README.md maps every CSV and JSONL column to a column here.
--
-- Applied by research_record.store.apply_schema(), which runs this file inside the schema named by STORE_SCHEMA
-- (default: public). Every statement is idempotent, so applying it twice changes nothing.
--
-- Conventions
--   * An empty CSV cell is NULL here, and NULL is an empty cell on the way out. Nothing else is rewritten.
--   * Excerpts, sentences and model answers are `text`, stored exactly as given: no trimming, no Unicode
--     normalisation, no newline conversion.
--   * Whole pipeline rows kept for the stages to read back (a draft, what 04 found) are `json`, not `jsonb`:
--     `json` keeps the text as written, so key order and number formatting survive.
--   * Targets and reported values are `numeric`, which keeps the digits as typed ("46.0" stays "46.0").

CREATE TABLE IF NOT EXISTS companies (
    cik                       text PRIMARY KEY CHECK (cik ~ '^\d{10}$'),
    ticker                    text NOT NULL UNIQUE CHECK (ticker ~ '^[A-Z0-9.\-]{1,10}$'),
    name                      text NOT NULL,
    sector                    text,
    -- The fiscal year end rule: the month the fiscal year ends in, and whether "FY2026" names the year it ends in
    -- or the year it starts in (pipeline.common.Company).
    fiscal_year_end_month     smallint CHECK (fiscal_year_end_month BETWEEN 1 AND 12),
    fiscal_year_named_for     text NOT NULL DEFAULT 'end' CHECK (fiscal_year_named_for IN ('end', 'start')),
    tax_rate_parens_negative  boolean NOT NULL DEFAULT false,
    publish                   boolean NOT NULL DEFAULT true,
    unpublished_reason        text,
    status                    text NOT NULL DEFAULT 'config'
                              CHECK (status IN ('config', 'requested', 'building', 'built', 'parked')),
    first_built_at            timestamptz,
    last_refreshed_at         timestamptz,
    -- How far the refresh has read EDGAR for this company (data/manifest.json): its newest filing of any wanted form,
    -- cached or not, and every accession filed on that date.
    last_filing_date          date,
    accessions_on_last_date   json
);

CREATE TABLE IF NOT EXISTS filings (
    accession       text PRIMARY KEY CHECK (accession ~ '^\d{10}-\d{2}-\d{6}$'),
    cik             text NOT NULL REFERENCES companies (cik),
    form            text NOT NULL,                 -- 8-K, 10-K, 10-Q
    filed_at        date NOT NULL,
    url             text NOT NULL,                 -- the URL that returned the document (the sidecar's final_url)
    fetched_at      timestamptz NOT NULL,
    requested_url   text,
    doc_type        text,                          -- EX-99.1, EX-99, 10-K, 10-Q
    items           json,                          -- the 8-K item numbers, as listed
    http_status     integer,
    content_type    text,
    bytes           bigint,
    raw_sha256      text CHECK (raw_sha256 ~ '^[0-9a-f]{64}$'),      -- of the bytes as fetched
    content_sha256  text CHECK (content_sha256 ~ '^[0-9a-f]{64}$')   -- of the extracted text; what evidence carries
);
CREATE INDEX IF NOT EXISTS filings_cik_idx ON filings (cik, filed_at);

CREATE TABLE IF NOT EXISTS candidates (
    -- The block id: 03_structure's custom_id for the candidate, {c|b}-{cik}-{accession without dashes}-{char_start}.
    candidate_id    text PRIMARY KEY,
    cik             text NOT NULL REFERENCES companies (cik),
    accession       text NOT NULL,
    seq             integer NOT NULL,              -- position in the company's candidate list, which the stages rely on
    filing_type     text NOT NULL,
    filed_at        date NOT NULL,
    capture_method  text NOT NULL CHECK (capture_method IN ('sentence', 'section')),
    char_start      integer NOT NULL,
    char_end        integer NOT NULL,
    text            text NOT NULL,                 -- the candidate's `sentence`, verbatim
    text_version    integer,
    -- Stage-02 metadata: heading, lead_in, context_before, context_after, block_lines, block_end, table_header.
    meta            json NOT NULL,
    UNIQUE (cik, seq)
);
CREATE INDEX IF NOT EXISTS candidates_cik_idx ON candidates (cik);
CREATE INDEX IF NOT EXISTS candidates_accession_idx ON candidates (accession);

CREATE TABLE IF NOT EXISTS statements (
    record_id                 text PRIMARY KEY CHECK (record_id ~ '^[0-7][0-9A-HJKMNP-TV-Z]{25}$'),
    cik                       text NOT NULL REFERENCES companies (cik),

    -- The research-record row, as the review queue holds it. Every field of a published row is here.
    company                   text,
    ticker                    text,
    claim                     text,
    assumption_text           text,
    metric                    text,
    target_low                numeric,
    target_high               numeric,
    unit                      text,
    target_period             text,
    stated_at                 date,
    assumption_source_url     text,
    assumption_accession      text,
    assumption_filing_type    text,
    assumption_filed_at       date,
    assumption_fetched_at     timestamptz,
    assumption_content_sha256 text,
    assumption_excerpt        text,
    -- Outcome fields.
    outcome_reported_value    numeric,
    outcome_reported_at       date,
    outcome_source_url        text,
    outcome_accession         text,
    outcome_filing_type       text,
    outcome_filed_at          date,
    outcome_fetched_at        timestamptz,
    outcome_content_sha256    text,
    outcome_excerpt           text,
    invalidation_condition    text,
    -- The queue's own status column: always "open" there. The status a published row carries is published_status.
    status                    text CHECK (status IN ('open', 'met', 'missed', 'withdrawn', 'unresolved')),
    -- Acknowledgement fields.
    acknowledged_at           date,
    ack_source_url            text,
    ack_accession             text,
    ack_filing_type           text,
    ack_filed_at              date,
    ack_fetched_at            timestamptz,
    ack_content_sha256        text,
    ack_excerpt               text,
    days_to_falsifiable       integer,
    days_to_acknowledged      integer,
    last_reviewed_at          date,               -- the published row's last_reviewed_at
    reviewer                  text,

    -- Review. approved, hand_verified and reviewer_note are the CSV's own columns. review_decision and review_tier
    -- are worked out from them when a row is written (research_record.store.review_decision).
    approved                  boolean,
    hand_verified             boolean,
    reviewer_note             text,
    review_decision           text CHECK (review_decision IN ('y', 'n', 's', 'v')),
    review_tier               text CHECK (review_tier IN ('hand', 'machine')),
    change_direction          text CHECK (change_direction IN ('initiated', 'raised', 'lowered', 'maintained', 'withdrawn')),
    -- What rubric.resolve_record gives for an approved row: the status the release carries. NULL on any other row.
    published_status          text CHECK (published_status IN ('met', 'missed', 'withdrawn', 'unresolved', 'open')),

    -- Queue flags.
    conflict                  boolean,
    empty_block               boolean,
    ack_pending_review        boolean,
    change_pending_review     boolean,

    -- Reviewer aids (the CSV's aid_* columns), as written.
    aid_proposed_status       text,
    aid_capture_method        text,
    aid_heading               text,
    aid_lead_in               text,
    aid_table_header          text,
    aid_value_column          text,
    aid_outcome_note          text,
    aid_flag_note             text,
    aid_withdrawal_note       text,
    aid_verify                text,
    aid_verify_reason         text,
    aid_verify_class          text,
    aid_suggested_note        text,
    aid_ack_proposed_at       text,
    aid_ack_proposed_excerpt  text,
    aid_ack_proposed_url      text,
    aid_ack_proposed_evidence text,
    aid_proposed_change       text,
    aid_derived_snapshot      text,
    aid_context               text,

    -- Position in the company's review queue. NULL: the row is not in the queue (a draft 05 has not queued yet).
    queue_seq                 integer,
    -- Stage 03's draft for this statement, the whole row as 03 wrote it, and its position in the drafts list.
    draft                     json,
    draft_seq                 integer,
    -- What stage 04 found for it (outcome, outcome_reason, acknowledgement, withdrawal), and its position.
    outcome_found             json,
    outcome_seq               integer,

    created_at                timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS statements_cik_idx ON statements (cik);
CREATE INDEX IF NOT EXISTS statements_cik_period_idx ON statements (cik, target_period);
CREATE INDEX IF NOT EXISTS statements_assumption_accession_idx ON statements (assumption_accession);
CREATE INDEX IF NOT EXISTS statements_outcome_accession_idx ON statements (outcome_accession);

CREATE TABLE IF NOT EXISTS jobs (
    id           bigserial PRIMARY KEY,
    cik          text NOT NULL REFERENCES companies (cik),
    kind         text NOT NULL CHECK (kind IN ('build', 'refresh')),
    -- waiting: a batch is still processing; running the job again resumes it.
    status       text NOT NULL DEFAULT 'running'
                 CHECK (status IN ('running', 'waiting', 'succeeded', 'failed', 'parked')),
    stage        text,                             -- the stage it reached
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    usd_spent    numeric(14, 8) NOT NULL DEFAULT 0,
    error        text,
    log_path     text
);
CREATE INDEX IF NOT EXISTS jobs_cik_idx ON jobs (cik, started_at);
CREATE INDEX IF NOT EXISTS jobs_started_idx ON jobs (started_at);

CREATE TABLE IF NOT EXISTS llm_calls (
    id               bigserial PRIMARY KEY,        -- also the archive's order
    step             text,                         -- 03_structure, 03b_verify, 04_outcomes, ...; NULL on a token count
    custom_id        text,
    cik              text,                         -- where the custom_id or the job says whose call it was
    job_id           bigint REFERENCES jobs (id),
    -- sha256 of the request as pipeline.llm builds it (LlmRequest.fingerprint). NULL on answers archived before
    -- fingerprints existed.
    fingerprint      text CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    model            text,
    kind             text NOT NULL CHECK (kind IN ('canary', 'sync', 'batch', 'count')),
    -- succeeded | errored | canceled | expired | truncated | invalid, as the archive has it;
    -- pending: submitted in a batch that has not ended (usd is its share of the worst case);
    -- counted: a count_tokens result, kept so a request is priced without asking again.
    status           text NOT NULL,
    batch_id         text,                         -- the batch id, or "sync" / "canary"
    request_sha256   text,                         -- the request hash: the fingerprint, where there is one
    response_sha256  text,                         -- sha256 of response_text
    response_text    text,                         -- the model's answer, verbatim
    error            text,
    input_tokens     integer NOT NULL DEFAULT 0,
    output_tokens    integer NOT NULL DEFAULT 0,
    usd              numeric(14, 8) NOT NULL DEFAULT 0,
    created_at       timestamptz DEFAULT now(),
    archive_seq      integer                       -- line number in data/batches/{step}.raw.jsonl, for a loaded row
);
CREATE INDEX IF NOT EXISTS llm_calls_fingerprint_idx ON llm_calls (fingerprint);
CREATE INDEX IF NOT EXISTS llm_calls_step_custom_idx ON llm_calls (step, custom_id);
CREATE INDEX IF NOT EXISTS llm_calls_cik_idx ON llm_calls (cik);
CREATE INDEX IF NOT EXISTS llm_calls_created_idx ON llm_calls (created_at);
CREATE INDEX IF NOT EXISTS llm_calls_job_idx ON llm_calls (job_id);
CREATE UNIQUE INDEX IF NOT EXISTS llm_calls_archive_line_idx ON llm_calls (step, archive_seq) WHERE archive_seq IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS llm_calls_count_idx ON llm_calls (fingerprint) WHERE kind = 'count';

CREATE TABLE IF NOT EXISTS flags (
    id          bigserial PRIMARY KEY,
    record_id   text NOT NULL REFERENCES statements (record_id),
    email       text,
    note        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    resolved    boolean NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS flags_record_idx ON flags (record_id);

-- Not one of the seven objects asked for. The pipeline's by-product files have no place in them and the stages
-- read them back (05 builds its outlook-block flags from drafts.empty_blocks), so they are kept here whole.
CREATE TABLE IF NOT EXISTS stage_diagnostics (
    cik        text NOT NULL REFERENCES companies (cik),
    -- drafts.rejects | drafts.skipped | drafts.empty_blocks | outcomes.rejects
    source     text NOT NULL CHECK (source IN ('drafts.rejects', 'drafts.skipped', 'drafts.empty_blocks', 'outcomes.rejects')),
    seq        integer NOT NULL,
    custom_id  text,                               -- the candidate it is about (drafts.*)
    record_id  text,                               -- the draft (outcomes.rejects) or the block id (drafts.empty_blocks)
    reason     text,
    body       json NOT NULL,                      -- the whole row, as the stage wrote it
    PRIMARY KEY (cik, source, seq)
);
CREATE INDEX IF NOT EXISTS stage_diagnostics_record_idx ON stage_diagnostics (record_id);

-- Row level security, on every table and with no policies: a role that is not the table's owner (and has no
-- BYPASSRLS) reads and writes no rows at all. The pipeline connects as the owner, which RLS does not apply to.
ALTER TABLE companies ENABLE ROW LEVEL SECURITY;
ALTER TABLE filings ENABLE ROW LEVEL SECURITY;
ALTER TABLE candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE statements ENABLE ROW LEVEL SECURITY;
ALTER TABLE llm_calls ENABLE ROW LEVEL SECURITY;
ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE flags ENABLE ROW LEVEL SECURITY;
ALTER TABLE stage_diagnostics ENABLE ROW LEVEL SECURITY;
