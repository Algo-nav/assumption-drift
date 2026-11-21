"""One company, one job: the stages run against the database, under three spend caps.

    python -m pipeline.job run --cik CIK --kind build|refresh [--dry-run [--fetch]] [--sync] [--wait-minutes M] [--retry-parked] [--force]
    python -m pipeline.job add --cik CIK --ticker T --name NAME [--sector S] [--fiscal-year-end-month N] [--fiscal-year-named-for end|start]

A job always runs against Postgres (DATABASE_URL, STORE_SCHEMA; see research_record/store.py), whatever STORE says: the
stages are the same code, pointed at the database for the length of the job. The company comes from the `companies`
table, not from config.yaml; everything else (metrics, prompts, the EDGAR user agent and its request rate, model and
prices) is config.yaml, untouched. Nothing in a stage is changed by running it here.

  build     01_fetch (every filing since edgar.date_from), 02_candidates, 03_structure --submit, 05_review (the drafts
            are queued), 03b_verify --submit (the verifier checks the queued rows), 04_outcomes --submit,
            05_review --attach-outcomes (what 04 found goes onto the queued rows). The order the weekly refresh script
            uses; the job ends with the company's review rows queued, checked and carrying their outcomes. A company whose status is
            already `built` is refused (exit 2) unless --force is given: a build sends every request the current prompts
            have no archived answer for and replaces the company's drafts, which is not what a built company needs
  refresh   01_fetch --since-manifest (from the company's watermark in the database), then, if anything new was cached,
            02 and 03 on the new filings and 04 --from-review; one request at a time (--sync), as the weekly refresh does

A refresh stops at 04: queueing its new drafts (05_review) and checking them (03b_verify) are not part of it.

Caps, read from the environment. Each is checked before anything is sent, against the worst case of what is about to go:

  JOB_CAP_USD        default 1.50   what this job has spent
  DAILY_CAP_USD      default 8.00   what every job started today (UTC) has spent
  WORKSPACE_CAP_USD  default 70.00  every model call recorded this calendar month (UTC), jobs or not

A cap that would be passed stops the job with status `parked` and an error that names the cap and the numbers; nothing
is sent. config.yaml's lifetime `llm.budget_usd` still applies underneath, and stops a job the same way.

Every model call a job makes is a row in `llm_calls` with its cost and the job's id; `jobs.usd_spent` is their sum.

Jobs run one at a time: a job holds a database lock from start to finish and a second job waits for it, so EDGAR only
ever sees one client, at the rate config.yaml sets under the existing named user agent. A stage that exits non-zero
fails the job; a company whose last two jobs failed is `parked`, and is not run again without --retry-parked. A batch
that has not ended when --wait-minutes runs out leaves the job `waiting`; running the same command again resumes it.

--dry-run prices the job and calls nothing: it builds the requests each stage would send, looks each one's fingerprint up
in `llm_calls`, and counts an answered request as free, a request whose input tokens were counted before at that count,
and any other request by the offline estimate (marked as one). A later stage whose requests depend on answers that are
not there yet cannot be priced, and is reported as such. It creates no job and writes nothing, unless --fetch is given:
then 01 and 02 run first (EDGAR only, no model), so a company with nothing cached can be priced.

Each job's files are under data/_work/jobs/{id}/: the config it ran with, the log, the list of new filings.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import json
import os
import sys
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from pipeline import common, llm
from pipeline import manifest as manifest_step
from research_record import store

JOBS_DIR = common.WORK_DIR / "jobs"
CAP_DEFAULTS = {"JOB_CAP_USD": 1.50, "DAILY_CAP_USD": 8.00, "WORKSPACE_CAP_USD": 70.00}
#: A stage's exit code for "a batch is still processing; run again to resume" (03_structure, 03b_verify, 04_outcomes).
EXIT_BATCH_PENDING = 5
FAILURES_BEFORE_PARKED = 2


class JobError(Exception):
    """The job cannot start: no such company, a parked company, a bad argument."""


def caps_from_env(env: dict[str, str] | None = None) -> dict[str, float]:
    env = os.environ if env is None else env
    caps = {}
    for name, default in CAP_DEFAULTS.items():
        raw = env.get(name)
        try:
            caps[name] = float(raw) if raw not in (None, "") else default
        except ValueError as exc:
            raise JobError(f"{name} must be a number of dollars, got {raw!r}") from exc
        if caps[name] < 0:
            raise JobError(f"{name} must not be negative, got {raw!r}")
    return caps


class SpendGuard:
    """The three caps, for one job. pipeline.llm asks it before every send (`check`) and tells it what each call cost
    (`add`); `hit` is the reason the job must park, once there is one."""

    def __init__(self, db: Any, job_id: int, caps: dict[str, float], *, spent: float = 0.0,
                 today: Callable[[], date] = lambda: datetime.now(timezone.utc).date()) -> None:
        self.db, self.job_id, self.caps, self.spent, self.today = db, job_id, caps, float(spent), today
        self.hit: str | None = None

    def levels(self) -> list[tuple[str, float, str]]:
        """(cap, dollars already counted against it, what those dollars are), in the order they are checked."""
        day = self.today()
        return [
            ("JOB_CAP_USD", self.spent, "already spent by this job"),
            ("DAILY_CAP_USD", self.db.spent_by_jobs_started_on(day), f"already spent by the jobs started today ({day.isoformat()} UTC)"),
            ("WORKSPACE_CAP_USD", self.db.spent_in_month(day), f"already spent on model calls in {day.strftime('%B %Y')}"),
        ]

    def check(self, worst_usd: float, step: str = "") -> None:
        for name, used, what in self.levels():
            if used + worst_usd > self.caps[name] + 1e-9:
                self.hit = (f"{name}: {step + ': ' if step else ''}worst-case ${worst_usd:.4f} on top of ${used:.4f} {what} is more than "
                            f"the cap of ${self.caps[name]:.2f}. Nothing was sent.")
                raise llm.BudgetExceeded(self.hit)

    def exhausted(self) -> str | None:
        """The reason not to start at all: a cap that is already used up before this job sends anything."""
        for name, used, what in self.levels():
            if used >= self.caps[name] - 1e-9:
                return f"{name}: ${used:.4f} {what}, and the cap is ${self.caps[name]:.2f}. Nothing was sent."
        return None

    def add(self, usd: float) -> None:
        self.spent += usd
        self.db.add_job_spend(self.job_id, usd)

    def stopped(self, reason: str) -> None:
        self.hit = reason


# --- the company, as the stages want it -------------------------------------------------------------------------------


def company_entry(row: dict[str, Any]) -> dict[str, Any]:
    """A `companies` row as a config.yaml company entry."""
    entry = {"name": row["name"], "ticker": row["ticker"], "cik": row["cik"],
             "fiscal_year_named_for": row["fiscal_year_named_for"], "tax_rate_parens_negative": row["tax_rate_parens_negative"],
             "publish": row["publish"]}
    for key in ("fiscal_year_end_month", "unpublished_reason", "sector"):
        if row[key] is not None:
            entry[key] = row[key]
    return entry


def job_config(row: dict[str, Any], base: Path = common.CONFIG_PATH) -> dict[str, Any]:
    """config.yaml with its company list replaced by this one company. Everything else is as it is in the file."""
    with open(base, encoding="utf-8") as fh:
        config = yaml.safe_load(fh)
    config["companies"] = [company_entry(row)]
    return config


def write_job_config(row: dict[str, Any], folder: Path, base: Path = common.CONFIG_PATH) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "config.yaml"
    path.write_text(yaml.safe_dump(job_config(row, base), sort_keys=False, allow_unicode=True), encoding="utf-8")
    common.load_config(path)  # the same check every stage makes: the EDGAR user agent is the expected one
    return path


def write_manifest(row: dict[str, Any], path: Path) -> None:
    """The one-company manifest 01_fetch --since-manifest reads, from the company's watermark in the database."""
    if row["last_filing_date"] is None:
        raise JobError(f"{row['ticker']} has no refresh watermark (last_filing_date): it has not been built. Run --kind build first.")
    through = row["last_refreshed_at"].date() if row["last_refreshed_at"] else row["last_filing_date"]
    state = manifest_step.CompanyState(row["last_filing_date"], tuple(sorted(row["accessions_on_last_date"] or [])))
    manifest_step.save_manifest(manifest_step.Manifest(through, {row["cik"]: state}), path)


