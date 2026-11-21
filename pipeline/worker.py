"""The on-demand worker: builds the companies visitors asked for, one at a time, and emails them when one is live.

    python -m pipeline.worker                 run until told to stop (SIGTERM or Ctrl-C)
    python -m pipeline.worker --once          one pass over the queue, then exit
    python -m pipeline.worker --derive CIK    print what would be set up for a CIK from the SEC's two APIs, and the 10-K facts
                                              the fiscal year fields came from; writes nothing

It reads `company_requests` (db/schema.sql) and always runs against Postgres (DATABASE_URL, STORE_SCHEMA), as a job does.
db/WORKER.md lists every environment variable it reads.

One worker, one build. At startup it takes a Postgres advisory lock without waiting; if another worker holds it, this one
logs that and exits 0. Builds are never interleaved: a pending batch is kept per stage, not per company, so a second
build is not started while one is waiting for its batch.

Each pass, in this order:

  1. Queued requests go back to open when the spending caps have room again (or their company got built meanwhile).
  2. A request that is building is carried on: its job is run again, which resumes it where it stopped.
  3. Open requests, oldest first:
       the company is already built          -> live, and the email goes out
       another request for it is building    -> duplicate ("already requested"); emailed when that build is live
         or queued
       the company is parked                 -> parked; a parked company is never retried from here
       a build is still waiting on a batch   -> left open for a later pass
       a cap has no room for a build         -> queued, no job is created
       the company has no `companies` row    -> set up from the SEC's submissions and company facts APIs, or parked
                                                ("needs manual setup")
       otherwise                             -> a jobs row, the request is building, and pipeline.job.run_job runs in
                                                this process
  4. Every built company that still owes someone an email is emailed (pipeline/notify.py). A company that built with
     nothing to show owes one too: its requests are parked, and each is told so once.

Caps. JOB_CAP_USD, DAILY_CAP_USD and WORKSPACE_CAP_USD are the ones pipeline.job reads. Before a build starts, the worker
adds JOB_CAP_USD (the most one build may spend) to what llm_calls.usd says was spent today and this month (UTC); if either
sum is over its cap the request is queued. Inside the build, pipeline.job's own guard still checks every send.

Setting a company up. pipeline.job builds from a `companies` row, not from a CIK. For a CIK with no row the worker reads
data.sec.gov/submissions/CIK##########.json for the name, the ticker and the fiscal year end, and
data.sec.gov/api/xbrl/companyfacts/CIK##########.json for how the company's 10-Ks name and end their fiscal years
(`derive_fiscal_year`). Where the two disagree, or the facts do not settle it, it does not invent: see `derive_company`.

Failure. pipeline.job's rule: a company whose last two jobs failed is parked. After a first failure the request goes back
to open and is tried once more on a later pass; after the second the request is parked.

Shutdown. SIGTERM sets a flag. A build in progress stops at its next stage boundary (the job is left `waiting` and is
resumed by the next worker), the loop ends, the lock is released, and the process exits 0. If the process is killed
before it gets there, the next worker finds the request still building and runs its job again.

Every state change is one log line: request id, cik, status, and what the request's job has spent so far.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import re
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import requests

from pipeline import common, job, notify
from research_record import store

log = logging.getLogger("pipeline.worker")

DAILY_REASON = "daily spending limit reached, resumes tomorrow"
MONTHLY_REASON = "monthly spending limit reached"
FAILED_REASON = "build failed, we are looking at it"
SETUP_REASON = "needs manual setup"
DUPLICATE_REASON = "already requested"
#: A company can build cleanly and have nothing to show. Its requests are parked with this reason and still emailed
#: (notify.build_empty_email): the person is told there is no page, not left waiting.
EMPTY_REASON = "built, but no forward-looking statements were found"

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SUBMISSIONS_HOST = "data.sec.gov"
#: The two things on data.sec.gov the worker may ask for.
SEC_PATHS = ("/submissions/", "/api/xbrl/companyfacts/")
#: us-gaap concepts whose 10-K facts date a fiscal year, in the order they are tried.
FACT_CONCEPTS = ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "Assets")
#: Fewer 10-Ks than this settle nothing.
MIN_FACTS = 2
#: Only 10-Ks whose period ends on or after this day vote on fiscal_year_named_for. 10-Ks before it tag `fy` as the
#: end year minus one for January filers whatever the company calls the year (Walmart, NVIDIA, Salesforce and Target
#: alike, through the years ended January 2014), so they are not evidence of how the company names its years.
#: The concept is chosen by the same 10-Ks: the first of FACT_CONCEPTS that MIN_FACTS or more of them report, so a
#: concept a company stopped using before the cutoff does not stand in the way of one it uses now.
NAMING_CUTOFF = date(2015, 1, 1)
DEFAULT_POLL_SECONDS = 30.0
DEFAULT_WAIT_MINUTES = 10.0
#: How many times one process tries to email one request before leaving it for a person (or the next deploy).
NOTIFY_ATTEMPTS = 3
#: After the SEC answers 403 or 429, how long the worker asks it nothing.
EDGAR_BACKOFF_SECONDS = 600.0

_TICKER = re.compile(r"^[A-Z0-9.\-]{1,10}$")  # the companies.ticker check
_CIK = re.compile(r"^\d{10}$")


# --- setting a company up from the SEC ----------------------------------------------------------------------------------


class NeedsSetup(Exception):
    """A person has to set this company up: something the build needs is not in the SEC's answer."""


