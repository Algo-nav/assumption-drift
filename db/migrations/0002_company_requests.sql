-- 0002: company_requests, the on-demand build queue (pipeline/worker.py, pipeline/notify.py).
--
-- Only the statements db/schema.sql gained in this change. Every one is idempotent, so running it twice changes
-- nothing. It creates objects in the current schema: run it where the other tables are (public, unless STORE_SCHEMA
-- says otherwise).
--
-- No column is added to companies: statements.review_tier ('hand' or 'machine') already records the tier per row.

-- A visitor's request for a company that is not built yet (pipeline/worker.py builds it, pipeline/notify.py emails
-- the link). The email is unverified until sign-in exists: nothing a visitor typed (ticker, name, role) is ever put in
-- an email, and the worker never mails one address twice for one company.
CREATE TABLE IF NOT EXISTS company_requests (
    id           bigserial PRIMARY KEY,
    cik          text NOT NULL CHECK (cik ~ '^\d{10}$'),
    ticker       text,                             -- as the visitor's search gave it; the companies row is the record
    name         text,
    email        text NOT NULL,
    role         text,
    -- open: waiting for the worker. queued: held by a spending cap. building: its job is running or waiting.
    -- live: the company is built. parked: needs a person. duplicate: another request is building the same company.
    status       text NOT NULL DEFAULT 'open'
                 CHECK (status IN ('open', 'queued', 'building', 'live', 'parked', 'duplicate')),
    reason       text,                             -- why queued or parked, in plain words for the user
    job_id       bigint REFERENCES jobs (id),
    created_at   timestamptz NOT NULL DEFAULT now(),
    notified_at  timestamptz,                      -- when the "it is live" email went out (or was logged, in a dry run)
    source       text NOT NULL DEFAULT 'pilot',
    resend_id    text                              -- the Resend message id, when one was sent
);
CREATE INDEX IF NOT EXISTS company_requests_cik_status_idx ON company_requests (cik, status);
-- One address has at most one request in flight for a company. Case is ignored: the address is unverified.
CREATE UNIQUE INDEX IF NOT EXISTS company_requests_one_active_idx ON company_requests (cik, lower(email))
    WHERE status IN ('open', 'queued', 'building');

ALTER TABLE company_requests ENABLE ROW LEVEL SECURITY;