# --- stages -----------------------------------------------------------------------------------------------------------


@dataclass
class Stage:
    name: str
    module: str
    argv: list[str]

    def run(self) -> int:
        return int(importlib.import_module(self.module).main(self.argv) or 0)


@dataclass
class Plan:
    """What a job runs. `after_fetch` is asked once 01 has run and may drop the rest (a refresh that found nothing new)."""
    stages: list[Stage]
    after_fetch: Callable[[], bool] = lambda: True
    new_filings: Path | None = None


def plan_stages(kind: str, row: dict[str, Any], folder: Path, config_path: Path, *, sync: bool, wait_minutes: float) -> Plan:
    base = ["--config", str(config_path), "--company", row["ticker"]]
    send = ["--submit", "--wait-minutes", str(wait_minutes)]
    if kind == "build":
        one_by_one = ["--sync"] if sync else []
        return Plan([
            Stage("01_fetch", "pipeline.01_fetch", base),
            Stage("02_candidates", "pipeline.02_candidates", base),
            Stage("03_structure", "pipeline.03_structure", base + send + one_by_one),
            Stage("05_review", "pipeline.05_review", base),
            Stage("03b_verify", "pipeline.03b_verify", base + send + one_by_one),
            Stage("04_outcomes", "pipeline.04_outcomes", base + send + one_by_one),
            Stage("05_review_attach", "pipeline.05_review", base + ["--attach-outcomes"]),
        ])
    manifest_path, new_filings = folder / "manifest.json", folder / "new_filings.json"
    write_manifest(row, manifest_path)
    scope = ["--accessions", str(new_filings)]

    def anything_new() -> bool:
        return any(f.get("path") for f in json.loads(new_filings.read_text(encoding="utf-8")).get("filings", []))

    return Plan([
        Stage("01_fetch", "pipeline.01_fetch", base + ["--since-manifest", str(manifest_path), "--new-filings", str(new_filings)]),
        Stage("02_candidates", "pipeline.02_candidates", base + scope),
        Stage("03_structure", "pipeline.03_structure", base + scope + ["--sync"] + send),
        Stage("04_outcomes", "pipeline.04_outcomes", base + ["--from-review"] + scope + ["--sync"] + send),
    ], after_fetch=anything_new, new_filings=new_filings)