@dataclass(frozen=True)
class Derived:
    name: str
    ticker: str
    fiscal_year_end_month: int
    fiscal_year_named_for: str


@dataclass(frozen=True)
class FiscalFact:
    """What one 10-K says about its own fiscal year: the SEC's `fy` for the filing, and the day that year ended."""
    concept: str
    accn: str
    filed: str
    fy: int
    end: date

    @property
    def anchor(self) -> date:
        """The end date a week earlier, the rule pipeline.common already uses (fiscal_label_for_end): a 52/53 week year
        ends a few days either side of a month end, so Feb. 1 is placed in January and Jan. 2 in December of the year before."""
        return self.end - timedelta(days=7)

    @property
    def named_for(self) -> str:
        return "end" if self.fy == self.anchor.year else "start"

    @property
    def end_month(self) -> int:
        return self.anchor.month


@dataclass(frozen=True)
class FiscalYear:
    named_for: str
    end_month: int
    concept: str
    facts: tuple[FiscalFact, ...]


def annual_facts(facts: dict[str, Any]) -> tuple[str | None, list[FiscalFact]]:
    """(concept, one FiscalFact per 10-K) from a company facts answer, oldest filing first.

    The concepts are tried in FACT_CONCEPTS order and the first one that MIN_FACTS or more 10-Ks ending on or after
    NAMING_CUTOFF report is used. Failing that, the first that MIN_FACTS or more 10-Ks of any date report (enough for
    the month, and for a December year, which needs no vote); failing that, the first with any (the caller then
    refuses). Only entries with form 10-K and fp FY are read.

    Every fact in a 10-K carries that filing's `fy`, including the prior years it repeats for comparison. Comparing each
    of those with its own end date would count two "wrong" years for every right one, so a 10-K gets one fact: the
    one with the latest end date, which is the year the filing reports."""
    gaap = ((facts.get("facts") or {}).get("us-gaap") or {}) if isinstance(facts, dict) else {}
    fallback: tuple[str | None, list[FiscalFact]] = (None, [])
    older: tuple[str | None, list[FiscalFact]] = (None, [])
    for concept in FACT_CONCEPTS:
        latest: dict[str, FiscalFact] = {}
        units = (gaap.get(concept) or {}).get("units") or {}
        for entries in units.values():
            for entry in entries or []:
                if not isinstance(entry, dict) or entry.get("form") != "10-K" or entry.get("fp") != "FY":
                    continue
                fy, accn = entry.get("fy"), entry.get("accn")
                try:
                    ended = date.fromisoformat(entry.get("end") or "")
                except (TypeError, ValueError):
                    continue
                if not isinstance(fy, int) or isinstance(fy, bool) or not isinstance(accn, str):
                    continue
                if accn not in latest or ended > latest[accn].end:
                    latest[accn] = FiscalFact(concept, accn, str(entry.get("filed") or ""), fy, ended)
        found = sorted(latest.values(), key=lambda f: (f.end, f.filed, f.accn))
        if sum(f.end >= NAMING_CUTOFF for f in found) >= MIN_FACTS:
            return concept, found
        if len(found) >= MIN_FACTS and not older[1]:
            older = (concept, found)
        if found and not fallback[1]:
            fallback = (concept, found)
    return older if older[1] else fallback


def _majority(values: list[Any]) -> Any | None:
    """The value more than half of `values` share, or None."""
    for value in set(values):
        if values.count(value) * 2 > len(values):
            return value
    return None


