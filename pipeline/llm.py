"""Shared plumbing for the Haiku batch steps (03_structure, 04_outcomes).

Nothing else in the pipeline may call a model. Every call goes through here, so
the same rules apply to all of them:

  * one budget across every step, checked against the WORST case (each request
    running to max_tokens) before anything is submitted;
  * input tokens counted by the API, not guessed, when credentials exist;
  * one synchronous canary request first, so a bad parameter fails on one
    request instead of on the whole batch;
  * every raw model output archived under data/batches/, so nothing is paid for
    twice and every draft can be re-derived without another call;
  * batches are resumable: re-running never submits a request that already
    succeeded, and never submits a second batch while one is pending.

Credentials: the SDK's own chain (ANTHROPIC_API_KEY, an `ant auth login` profile),
plus ANTHROPIC_API_KEY read from a git-ignored .env at the repo root.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import anthropic

from pipeline.common import REPO_ROOT

BATCH_DIR = REPO_ROOT / "data" / "batches"  # read at call time, so tests can point it elsewhere

_CUSTOM_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
#: Deliberately low, so the offline estimate errs on the high side.
CHARS_PER_TOKEN_ESTIMATE = 3.0


class BudgetExceeded(RuntimeError):
    """The projected worst-case cost is more than the budget has left."""


class NoCredentials(RuntimeError):
    """No API credentials could be found."""


# --- credentials and client ------------------------------------------------


def load_env(path: Path = REPO_ROOT / ".env") -> None:
    """Read ANTHROPIC_API_KEY from a .env file if the environment does not already have one."""
    if os.environ.get("ANTHROPIC_API_KEY") or not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("ANTHROPIC_API_KEY="):
            value = line.split("=", 1)[1].strip().strip("'\"")
            if value:
                os.environ["ANTHROPIC_API_KEY"] = value
            return


def make_client() -> Any:
    load_env()
    return anthropic.Anthropic()


def credentials_available(client: Any, model: str) -> bool:
    """True when a free count_tokens call authenticates. Never prints or returns the key."""
    try:
        client.messages.count_tokens(model=model, messages=[{"role": "user", "content": "ping"}])
        return True
    except TypeError as exc:  # the SDK raises this when no auth method can be resolved
        if "authentication" in str(exc).lower():
            return False
        raise
    except anthropic.AuthenticationError:
        return False


# --- requests, pricing, projection ----------------------------------------


@dataclass(frozen=True)
class LlmConfig:
    model: str
    budget_usd: float
    price_input: float  # USD per million tokens
    price_output: float
    batch_discount: float

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "LlmConfig":
        return cls(
            model=cfg["model"],
            budget_usd=float(cfg["budget_usd"]),
            price_input=float(cfg["price_input_per_mtok"]),
            price_output=float(cfg["price_output_per_mtok"]),
            batch_discount=float(cfg["batch_discount"]),
        )

    def usd(self, input_tokens: int, output_tokens: int, *, batch: bool = True) -> float:
        cost = (input_tokens * self.price_input + output_tokens * self.price_output) / 1_000_000
        return cost * (self.batch_discount if batch else 1.0)


@dataclass(frozen=True)
class LlmRequest:
    custom_id: str
    system: str
    user: str
    max_tokens: int
    schema: dict[str, Any]

    def __post_init__(self) -> None:
        if not _CUSTOM_ID.match(self.custom_id):
            raise ValueError(f"custom_id {self.custom_id!r} must match {_CUSTOM_ID.pattern}")

    def params(self, model: str) -> dict[str, Any]:
        return {
            "model": model,
            "max_tokens": self.max_tokens,
            "system": self.system,
            "messages": [{"role": "user", "content": self.user}],
            "output_config": {"format": {"type": "json_schema", "schema": self.schema}},
        }

    def fingerprint(self, model: str) -> str:
        return hashlib.sha256(json.dumps(self.params(model), sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class Projection:
    requests: int
    input_tokens: int
    exact: bool  # counted by the API (True) or estimated offline (False)
    max_output_tokens: int
    expected_output_tokens: int
    usd_expected: float
    usd_worst: float

    def describe(self, label: str, *, cap: float, committed: float) -> str:
        remaining = cap - committed
        verdict = "within the cap" if self.usd_worst <= remaining else "EXCEEDS THE CAP"
        return (
            f"{label}: {self.requests:,} requests, {self.input_tokens:,} input tokens "
            f"({'counted by the API' if self.exact else 'ESTIMATE, not an API count'}), "
            f"output capped at {self.max_output_tokens:,} tokens in total\n"
            f"  projected cost at Batch prices: expected ${self.usd_expected:.2f}, "
            f"worst case ${self.usd_worst:.2f}\n"
            f"  budget ${cap:.2f}, already committed ${committed:.2f}, remaining ${remaining:.2f}: {verdict}"
        )


def estimate_tokens(requests: list[LlmRequest]) -> list[int]:
    return [
        math.ceil((len(r.system) + len(r.user) + len(json.dumps(r.schema))) / CHARS_PER_TOKEN_ESTIMATE)
        for r in requests
    ]


def project(
    requests: list[LlmRequest],
    input_counts: list[int],
    *,
    exact: bool,
    llm: LlmConfig,
    expected_output_per_request: int,
) -> Projection:
    max_out = sum(r.max_tokens for r in requests)
    expected_out = expected_output_per_request * len(requests)
    total_in = sum(input_counts)
    return Projection(
        requests=len(requests),
        input_tokens=total_in,
        exact=exact,
        max_output_tokens=max_out,
        expected_output_tokens=expected_out,
        usd_expected=llm.usd(total_in, expected_out),
        usd_worst=llm.usd(total_in, max_out),
    )


def count_input_tokens(
    client: Any,
    llm: LlmConfig,
    requests: list[LlmRequest],
    *,
    cache_path: Path | None = None,
    workers: int = 4,
) -> list[int]:
    """Exact input tokens per request, from the API's count_tokens endpoint. Cached by request content."""
    cache_path = cache_path or BATCH_DIR / "token_cache.json"
    try:
        cache: dict[str, int] = json.loads(cache_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}

    def count(req: LlmRequest) -> int:
        key = req.fingerprint(llm.model)
        if key not in cache:
            p = req.params(llm.model)
            resp = client.messages.count_tokens(
                model=p["model"], system=p["system"], messages=p["messages"], output_config=p["output_config"]
            )
            cache[key] = int(resp.input_tokens)
        return cache[key]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        counts = list(pool.map(count, requests))
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache))
    return counts