class _Tee(io.TextIOBase):
    def __init__(self, *streams: Any) -> None:
        self.streams = streams

    def write(self, text: str) -> int:
        for s in self.streams:
            s.write(text)
        return len(text)

    def flush(self) -> None:
        for s in self.streams:
            s.flush()


# --- running ----------------------------------------------------------------------------------------------------------


@dataclass
class JobResult:
    job_id: int | None
    status: str
    error: str | None = None
    usd_spent: float = 0.0
    stage: str | None = None
    log_path: Path | None = None
    lines: list[str] = field(default_factory=list)


def _parked_by_failures(db: Any, cik: str) -> bool:
    finished = [j for j in db.jobs(cik) if j["status"] in ("succeeded", "failed", "parked")]
    return len(finished) >= FAILURES_BEFORE_PARKED and all(j["status"] == "failed" for j in finished[:FAILURES_BEFORE_PARKED])


def _advance_watermark(db: Any, row: dict[str, Any], kind: str, plan: Plan, today: date) -> None:
    """After a job that succeeded: where the next refresh starts. A build takes it from the cached filings, a refresh
    from what 01 reported, by pipeline.manifest's own rules."""
    now = datetime.now(timezone.utc)
    cik = row["cik"]
    db.conn.execute("UPDATE companies SET first_built_at = COALESCE(first_built_at, %s) WHERE cik = %s", (now, cik))
    if kind == "build":
        try:
            state = manifest_step.bootstrap(common.RAW_DIR, [cik], today).companies[cik]
        except manifest_step.ManifestError:  # nothing cached for it: built, with no filing to start the next refresh from
            db.upsert_company(cik, status="built", last_refreshed_at=now)
            return
    elif plan is None or plan.new_filings is None or not plan.new_filings.exists():
        db.upsert_company(cik, status="built", last_refreshed_at=now)
        return
    else:
        before = manifest_step.Manifest(today, {cik: manifest_step.CompanyState(row["last_filing_date"], tuple(row["accessions_on_last_date"] or []))})
        payload = json.loads(plan.new_filings.read_text(encoding="utf-8"))
        after, report = manifest_step.advance(before, payload, run_date=today)
        state = after.companies[cik]
        if report[cik] not in ("advanced", "unchanged"):  # held: a filing did not fetch cleanly; look for it again next time
            db.upsert_company(cik, status="built")
            return
    db.upsert_company(cik, status="built", last_refreshed_at=now, last_filing_date=state.last_filing_date,
                      accessions_on_last_date=list(state.accessions_on_last_date))