def derive_fiscal_year(facts: dict[str, Any]) -> FiscalYear:
    """fiscal_year_end_month and fiscal_year_named_for from a company facts answer. Raises NeedsSetup rather than guess.

    Each 10-K (see `annual_facts`) says two things: the month its year ended in, and whether the SEC's `fy` for it is
    the calendar year it ended in ("end") or not ("start": the year it started in). The month is taken by majority over
    the 10-Ks; the name by majority over the 10-Ks whose period ends on or after NAMING_CUTOFF (see there for why the
    older ones do not count). Refused: fewer than MIN_FACTS 10-Ks, fewer than MIN_FACTS of them from the cutoff on, or
    no majority (an even split).

    When the year ends in December the two names mean the same thing (pipeline.common.period_end), so it is "end"
    whatever `fy` says, and no vote is held."""
    concept, found = annual_facts(facts)
    if len(found) < MIN_FACTS:
        tried = ", ".join(FACT_CONCEPTS)
        raise NeedsSetup(f"the SEC company facts API has {len(found)} 10-K fiscal year fact(s) for this CIK (tried {tried}); "
                         f"{MIN_FACTS} are needed to say how its fiscal years are named")
    month = _majority([f.end_month for f in found])
    if month is None:
        raise NeedsSetup(f"the {len(found)} 10-K facts ({concept}) do not agree on the month the fiscal year ends in: "
                         + ", ".join(f"{f.end.isoformat()}" for f in found))
    if month == 12:
        named_for = "end"
    else:
        voting = [f for f in found if f.end >= NAMING_CUTOFF]
        if len(voting) < MIN_FACTS:
            raise NeedsSetup(f"{len(voting)} of the {len(found)} 10-K facts ({concept}) end on or after {NAMING_CUTOFF.isoformat()}; "
                             f"{MIN_FACTS} are needed to say whether FY labels name the year the fiscal year ends in or the year it starts in")
        named_for = _majority([f.named_for for f in voting])
        if named_for is None:
            raise NeedsSetup(f"the {len(voting)} 10-K facts ({concept}) from {NAMING_CUTOFF.isoformat()} on split evenly on whether FY labels "
                             "name the year the fiscal year ends in or the year it starts in: "
                             + ", ".join(f"fy {f.fy} ended {f.end.isoformat()}" for f in voting))
    return FiscalYear(named_for=named_for, end_month=month, concept=concept or "", facts=tuple(found))


def derive_company(payload: dict[str, Any], facts: dict[str, Any], wanted_ticker: str | None = None) -> Derived:
    """The `companies` fields a build needs, from a submissions API answer and a company facts API answer. Raises
    NeedsSetup rather than guess.

    name                      `name`, as the SEC has it.
    ticker                    from `tickers`. The one the request named if the SEC lists it, else the only one listed.
                              None listed, or several and the request named none of them: not chosen here.
    fiscal_year_end_month     from the 10-K facts' end dates (`derive_fiscal_year`), checked against the submissions
                              answer's `fiscalYearEnd` (MMDD). Both dates are placed by the month a week earlier, the
                              rule pipeline.common already uses (fiscal_label_for_end): 0131 and 0203 are both January.
                              The two sources disagreeing is not settled here.
    fiscal_year_named_for     from the 10-K facts (`derive_fiscal_year`): whether the SEC's `fy` for a 10-K is the year
                              its fiscal year ended in.

    tax_rate_parens_negative is in neither answer and is not set here: the new row takes the column's
    default (false), which is the pipeline's general rule for a parenthesised rate, not a finding about this filer.
    """
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise NeedsSetup("the SEC submissions API gives no name for this CIK")
    listed = [t.strip().upper() for t in payload.get("tickers") or [] if isinstance(t, str) and t.strip()]
    wanted = (wanted_ticker or "").strip().upper()
    if not listed:
        raise NeedsSetup("the SEC submissions API lists no ticker for this CIK")
    if wanted in listed:
        ticker = wanted
    elif len(listed) == 1:
        ticker = listed[0]
    else:
        raise NeedsSetup(f"the SEC lists {len(listed)} tickers ({', '.join(listed)}) and the request names none of them")
    if not _TICKER.match(ticker):
        raise NeedsSetup(f"ticker {ticker!r} does not fit companies.ticker")
    month = submissions_month(payload)
    fiscal = derive_fiscal_year(facts)
    if fiscal.end_month != month:
        raise NeedsSetup(f"the 10-K facts ({fiscal.concept}) say the fiscal year ends in month {fiscal.end_month}, the SEC submissions "
                         f"API says month {month} (fiscalYearEnd {payload.get('fiscalYearEnd')})")
    return Derived(name=name.strip(), ticker=ticker, fiscal_year_end_month=month, fiscal_year_named_for=fiscal.named_for)