# --- ledger ----------------------------------------------------------------


class Ledger:
    """Append-only record of what has been committed. One budget, every step."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or BATCH_DIR / "ledger.jsonl"

    def append(self, **entry: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    def entries(self) -> list[dict[str, Any]]:
        try:
            return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]
        except FileNotFoundError:
            return []

    def committed_usd(self) -> float:
        """Canary calls, plus per batch its latest entry: worst case once submitted, actual once ended."""
        total = 0.0
        per_batch: dict[str, float] = {}
        for entry in self.entries():
            if entry["kind"] == "canary":
                total += entry["usd"]
            else:
                per_batch[entry["batch_id"]] = entry["usd"]
        return total + sum(per_batch.values())


def check_budget(projection: Projection, llm: LlmConfig, ledger: Ledger) -> None:
    remaining = llm.budget_usd - ledger.committed_usd()
    if projection.usd_worst > remaining:
        raise BudgetExceeded(
            f"worst-case ${projection.usd_worst:.2f} is more than the ${remaining:.2f} left of the "
            f"${llm.budget_usd:.2f} budget. Nothing was submitted."
        )


# --- results ---------------------------------------------------------------


@dataclass(frozen=True)
class Result:
    custom_id: str
    status: str  # succeeded | errored | canceled | expired | truncated
    text: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None
    batch_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"


def _message_result(custom_id: str, message: Any, batch_id: str) -> Result:
    text = next((b.text for b in message.content if getattr(b, "type", None) == "text"), None)
    usage = message.usage
    status = "truncated" if getattr(message, "stop_reason", None) == "max_tokens" else "succeeded"
    if text is None:
        status = "errored"
    return Result(custom_id, status, text, usage.input_tokens, usage.output_tokens, None, batch_id)


class RawArchive:
    """data/batches/{step}.raw.jsonl: every model output ever paid for, append only."""

    def __init__(self, step: str, directory: Path | None = None) -> None:
        directory = directory or BATCH_DIR
        self.path = directory / f"{step}.raw.jsonl"
        self.state_path = directory / f"{step}.state.json"

    def load(self) -> dict[str, Result]:
        """Latest result per custom_id; a later failure never replaces an earlier success."""
        results: dict[str, Result] = {}
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return results
        for line in lines:
            if not line.strip():
                continue
            r = Result(**json.loads(line))
            if r.custom_id in results and results[r.custom_id].ok and not r.ok:
                continue
            results[r.custom_id] = r
        return results

    def append(self, results: list[Result]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            for r in results:
                fh.write(json.dumps(r.__dict__, ensure_ascii=False) + "\n")


def parse_json_text(text: str | None) -> Any:
    """json.loads that tolerates a code fence. None if it does not parse."""
    if text is None:
        return None
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


# --- the batch runner ------------------------------------------------------


@dataclass
class BatchOutcome:
    results: dict[str, Result]  # every result ever archived for this step
    submitted: int = 0
    pending_batch: str | None = None  # set when we stopped waiting
    notes: list[str] = field(default_factory=list)


def run_batch(
    client: Any,
    llm: LlmConfig,
    step: str,
    requests: list[LlmRequest],
    *,
    projection: Projection,
    canary_check: Callable[[str], None],
    ledger: Ledger | None = None,
    archive: RawArchive | None = None,
    wait_seconds: float = 3600,
    poll_seconds: float = 30,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] = print,
) -> BatchOutcome:
    """Submit what has no successful result yet, wait, archive. Safe to run again."""
    ledger = ledger or Ledger()
    archive = archive or RawArchive(step)
    have = archive.load()
    outcome = BatchOutcome(results=have)

    batch_id: str | None = None
    if archive.state_path.exists():
        batch_id = json.loads(archive.state_path.read_text())["batch_id"]
        log(f"{step}: resuming pending batch {batch_id}; nothing new is submitted")
    else:
        todo = [r for r in requests if not (r.custom_id in have and have[r.custom_id].ok)]
        if not todo:
            log(f"{step}: every request already has a successful result")
            return outcome
        check_budget(projection, llm, ledger)

        canary, batch_reqs = todo[0], todo[1:]
        message = client.messages.create(**canary.params(llm.model))
        first = _message_result(canary.custom_id, message, "canary")
        check_error: Exception | None = None
        if first.ok:
            try:
                canary_check(first.text or "")
            except Exception as exc:  # noqa: BLE001 - re-raised below, after the paid-for result is safe
                check_error = exc
                first = Result(first.custom_id, "invalid", first.text, first.input_tokens, first.output_tokens, str(exc)[:200], "canary")
        # Archive and cost it before anything can raise: it was paid for either way.
        archive.append([first])
        outcome.results[first.custom_id] = first
        ledger.append(kind="canary", step=step, batch_id="canary", usd=llm.usd(first.input_tokens, first.output_tokens, batch=False))
        if check_error is not None:
            raise check_error
        log(f"{step}: canary request ok ({first.input_tokens} in, {first.output_tokens} out)")
        if not first.ok:
            outcome.notes.append(f"canary {first.custom_id} came back {first.status}")
            return outcome
        if not batch_reqs:
            return outcome

        batch = client.messages.batches.create(
            requests=[{"custom_id": r.custom_id, "params": r.params(llm.model)} for r in batch_reqs]
        )
        batch_id = batch.id
        archive.state_path.parent.mkdir(parents=True, exist_ok=True)
        archive.state_path.write_text(json.dumps({"batch_id": batch_id, "requests": len(batch_reqs)}))
        ledger.append(kind="submitted", step=step, batch_id=batch_id, usd=projection.usd_worst, requests=len(batch_reqs))
        outcome.submitted = len(batch_reqs)
        log(f"{step}: submitted batch {batch_id} with {len(batch_reqs):,} requests")

    deadline = clock() + wait_seconds
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        if batch.processing_status == "ended":
            break
        if clock() >= deadline:
            outcome.pending_batch = batch_id
            log(f"{step}: batch {batch_id} still processing ({batch.request_counts.processing:,} left). "
                "Run the same command again to resume; nothing will be resubmitted.")
            return outcome
        sleep(poll_seconds)

    collected: list[Result] = []
    for item in client.messages.batches.results(batch_id):
        res = item.result
        if res.type == "succeeded":
            collected.append(_message_result(item.custom_id, res.message, batch_id))
        else:
            error = getattr(getattr(res, "error", None), "type", None)
            collected.append(Result(item.custom_id, res.type, None, 0, 0, error, batch_id))
    archive.append(collected)
    for r in collected:
        if r.custom_id in outcome.results and outcome.results[r.custom_id].ok and not r.ok:
            continue
        outcome.results[r.custom_id] = r
    actual = llm.usd(sum(r.input_tokens for r in collected), sum(r.output_tokens for r in collected))
    ledger.append(kind="ended", step=step, batch_id=batch_id, usd=actual, succeeded=sum(r.ok for r in collected))
    archive.state_path.unlink(missing_ok=True)
    failed = [r for r in collected if not r.ok]
    log(f"{step}: batch ended, {len(collected) - len(failed):,} succeeded, {len(failed):,} did not; actual cost ${actual:.2f}")
    if failed:
        outcome.notes.append(f"{len(failed)} requests did not succeed; run again to retry them")
    return outcome