def run_job(cik: str, kind: str, *, db: Any | None = None, caps: dict[str, float] | None = None, sync: bool = False,
            wait_minutes: float = 60, retry_parked: bool = False, force: bool = False, stages: Callable[..., Plan] | None = None,
            jobs_dir: Path | None = None, echo: bool = True, job_id: int | None = None,
            should_stop: Callable[[], bool] | None = None) -> JobResult:
    """Run one job to the end and record it. `stages`, for tests, replaces the real stage list: it is called with the same
    arguments as `plan_stages` plus `guard=` and `db=`, and returns a Plan.

    `job_id` and `should_stop` are for pipeline.worker. `job_id` names a jobs row the caller has already created for this
    company and kind (status running or waiting): the job runs as that row, and what the row has spent so far counts
    against its cap. `should_stop` is asked before each stage; once it says yes the job is left `waiting` at that stage
    boundary, exactly as a batch that has not ended leaves it, and the next run resumes it."""
    if kind not in ("build", "refresh"):
        raise JobError(f"--kind must be build or refresh, got {kind!r}")
    db = db or store.PostgresBackend()
    caps = caps or caps_from_env()
    cik = str(cik).zfill(10)
    with db.job_lock(), store.use(db):
        row = db.company(cik)
        if row is None:
            raise JobError(f"{cik} is not in the companies table. Add it first: python -m pipeline.job add --cik {cik} --ticker ... --name ...")
        if kind == "build" and row["status"] == "built" and not force:
            raise JobError(f"{row['ticker']} is already built. A build would send every request the current prompts have no archived "
                           "answer for and replace the company's drafts. Use --kind refresh to pick up new filings, or add --force "
                           "to build it again.")
        if row["status"] == "parked" and not retry_parked:
            raise JobError(f"{row['ticker']} is parked after {FAILURES_BEFORE_PARKED} failed jobs. Read the last job's error and log, "
                           "then run again with --retry-parked.")
        if kind == "refresh" and stages is None and row["last_filing_date"] is None:
            raise JobError(f"{row['ticker']} has no refresh watermark (last_filing_date): it has not been built. Run --kind build first.")
        waiting = next((j for j in db.jobs(cik) if j["status"] == "waiting" and j["kind"] == kind), None)
        if job_id is not None:
            waiting = next((j for j in db.jobs(cik) if j["id"] == job_id and j["kind"] == kind and j["status"] in ("running", "waiting")), None)
            if waiting is None:
                raise JobError(f"job {job_id} is not a running or waiting {kind} job of {row['ticker']}")
        job_id = waiting["id"] if waiting else db.create_job(cik, kind)
        folder = (jobs_dir or JOBS_DIR) / str(job_id)
        folder.mkdir(parents=True, exist_ok=True)
        log_path = folder / "job.log"
        try:
            shown = str(log_path.relative_to(common.REPO_ROOT))
        except ValueError:
            shown = str(log_path)
        db.update_job(job_id, status="running", log_path=shown, error=None, finished_at=None)
        previous_status = row["status"] if row["status"] != "building" else "requested"
        db.upsert_company(cik, status="building")
        guard = SpendGuard(db, job_id, caps, spent=float(waiting["usd_spent"]) if waiting else 0.0)
        db.job_id, db.job_cik = job_id, cik

        status, error, reached = "succeeded", None, None
        plan: Plan | None = None
        with log_path.open("a", encoding="utf-8") as log:
            out = _Tee(sys.stdout, log) if echo else log
            err = _Tee(sys.stderr, log) if echo else log
            print(f"== job {job_id}: {kind} {row['ticker']} ({cik}) {datetime.now(timezone.utc).isoformat(timespec='seconds')}"
                  f"{' (resumed)' if waiting and waiting['status'] == 'waiting' else ''}; caps " + ", ".join(f"{k}=${v:.2f}" for k, v in caps.items()), file=out)
            llm.GUARD = guard
            try:
                stop = guard.exhausted()
                if stop:
                    status, error = "parked", stop
                else:
                    config_path = write_job_config(row, folder)
                    plan = (stages or plan_stages)(kind, row, folder, config_path, sync=sync, wait_minutes=wait_minutes,
                                                   **({"guard": guard, "db": db} if stages else {}))
                    for stage in plan.stages:
                        if should_stop is not None and should_stop():
                            status, error = "waiting", (f"stopped before {stage.name}: the worker was asked to shut down. "
                                                        "Run the job again to resume; nothing is resubmitted.")
                            break
                        reached = stage.name
                        db.update_job(job_id, stage=reached)
                        print(f"\n== {stage.name}", file=out)
                        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                            code = stage.run()
                        print(f"exit {code}", file=out)
                        if guard.hit:
                            status, error = "parked", guard.hit
                        elif code == EXIT_BATCH_PENDING:
                            status, error = "waiting", f"{stage.name}: a batch is still processing. Run the same command again to resume; nothing is resubmitted."
                        elif code != 0:
                            status, error = "failed", f"{stage.name} exited {code}. See {shown}."
                        if status != "succeeded":
                            break
                        if stage.name == "01_fetch" and not plan.after_fetch():
                            print("no new filing with a document: nothing to draft", file=out)
                            break
            except Exception as exc:  # noqa: BLE001 - whatever a stage raised is the job's error, and the job is recorded as failed
                traceback.print_exc(file=log)
                status, error = ("parked", guard.hit) if guard.hit else ("failed", f"{reached or 'setup'}: {type(exc).__name__}: {str(exc)[:300]}")
            finally:
                llm.GUARD = None
                db.job_id = db.job_cik = None
            print(f"\n== job {job_id}: {status}" + (f": {error}" if error else "") + f"; spent ${guard.spent:.4f}", file=out)

        finished = None if status == "waiting" else datetime.now(timezone.utc)
        db.update_job(job_id, status=status, error=error, finished_at=finished, stage=reached)
        if status == "succeeded":
            _advance_watermark(db, row, kind, plan, datetime.now(timezone.utc).date())
        elif status == "waiting":
            pass  # still building
        elif status == "failed" and _parked_by_failures(db, cik):
            db.upsert_company(cik, status="parked")
            error = f"{error} This is the company's second failed job in a row: the company is parked."
            db.update_job(job_id, error=error)
        else:  # a cap parked the job, or a first failure: the company is what it was before the job
            db.upsert_company(cik, status=previous_status)
        return JobResult(job_id, status, error, float(db.job(job_id)["usd_spent"]), reached, log_path)