def submissions_month(payload: dict[str, Any]) -> int:
    """The month the fiscal year ends in, from a submissions answer's `fiscalYearEnd` (MMDD), placed a week earlier."""
    fye = payload.get("fiscalYearEnd")
    m = re.fullmatch(r"(\d{2})(\d{2})", fye) if isinstance(fye, str) else None
    try:
        end = date(2000, int(m.group(1)), int(m.group(2))) if m else None  # 2000: a leap year, so 0229 is a date
    except ValueError:
        end = None
    if end is None:
        raise NeedsSetup(f"the SEC submissions API gives no usable fiscalYearEnd for this CIK ({fye!r})")
    return (end - timedelta(days=7)).month


def submissions_client(config: dict[str, Any] | None = None) -> Any:
    """pipeline.01_fetch's EDGAR client (the declared User-Agent, the rate limit, retries), allowed to ask
    data.sec.gov for submissions and company facts and nothing else. The stages' own client stays limited to the
    hosts evidence may come from."""
    fetch = importlib.import_module("pipeline.01_fetch")

    class SubmissionsClient(fetch.EdgarClient):
        @staticmethod
        def _check(url: str) -> str:
            if not url.startswith(tuple(f"https://{SUBMISSIONS_HOST}{path}" for path in SEC_PATHS)):
                raise fetch.EdgarError(f"refusing {url}: only the submissions and company facts APIs on {SUBMISSIONS_HOST} are allowed here")
            return SUBMISSIONS_HOST

    edgar = (config or common.load_config())["edgar"]
    return SubmissionsClient(edgar["user_agent"], edgar["max_rps"])


def _fetch_json(url: str, client: Any, api: str, missing: str, cik: str) -> dict[str, Any]:
    fetched = client.get(url)
    if fetched.status == 404:
        raise NeedsSetup(missing)
    if fetched.status != 200:
        raise importlib.import_module("pipeline.01_fetch").EdgarError(f"HTTP {fetched.status} from the {api} API for {cik}")
    try:
        payload = json.loads(fetched.content)
    except ValueError as exc:
        raise NeedsSetup(f"the SEC {api} API did not answer with JSON") from exc
    if not isinstance(payload, dict):
        raise NeedsSetup(f"the SEC {api} API did not answer with an object")
    return payload


def fetch_submissions(cik: str, client: Any) -> dict[str, Any]:
    return _fetch_json(SUBMISSIONS_URL.format(cik=cik), client, "submissions", "the SEC has no company with this CIK", cik)


def fetch_facts(cik: str, client: Any) -> dict[str, Any]:
    return _fetch_json(FACTS_URL.format(cik=cik), client, "company facts", "the SEC has no XBRL company facts for this CIK", cik)


# --- the worker ---------------------------------------------------------------------------------------------------------


