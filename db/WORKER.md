# The worker

`pipeline/worker.py` builds the companies visitors asked for (`company_requests`), one at a time, and
`pipeline/notify.py` emails each of them a link when the company is live. It is a worker service: it listens on no
port and has no health endpoint.

    worker: python -m pipeline.worker        (Procfile)

## Deploy

    railway up --service worker

Before the first deploy, apply `db/migrations/0002_company_requests.sql` to the database (see below). The worker
refuses to start without the table and says so.

## Environment

Every variable the worker reads. No values here; set them on the Railway service.

| Variable | Read by | Meaning | Default |
|---|---|---|---|
| `DATABASE_URL` | `research_record/store.py` | The Postgres database. Use a direct or session-mode connection: the lock below is a session lock, and a transaction-mode pooler does not keep one. | required |
| `STORE_SCHEMA` | `research_record/store.py` | The schema the tables are in. | `public` |
| `ANTHROPIC_API_KEY` | `pipeline/llm.py` | The model key the build stages use. | required for a build |
| `JOB_CAP_USD` | `pipeline/job.py`, `pipeline/worker.py` | The most one build may spend. Also what must fit under the two caps below before a build starts. | `1.50` |
| `DAILY_CAP_USD` | `pipeline/job.py`, `pipeline/worker.py` | The most that may be spent in a UTC day. | `8.00` |
| `WORKSPACE_CAP_USD` | `pipeline/job.py`, `pipeline/worker.py` | The most that may be spent in a calendar month (UTC). | `70.00` |
| `POLL_SECONDS` | `pipeline/worker.py` | Seconds between passes over the queue. | `30` |
| `WORKER_WAIT_MINUTES` | `pipeline/worker.py` | How long a stage waits for its batch before the job is left waiting and picked up on a later pass. Also the longest a shutdown can take to reach a stage boundary. | `10` |
| `RESEND_API_KEY` | `pipeline/notify.py` | The Resend key. Unset: no email is sent, each one is written to the log. | unset |
| `NOTIFY_FROM` | `pipeline/notify.py` | The sender. | `SCND Order <pilot@scndorder.com>` |
| `PILOT_BASE_URL` | `pipeline/notify.py` | The site the link in the email points at. | `https://pilot.scndorder.com` |
| `NOTIFY_DRY_RUN` | `pipeline/notify.py` | `1`: no email is sent, each one is written to the log, and `notified_at` is still set. | unset |
| `NOTIFY_MAX_PER_ADDRESS_PER_DAY` | `pipeline/notify.py` | How many companies one address may be emailed about in a UTC day. The rest are held and go out on a later day. | `5` |

Not read by the worker: `STORE` (a job runs against Postgres whatever it says). The EDGAR user agent and request rate,
the model and its prices are `pipeline/config.yaml`, as for every other run.

`RESEND_API_KEY` is read from the environment only, never from `.env`.

## The lock, in the logs

One worker runs at a time. At startup it takes a Postgres advisory lock without waiting.

    INFO worker lock taken (schema public): this is the only worker; polling every 30s

means this instance has the lock and is working. A second instance logs

    INFO worker lock is held by another worker: exiting, nothing to do

and exits 0. On shutdown the first one logs `worker lock released; worker stopped`.

## What the other log lines say

Every change to a request is one line:

    INFO request=12 cik=0000000123 status=building usd=0.0000 job=31
    INFO request=12 cik=0000000123 status=live usd=0.4312 job=31 machine_tier_rows=57
    INFO request=12 cik=0000000123 status=live usd=0.4312 emailed r***@example.com resend_id=...

`usd` is what the request's job has spent so far. Addresses are masked in the log. A parked request carries a
`detail:` saying what a person needs to look at; the `reason` is the plain sentence the site can show.

| Status | Reason | What happened |
|---|---|---|
| `queued` | `daily spending limit reached, resumes tomorrow` | A build would pass `DAILY_CAP_USD`. No job was created. It reopens by itself. |
| `queued` | `monthly spending limit reached` | The same for `WORKSPACE_CAP_USD`. |
| `duplicate` | `already requested` | Another request is building or queued for the same company. It is emailed when that one is live. |
| `parked` | `needs manual setup` | The company has no `companies` row and the SEC submissions API does not state everything a build needs (see below). |
| `parked` | `build failed, we are looking at it` | The company's last two jobs failed, or a job passed `JOB_CAP_USD`. Never retried from here. |
| `parked` | `built, but no forward-looking statements were found` | The build succeeded and there is nothing to show. No email is sent. |

## Setting a company up by hand

A build needs a `companies` row. For a CIK that has none the worker reads
`data.sec.gov/submissions/CIK##########.json` and takes the name, the ticker and the fiscal year end month. It does not
guess the rest: a company whose fiscal year does not end in December is parked, because whether its "FY2026" names the
year it ends in or the year it starts in is not in the SEC's answer.

To see what the worker would do for a CIK, writing nothing:

    python -m pipeline.worker --derive 0000000123

To set a parked company up and release its requests:

    python -m pipeline.job add --cik CIK --ticker T --name NAME --fiscal-year-end-month N --fiscal-year-named-for end|start
    UPDATE company_requests SET status = 'open', reason = NULL WHERE cik = 'CIK' AND status = 'parked';

A company parked after two failed builds stays parked until a person runs
`python -m pipeline.job run --cik CIK --kind build --retry-parked`. If that build succeeds, the worker emails the
company's live and duplicate requests on its next pass; a parked request is released with the `UPDATE` above.

## Shutdown

Railway sends SIGTERM on redeploy. The worker finishes the stage in hand, leaves the job `waiting`, releases the lock
and exits 0; the next instance resumes the job. If the process is killed first, the next instance finds the request
still `building` and runs its job again: answers already paid for are reused, and a submitted batch is not resubmitted.