# --- the dry run --------------------------------------------------------------------------------------------------------


@dataclass
class StagePrice:
    step: str
    requests: int = 0
    answered: int = 0          # an answer to this exact request is archived: free
    counted: int = 0           # to send, input tokens counted before
    estimated: int = 0         # to send, input tokens estimated offline
    input_tokens: int = 0
    usd_expected: float = 0.0
    usd_worst: float = 0.0
    usd_already_paid: float = 0.0   # what the archived answers cost when they were bought
    note: str = ""


def price_requests(db: Any, cfg: llm.LlmConfig, step: str, requests: list[llm.LlmRequest], expected_output: int, *, sync: bool
                   ) -> tuple[StagePrice, dict[str, llm.Result]]:
    """Price one stage's requests from what the database holds for their fingerprints. Also the archived answers to them,
    for the stage after."""
    have = llm.current_results(llm.RawArchive(step).load(), requests, cfg.model)
    todo = [r for r in requests if not (r.custom_id in have and have[r.custom_id].ok)]
    known = db.priced([fp for r in requests for fp in (r.fingerprint(cfg.model), r.legacy_fingerprint(cfg.model))])
    price = StagePrice(step, requests=len(requests), answered=len(requests) - len(todo))
    for r in requests:
        if r.custom_id in have and have[r.custom_id].ok:
            hit = known.get(have[r.custom_id].fingerprint or "")
            price.usd_already_paid += hit["usd"] if hit else 0.0
    estimates = dict(zip((r.custom_id for r in todo), llm.estimate_tokens(todo)))
    one_by_one = sync or len(todo) < cfg.sync_below
    for r in todo:
        hit = known.get(r.fingerprint(cfg.model)) or known.get(r.legacy_fingerprint(cfg.model))
        if hit and hit["input_tokens"]:
            tokens, price.counted = hit["input_tokens"], price.counted + 1
        else:
            tokens, price.estimated = estimates[r.custom_id], price.estimated + 1
        price.input_tokens += tokens
        price.usd_expected += cfg.usd(tokens, expected_output, batch=not one_by_one)
        price.usd_worst += cfg.usd(tokens, r.max_tokens, batch=not one_by_one)
    if todo:
        price.note = "one by one, full price" if one_by_one else "as a batch, batch price (the one canary request is priced as batch too)"
    return price, have