class Worker:
    """`build`, `derive` and `post` are the three things that reach outside the database; tests replace them.

    build(cik, job_id, should_stop)   run the build as jobs row `job_id`; the default is pipeline.job.run_job
    derive(cik, wanted_ticker)        -> Derived, or raises NeedsSetup; the default asks the SEC's two APIs
    post(url, ...)                    requests.post, for pipeline.notify
    """

    def __init__(self, db: Any, *, caps: dict[str, float] | None = None, build: Callable[..., Any] | None = None,
                 derive: Callable[[str, str | None], Derived] | None = None, settings: notify.Settings | None = None,
                 post: Callable[..., Any] = requests.post, poll_seconds: float = DEFAULT_POLL_SECONDS,
                 wait_minutes: float = DEFAULT_WAIT_MINUTES, echo: bool = True, jobs_dir: Path | None = None,
                 today: Callable[[], date] = lambda: datetime.now(timezone.utc).date()) -> None:
        self.db = db
        self.caps = caps or job.caps_from_env()
        self.build = build or self._run_job
        self.derive = derive or self._derive_from_sec
        self.settings = settings or notify.Settings.from_env()
        self.post, self.poll_seconds, self.wait_minutes, self.echo, self.jobs_dir, self.today = post, poll_seconds, wait_minutes, echo, jobs_dir, today
        self.stopping = False
        self._wake = threading.Event()
        self._lock: Any = None
        self._edgar: Any = None
        self._edgar_quiet_until = 0.0
        self._notify_failures: dict[int, int] = {}
        self._said: set[tuple[int, str]] = set()

    # -- the three ways out of the database --

    def _run_job(self, cik: str, job_id: int, should_stop: Callable[[], bool]) -> Any:
        return job.run_job(cik, "build", db=self.db, caps=self.caps, wait_minutes=self.wait_minutes, job_id=job_id,
                           should_stop=should_stop, echo=self.echo, jobs_dir=self.jobs_dir)

    def _derive_from_sec(self, cik: str, wanted_ticker: str | None) -> Derived:
        if self._edgar is None:
            self._edgar = submissions_client()
        return derive_company(fetch_submissions(cik, self._edgar), fetch_facts(cik, self._edgar), wanted_ticker)

    # -- the log --

    def _usd(self, request: dict[str, Any]) -> float:
        return float(self.db.job(request["job_id"])["usd_spent"]) if request.get("job_id") else 0.0

    def _line(self, request: dict[str, Any], status: str, note: str = "") -> None:
        log.info("request=%s cik=%s status=%s usd=%.4f%s", request["id"], request["cik"], status, self._usd(request),
                 f" {note}" if note else "")

    def _set(self, request: dict[str, Any], status: str, reason: str | None = None, note: str = "", **fields: Any) -> dict[str, Any]:
        """Change a request's status (and reason: a status that has none clears it) and say so in one line."""
        self.db.update_request(request["id"], status=status, reason=reason, **fields)
        request = {**request, "status": status, "reason": reason, **fields}
        self._line(request, status, " ".join(p for p in (f'reason="{reason}"' if reason else "", note) if p))
        return request

    def _once(self, request_id: int, what: str) -> bool:
        """True the first time this process has this to say about this request: a held email is not logged every pass."""
        if (request_id, what) in self._said:
            return False
        self._said.add((request_id, what))
        return True

    # -- caps --

    def cap_reason(self) -> str | None:
        """Why a build may not start now, or None. A build may spend up to JOB_CAP_USD, so that is what must fit."""
        day, worst = self.today(), self.caps["JOB_CAP_USD"]
        if self.db.spent_in_month(day) + worst > self.caps["WORKSPACE_CAP_USD"] + 1e-9:
            return MONTHLY_REASON
        if self.db.spent_on_day(day) + worst > self.caps["DAILY_CAP_USD"] + 1e-9:
            return DAILY_REASON
        return None

    # -- one pass --

    def tick(self) -> None:
        if self._lock is not None:
            self._lock.execute("SELECT 1")  # the lock lives on this connection: if it is gone, so is the lock, and so are we
        self._requeue()
        for request in self.db.requests(status="building"):
            if self.stopping:
                return
            self._guarded(self._carry_on, request)
        for listed in self.db.requests(status="open"):
            if self.stopping:
                return
            request = self.db.request(listed["id"])
            if request and request["status"] == "open":
                self._guarded(self._handle_open, request)
        if not self.stopping:
            for cik in self.db.ciks_owed_an_email(parked_reason=EMPTY_REASON):
                self.notify_cik(cik)

    def _guarded(self, step: Callable[[dict[str, Any]], None], request: dict[str, Any]) -> None:
        """One request must not stop the queue. A lost database connection is not caught: the process ends and the next
        one carries on from the rows. Anything else parks the request, and fails its job if it has one in flight."""
        import psycopg

        try:
            step(request)
        except (psycopg.OperationalError, psycopg.InterfaceError):
            raise
        except Exception:  # noqa: BLE001 - logged whole; the request is parked for a person to look at
            log.exception("request=%s cik=%s failed in the worker itself", request["id"], request["cik"])
            current = self.db.request(request["id"]) or request
            if current.get("job_id") and self.db.job(current["job_id"])["status"] in ("running", "waiting"):
                self.db.update_job(current["job_id"], status="failed", error="the worker failed outside the stages; see its log",
                                   finished_at=datetime.now(timezone.utc))
            self._set(current, "parked", FAILED_REASON)

    def _requeue(self) -> None:
        for request in self.db.requests(status="queued"):
            company = self.db.company(request["cik"])
            reason = None if company and company["status"] == "built" else self.cap_reason()
            if reason is None:
                self._set(request, "open", note="the caps have room again")
            elif reason != request["reason"]:
                self._set(request, "queued", reason)

    def _handle_open(self, request: dict[str, Any]) -> None:
        cik = request["cik"]
        if not _CIK.match(cik):
            self._set(request, "parked", SETUP_REASON, note="detail: the cik is not ten digits")
            return
        company = self.db.company(cik)
        if company and company["status"] == "built":
            self._set(request, "live")
            self.notify_cik(cik)
            return
        others = [r for r in self.db.requests(cik=cik) if r["id"] != request["id"] and r["status"] in ("building", "queued")]
        if others:
            self._set(request, "duplicate", DUPLICATE_REASON, note=f"of request {others[0]['id']}")
            return
        if company and company["status"] == "parked":
            self._set(request, "parked", FAILED_REASON, note="the company is parked; it is not retried from here")
            return
        if self.db.requests(status="building"):
            return  # one build at a time; this one keeps its place in the queue
        reason = self.cap_reason()
        if reason:
            self._set(request, "queued", reason)
            return
        if company is None and not self._set_up(request):
            return
        job_id = self.db.create_job(cik, "build")
        request = self._set(request, "building", job_id=job_id, note=f"job={job_id}")
        self._carry_on(request)

    def _set_up(self, request: dict[str, Any]) -> bool:
        """Put the company in `companies` from the SEC's answer. False: the request was parked, or left open to ask again."""
        fetch = importlib.import_module("pipeline.01_fetch")
        cik = request["cik"]
        if time.monotonic() < self._edgar_quiet_until:
            return False
        try:
            derived = self.derive(cik, request["ticker"])
            holder = self.db.ticker_holder(derived.ticker)
            if holder is not None:
                raise NeedsSetup(f"ticker {derived.ticker} already belongs to {holder} in companies")
        except NeedsSetup as exc:
            self._park(request, SETUP_REASON, f"detail: {exc}")
            return False
        except fetch.Blocked as exc:
            self._edgar_quiet_until = time.monotonic() + EDGAR_BACKOFF_SECONDS
            log.warning("request=%s cik=%s the SEC refused the request (%s); asking it nothing for %.0f minutes, the request stays open",
                        request["id"], cik, exc, EDGAR_BACKOFF_SECONDS / 60)
            return False
        except fetch.EdgarError as exc:
            log.warning("request=%s cik=%s the SEC could not be read (%s); the request stays open", request["id"], cik, exc)
            return False
        self.db.upsert_company(cik, ticker=derived.ticker, name=derived.name, status="requested",
                               fiscal_year_end_month=derived.fiscal_year_end_month, fiscal_year_named_for=derived.fiscal_year_named_for)
        log.info("request=%s cik=%s set up from the SEC: %s (%s), fiscal year ends in month %s and is named for its %s; "
                 "tax_rate_parens_negative left at its default (false)", request["id"], cik, derived.name, derived.ticker,
                 derived.fiscal_year_end_month, derived.fiscal_year_named_for)
        return True

    def _carry_on(self, request: dict[str, Any]) -> None:
        """Run the request's job (a new one, one waiting on a batch, or one a killed worker left behind) and act on how it ended."""
        try:
            self.build(request["cik"], request["job_id"], lambda: self.stopping)
        except job.JobError as exc:  # refused before any stage ran; the job row the worker made is closed, not left running
            if self.db.job(request["job_id"])["status"] in ("running", "waiting"):
                self.db.update_job(request["job_id"], status="failed", error=str(exc)[:500], finished_at=datetime.now(timezone.utc))
        self._settle(request)

    def _settle(self, request: dict[str, Any]) -> None:
        cik = request["cik"]
        row, company = self.db.job(request["job_id"]), self.db.company(cik)
        status, error = row["status"], row["error"] or ""
        built = bool(company) and company["status"] == "built"
        if status == "succeeded" or (status == "failed" and built):
            tiered = self.db.mark_machine_tier(cik) if status == "succeeded" else 0
            self._set(request, "live", job_id=request["job_id"], note=f"job={request['job_id']} machine_tier_rows={tiered}")
            self.notify_cik(cik)
        elif status in ("running", "waiting"):
            self._line(request, "building", f"job={request['job_id']} is waiting ({error or 'not finished'}); it is resumed on a later pass")
        elif status == "parked" and error.startswith("DAILY_CAP_USD"):
            self._set(request, "queued", DAILY_REASON, note=f"job={request['job_id']} stopped by the cap")
        elif status == "parked" and error.startswith("WORKSPACE_CAP_USD"):
            self._set(request, "queued", MONTHLY_REASON, note=f"job={request['job_id']} stopped by the cap")
        elif status == "failed" and not (company and company["status"] == "parked"):
            self._set(request, "open", note=f"job={request['job_id']} failed; one more try on a later pass")
        else:  # the second failure in a row, or a job stopped by JOB_CAP_USD or config.yaml's budget: a person's call
            self._park(request, FAILED_REASON, f"job={request['job_id']} {status}")

    def _park(self, request: dict[str, Any], reason: str, note: str) -> None:
        """Park a request, and with it the duplicates that were waiting on it: they share its fate, and its reason."""
        self._set(request, "parked", reason, note=note)
        for other in self.db.requests(cik=request["cik"], status="duplicate", unnotified=True):
            self._set(other, "parked", reason, note=f"with request {request['id']}")

    # -- the email --

    def notify_cik(self, cik: str) -> None:
        """Email every request for this built company that has not been emailed, whatever its status. A company that
        built with nothing to show: its requests are parked (EMPTY_REASON) and emailed that there is no page."""
        pending = self.db.requests(cik=cik, unnotified=True)
        if not pending:
            return
        company, counts = self.db.company(cik), self.db.email_counts(cik)
        if not counts["n_statements"]:
            pending = [request if request["status"] == "parked" else self._set(request, "parked", EMPTY_REASON, note="nothing to show")
                       for request in pending]
        for request in pending:
            self._notify_one(request, company, counts)

    def _notify_one(self, request: dict[str, Any], company: dict[str, Any], counts: dict[str, Any]) -> None:
        rid, email = request["id"], (request["email"] or "").strip()
        to = notify.masked(email)
        if self._notify_failures.get(rid, 0) >= NOTIFY_ATTEMPTS:
            return
        if not notify.valid_address(email):
            self._notify_failures[rid] = NOTIFY_ATTEMPTS
            self._line(request, request["status"], "not emailed: the address on the request is not one plain email address")
            return
        now = datetime.now(timezone.utc)
        if self.db.emailed_before(request["cik"], email, rid):
            self.db.update_request(rid, notified_at=now)
            self._line(request, request["status"], f"not emailed again: {to} was already emailed about this company; notified_at set")
            return
        if self.db.emails_sent_on(email, self.today()) >= self.settings.max_per_address_per_day:
            if self._once(rid, "held"):
                self._line(request, request["status"], f"email held: {to} has had {self.settings.max_per_address_per_day} today "
                                                       "(NOTIFY_MAX_PER_ADDRESS_PER_DAY); it goes out on a later day")
            return
        if counts["n_statements"]:
            subject, body = notify.build_email(company, counts, request, self.settings.base_url)
        else:
            subject, body = notify.build_empty_email(company, request)
        if self.settings.dry_run:
            self.db.update_request(rid, notified_at=now)
            self._line(request, request["status"], f"email NOT sent ({self.settings.why_dry}): written here instead, notified_at set\n"
                                                   f"To: {to}\nSubject: {subject}\n\n{body}")
            return
        try:
            resend_id = notify.send(email, subject, body, settings=self.settings, idempotency_key=f"company-request-{rid}", post=self.post)
        except notify.NotifyError as exc:
            self._notify_failures[rid] = NOTIFY_ATTEMPTS if exc.permanent else self._notify_failures.get(rid, 0) + 1
            left = NOTIFY_ATTEMPTS - self._notify_failures[rid]
            self._line(request, request["status"], f"email to {to} not sent: {exc}; " + (f"{left} more tries" if left else "not tried again by this process"))
            return
        self.db.update_request(rid, notified_at=now, resend_id=resend_id)
        self._line(request, request["status"], f"emailed {to} resend_id={resend_id}")

    # -- running --

    def stop(self, signum: int | None = None, frame: Any = None) -> None:
        if not self.stopping:
            log.info("asked to stop%s: finishing at the next stage boundary", f" (signal {signum})" if signum else "")
        self.stopping = True
        self._wake.set()

    def run(self, *, once: bool = False) -> int:
        self._lock = self.db.worker_lock()
        if self._lock is None:
            log.info("worker lock is held by another worker: exiting, nothing to do")
            return 0
        log.info("worker lock taken (schema %s): this is the only worker; polling every %.0fs", self.db.schema, self.poll_seconds)
        previous: dict[int, Any] = {}
        if threading.current_thread() is threading.main_thread():
            previous = {s: signal.signal(s, self.stop) for s in (signal.SIGTERM, signal.SIGINT)}
        try:
            while not self.stopping:
                self.tick()
                if once:
                    break
                self._wake.wait(self.poll_seconds)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
            self._lock.close()
            self._lock = None
            log.info("worker lock released; worker stopped")
        return 0