def dry_run(cik: str, kind: str, *, db: Any | None = None, caps: dict[str, float] | None = None, sync: bool = False,
            fetch: bool = False, jobs_dir: Path | None = None) -> tuple[list[StagePrice], list[str]]:
    """(a price per model stage, lines to print). No model call, no count_tokens call, no job row."""
    db = db or store.PostgresBackend()
    caps = caps or caps_from_env()
    cik = str(cik).zfill(10)
    lines: list[str] = []
    with store.use(db):
        row = db.company(cik)
        if row is None:
            raise JobError(f"{cik} is not in the companies table. Add it first: python -m pipeline.job add --cik {cik} --ticker ... --name ...")
        folder = (jobs_dir or JOBS_DIR) / f"dry-{cik}"
        config_path = write_job_config(row, folder)
        config = common.load_config(config_path)
        company = common.companies(config)[0]
        cfg = llm.LlmConfig.from_config(config["llm"])
        structure = importlib.import_module("pipeline.03_structure")
        outcomes_step = importlib.import_module("pipeline.04_outcomes")
        candidates_step = importlib.import_module("pipeline.02_candidates")

        scope: dict[str, set[str]] | None = None
        if fetch:
            plan = plan_stages(kind, row, folder, config_path, sync=sync, wait_minutes=0)
            with db.job_lock():
                for stage in plan.stages[:2]:
                    lines.append(f"--fetch: running {stage.name} (EDGAR only, no model)")
                    code = stage.run()
                    if code != 0:
                        raise JobError(f"{stage.name} exited {code}; nothing was priced")
                    if stage.name == "01_fetch" and not plan.after_fetch():
                        return [], lines + ["no new filing with a document: a refresh would send nothing ($0.00)"]
            if plan.new_filings is not None:
                scope = common.load_accession_scope(plan.new_filings)
        elif kind == "refresh":
            raise JobError("a refresh can only be priced once 01 has looked for new filings: add --fetch (EDGAR only, no model)")
        if not db.exists(common.CANDIDATES_DIR / f"{cik}.jsonl"):
            raise JobError(f"{row['ticker']} has no candidates in the database, so there is nothing to price. "
                           "Add --fetch to run 01 and 02 first (EDGAR only, no model).")

        prices: list[StagePrice] = []
        requests, index, _ = structure.build_requests([company], {"8-K"}, metrics=config["metrics"], accessions=scope)
        price, results = price_requests(db, cfg, structure.STEP, requests, structure.EXPECTED_OUTPUT_TOKENS, sync=sync or kind == "refresh")
        prices.append(price)

        if kind == "build":
            prices.append(StagePrice("03b_verify", note="depends on the review rows 05 queues from stage 03's drafts (one request a row); not priced"))
        later = (outcomes_step.STEP_OUTCOMES, outcomes_step.STEP_ACK, outcomes_step.STEP_WITHDRAWAL)
        if kind == "refresh":
            prices += [StagePrice(s, note="depends on 03's answers for the new filings and on the review queue; not priced") for s in later]
        elif price.answered < price.requests:
            why = f"needs the drafts, and {price.requests - price.answered:,} of {price.requests:,} stage-03 requests have no archived answer yet"
            prices += [StagePrice(s, note=f"cannot be priced: {why}") for s in later]
        else:
            drafts, _, _ = structure.derive_drafts(index, results, config["metrics"], config["metric_kinds"])
            ocfg = config["outcomes"]
            number = candidates_step.Settings.from_config(config["candidates"]).number
            today = date.today()
            docs = {cik: outcomes_step.DocStore(company)}
            by_cik = {cik: drafts.get(cik, [])}
            o_requests, o_index, _ = outcomes_step.build_outcome_requests(by_cik, docs, ocfg, number, today=today, as_of=today)
            o_price, o_results = price_requests(db, cfg, later[0], o_requests, outcomes_step.EXPECTED_OUTPUT_TOKENS, sync=sync)
            prices.append(o_price)
            if o_price.answered < o_price.requests:
                prices.append(StagePrice(later[1], note="cannot be priced: which rows are misses depends on outcome answers that are not archived yet"))
            else:
                found, _, _ = outcomes_step.derive_outcomes(o_index, o_results, docs, ocfg)
                k_requests, _ = outcomes_step.build_ack_requests(by_cik, found, docs, ocfg, number, today=today)
                prices.append(price_requests(db, cfg, later[1], k_requests, outcomes_step.EXPECTED_OUTPUT_TOKENS, sync=sync)[0])
            w_requests, _ = outcomes_step.build_withdrawal_requests(by_cik, {cik: company}, docs, ocfg, today=today)
            prices.append(price_requests(db, cfg, later[2], w_requests, outcomes_step.EXPECTED_OUTPUT_TOKENS, sync=sync)[0])
            lines.append(f"{len(drafts.get(cik, [])):,} drafts derived from the archived stage-03 answers (in memory; nothing was written)")

        lines.append(f"dry run: {kind} {row['ticker']} ({cik}), priced from llm_calls; no model call, no token count call")
        lines.append(f"{'stage':<20} {'requests':>8} {'answered':>8} {'to send':>8} {'counted':>8} {'estimated':>9} {'input tok':>10} {'expected $':>10} {'worst $':>9}")
        for p in prices:
            if p.note.startswith(("cannot", "depends")):
                lines.append(f"{p.step:<20} {'-':>8} {'-':>8} {'-':>8} {'-':>8} {'-':>9} {'-':>10} {'-':>10} {'-':>9}   {p.note}")
                continue
            lines.append(f"{p.step:<20} {p.requests:>8,} {p.answered:>8,} {p.requests - p.answered:>8,} {p.counted:>8,} {p.estimated:>9,} "
                         f"{p.input_tokens:>10,} {p.usd_expected:>10.4f} {p.usd_worst:>9.4f}" + (f"   {p.note}" if p.note else ""))
        expected, worst = sum(p.usd_expected for p in prices), sum(p.usd_worst for p in prices)
        paid = sum(p.usd_already_paid for p in prices)
        unpriced = [p.step for p in prices if p.note.startswith(("cannot", "depends"))]
        lines.append(f"{'total to send':<20} {'':>54} {expected:>10.4f} {worst:>9.4f}" + (f"   (without {', '.join(unpriced)})" if unpriced else ""))
        lines.append(f"the archived answers these requests would reuse cost ${paid:.4f} when they were bought")
        if any(p.estimated for p in prices):
            lines.append("estimated = input tokens guessed offline at 3 characters a token, which errs high; counted = from an earlier count_tokens")
        guard = SpendGuard(db, 0, caps)
        for name, used, what in guard.levels():
            verdict = "within the cap" if used + worst <= caps[name] + 1e-9 else "WOULD PARK"
            lines.append(f"  {name} ${caps[name]:.2f}: ${used:.4f} {what}; worst case ${worst:.4f} on top: {verdict}")
        return prices, lines