# --- command line -------------------------------------------------------------------------------------------------------


def _number(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError as exc:
        raise job.JobError(f"{name} must be a number, got {raw!r}") from exc
    if value < 0:
        raise job.JobError(f"{name} must not be negative, got {raw!r}")
    return value


def show_derivation(ciks: list[str]) -> int:
    """Print what the worker would set up for each CIK, the 10-K facts the two fiscal year fields came from, and how
    that compares with config.yaml where the company is there."""
    fetch = importlib.import_module("pipeline.01_fetch")
    config = common.load_config()
    by_hand = {c.cik: c for c in common.companies(config)}
    client = submissions_client(config)
    for raw in ciks:
        cik = raw.zfill(10)
        hand = by_hand.get(cik)
        try:
            payload = fetch_submissions(cik, client)
            print(f"{cik}: SEC says name={payload.get('name')!r} tickers={payload.get('tickers')!r} fiscalYearEnd={payload.get('fiscalYearEnd')!r}")
            facts = fetch_facts(cik, client)
            concept, found = annual_facts(facts)
            print(f"  10-K facts (us-gaap {concept}, form 10-K, fp FY, one per 10-K: the latest end date in it): {len(found)}")
            for f in found:
                counted = "" if f.end >= NAMING_CUTOFF else f" (before {NAMING_CUTOFF.isoformat()}: not counted for the name)"
                print(f"    fy={f.fy} end={f.end.isoformat()} filed={f.filed} accn={f.accn} -> month {f.end_month}, named for {f.named_for}{counted}")
            try:
                fiscal = derive_fiscal_year(facts)
                votes = ", ".join(f"{sum(f.named_for == v for f in found if f.end >= NAMING_CUTOFF)} {v}" for v in ("end", "start"))
                print(f"  derived: fiscal_year_end_month={fiscal.end_month} fiscal_year_named_for={fiscal.named_for} ({votes})")
            except NeedsSetup as exc:
                print(f"  derived: nothing ({exc})")
            derived = derive_company(payload, facts, hand.ticker if hand else None)
            print(f"  would set up: name={derived.name!r} ticker={derived.ticker} fiscal_year_end_month={derived.fiscal_year_end_month} "
                  f"fiscal_year_named_for={derived.fiscal_year_named_for} (tax_rate_parens_negative: column default, false)")
        except NeedsSetup as exc:
            print(f"  would park ({SETUP_REASON}): {exc}")
        except fetch.EdgarError as exc:
            print(f"  could not ask the SEC: {exc}")
            return 1
        if hand:
            print(f"  config.yaml has: ticker={hand.ticker} fiscal_year_end_month={hand.fiscal_year_end_month} "
                  f"fiscal_year_named_for={hand.fiscal_year_named_for} tax_rate_parens_negative={hand.tax_rate_parens_negative}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pipeline.worker", description="Build the companies visitors asked for, one at a time.")
    parser.add_argument("--once", action="store_true", help="one pass over the queue, then exit")
    parser.add_argument("--derive", nargs="+", metavar="CIK", help="print what would be set up for these CIKs from the SEC, and the 10-K facts behind it; writes nothing")
    args = parser.parse_args(argv)
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.derive:
        return show_derivation(args.derive)
    try:
        db = store.PostgresBackend()
        if not db._one("SELECT to_regclass('company_requests') IS NOT NULL"):
            raise store.StoreError("there is no company_requests table: apply db/migrations/0002_company_requests.sql first")
        worker = Worker(db, poll_seconds=_number("POLL_SECONDS", DEFAULT_POLL_SECONDS),
                        wait_minutes=_number("WORKER_WAIT_MINUTES", DEFAULT_WAIT_MINUTES))
    except (job.JobError, store.StoreError, ValueError) as exc:
        log.error("%s", exc)
        return 2
    return worker.run(once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