# --- command line -----------------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pipeline.job", description="Run the pipeline for one company against the database, under spend caps.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run, or with --dry-run price, one job")
    run.add_argument("--cik", required=True)
    run.add_argument("--kind", required=True, choices=["build", "refresh"])
    run.add_argument("--dry-run", action="store_true", help="price the job from the fingerprints in llm_calls; call nothing, record nothing")
    run.add_argument("--fetch", action="store_true", help="with --dry-run: run 01 and 02 first (EDGAR only), so there are candidates to price")
    run.add_argument("--sync", action="store_true", help="build: send every request one by one at full price, not as a batch")
    run.add_argument("--wait-minutes", type=float, default=60, help="how long to wait for a batch before leaving the job waiting")
    run.add_argument("--retry-parked", action="store_true", help="run a company that is parked after two failed jobs")
    run.add_argument("--force", action="store_true", help="build a company whose status is already built")
    add = sub.add_parser("add", help="put a company in the companies table with status requested")
    add.add_argument("--cik", required=True)
    add.add_argument("--ticker", required=True)
    add.add_argument("--name", required=True)
    add.add_argument("--sector")
    add.add_argument("--fiscal-year-end-month", type=int, choices=range(1, 13), metavar="1-12")
    add.add_argument("--fiscal-year-named-for", choices=["end", "start"], default="end")
    args = parser.parse_args(argv)

    try:
        if args.command == "add":
            db = store.PostgresBackend()
            cik = args.cik.zfill(10)
            if db.company(cik):
                raise JobError(f"{cik} is already in the companies table")
            db.upsert_company(cik, ticker=args.ticker.upper(), name=args.name, sector=args.sector, status="requested",
                              fiscal_year_end_month=args.fiscal_year_end_month, fiscal_year_named_for=args.fiscal_year_named_for)
            print(f"added {args.ticker.upper()} ({cik}) with status requested")
            return 0
        if args.dry_run:
            _, lines = dry_run(args.cik, args.kind, sync=args.sync, fetch=args.fetch)
            print("\n".join(lines))
            return 0
        if args.fetch:
            raise JobError("--fetch only goes with --dry-run")
        result = run_job(args.cik, args.kind, sync=args.sync, wait_minutes=args.wait_minutes, retry_parked=args.retry_parked,
                         force=args.force)
    except (JobError, store.StoreError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return {"succeeded": 0, "waiting": EXIT_BATCH_PENDING, "parked": 4}.get(result.status, 1)


if __name__ == "__main__":
    raise SystemExit(main())
