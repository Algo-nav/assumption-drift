"""pipeline/llm.py: the cost gate, the ledger, and the batch runner, against a fake client."""

from __future__ import annotations

import json

import pytest

import fakes
from pipeline import llm

SCHEMA = {"type": "object", "properties": {"items": {"type": "array"}}, "required": ["items"], "additionalProperties": False}
CFG = llm.LlmConfig(model="claude-haiku-4-5", budget_usd=10.0, price_input=1.0, price_output=5.0, batch_discount=0.5)


def reqs(n: int, max_tokens: int = 700) -> list[llm.LlmRequest]:
    return [llm.LlmRequest(f"r-{i}", "system text", f"user text {i}", max_tokens, SCHEMA) for i in range(n)]


@pytest.fixture(autouse=True)
def batch_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "BATCH_DIR", tmp_path / "batches")
    return tmp_path / "batches"


def ok(text: str) -> None:
    assert json.loads(text)["items"] == []


def projection_for(requests, tokens=1000, exact=True):
    return llm.project(requests, [tokens] * len(requests), exact=exact, llm=CFG, expected_output_per_request=100)


# --- requests and pricing --------------------------------------------------


def test_custom_ids_must_fit_the_batch_api_rules() -> None:
    with pytest.raises(ValueError):
        llm.LlmRequest("has spaces", "s", "u", 10, SCHEMA)
    with pytest.raises(ValueError):
        llm.LlmRequest("x" * 65, "s", "u", 10, SCHEMA)
    llm.LlmRequest("c-0001045810-000104581025000228-2527", "s", "u", 10, SCHEMA)


def test_params_carry_the_schema_and_no_effort_or_thinking() -> None:
    params = reqs(1)[0].params("claude-haiku-4-5")
    assert params["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}}
    assert "thinking" not in params and "effort" not in params["output_config"]  # Haiku 4.5 rejects effort


def test_projection_prices_at_the_batch_discount() -> None:
    p = projection_for(reqs(1000), tokens=2000)
    # 1000 requests: 2,000,000 in and 100,000 expected out at half price; worst case is 700,000 out.
    assert p.usd_expected == pytest.approx((2_000_000 * 1.0 + 100_000 * 5.0) / 1e6 * 0.5)
    assert p.usd_worst == pytest.approx((2_000_000 * 1.0 + 700_000 * 5.0) / 1e6 * 0.5)
    assert p.usd_worst > p.usd_expected


def test_the_offline_estimate_errs_high() -> None:
    r = llm.LlmRequest("a", "s" * 300, "u" * 300, 10, SCHEMA)
    assert llm.estimate_tokens([r])[0] >= (600 + len(json.dumps(SCHEMA))) / 4  # even a 4 chars/token tokenizer


def test_describe_says_when_the_number_is_only_an_estimate() -> None:
    p = projection_for(reqs(3), exact=False)
    assert "ESTIMATE" in p.describe("step", cap=10, committed=0)
    assert "counted by the API" in projection_for(reqs(3)).describe("step", cap=10, committed=0)


# --- credentials -----------------------------------------------------------


def test_missing_credentials_are_detected_without_raising() -> None:
    assert llm.credentials_available(fakes.FakeAnthropic(authenticated=False), CFG.model) is False
    assert llm.credentials_available(fakes.FakeAnthropic(), CFG.model) is True


def test_load_env_reads_the_key_from_a_dotenv_but_never_overrides_the_environment(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OTHER=1\nANTHROPIC_API_KEY='sk-test-123'\n")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    llm.load_env(env_file)
    import os

    assert os.environ["ANTHROPIC_API_KEY"] == "sk-test-123"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "already-set")
    llm.load_env(env_file)
    assert os.environ["ANTHROPIC_API_KEY"] == "already-set"


# --- token counting --------------------------------------------------------


def test_input_tokens_are_counted_by_the_api_once_and_then_cached() -> None:
    client = fakes.FakeAnthropic(input_tokens=1234)
    requests = reqs(5)
    assert llm.count_input_tokens(client, CFG, requests) == [1234] * 5
    assert client.messages.count_calls == 5
    assert llm.count_input_tokens(client, CFG, requests) == [1234] * 5
    assert client.messages.count_calls == 5  # served from the cache


# --- ledger and budget -----------------------------------------------------


def test_the_ledger_counts_canaries_and_replaces_a_worst_case_with_the_actual() -> None:
    ledger = llm.Ledger()
    ledger.append(kind="canary", step="s", batch_id="canary", usd=0.01)
    ledger.append(kind="submitted", step="s", batch_id="b1", usd=4.00)
    assert ledger.committed_usd() == pytest.approx(4.01)
    ledger.append(kind="ended", step="s", batch_id="b1", usd=1.25)
    assert ledger.committed_usd() == pytest.approx(1.26)
    ledger.append(kind="submitted", step="t", batch_id="b2", usd=3.00)
    assert ledger.committed_usd() == pytest.approx(4.26)


def test_the_gate_uses_the_worst_case_against_what_is_left() -> None:
    ledger = llm.Ledger()
    ledger.append(kind="submitted", step="s", batch_id="b1", usd=7.00)
    within = llm.Projection(1, 1, True, 1, 1, 1.0, 3.00)
    over = llm.Projection(1, 1, True, 1, 1, 1.0, 3.01)
    llm.check_budget(within, CFG, ledger)
    with pytest.raises(llm.BudgetExceeded, match="Nothing was submitted"):
        llm.check_budget(over, CFG, ledger)


# --- the batch runner ------------------------------------------------------


def run(client, requests, **kw):
    kw.setdefault("projection", projection_for(requests))
    return llm.run_batch(client, CFG, "step", requests, canary_check=ok, sleep=lambda s: None, log=lambda m: None, **kw)


def test_a_canary_goes_first_then_the_rest_as_one_batch_and_everything_is_archived() -> None:
    client = fakes.FakeAnthropic()
    outcome = run(client, reqs(5))
    assert len(client.messages.create_calls) == 1  # the canary, synchronous
    assert [item["custom_id"] for item in client.messages.batches.created[0]] == ["r-1", "r-2", "r-3", "r-4"]
    assert set(outcome.results) == {f"r-{i}" for i in range(5)} and all(r.ok for r in outcome.results.values())
    assert set(llm.RawArchive("step").load()) == set(outcome.results)
    assert not llm.RawArchive("step").state_path.exists()  # the pending marker is cleared


def test_the_ledger_ends_at_the_actual_cost_not_the_projection() -> None:
    client = fakes.FakeAnthropic(input_tokens=1000, output_tokens=100)
    run(client, reqs(5))
    canary = (1000 * 1.0 + 100 * 5.0) / 1e6  # full price
    batch = 4 * (1000 * 1.0 + 100 * 5.0) / 1e6 * 0.5  # half price
    assert llm.Ledger().committed_usd() == pytest.approx(canary + batch)


def test_over_budget_submits_nothing_at_all() -> None:
    client = fakes.FakeAnthropic()
    huge = llm.Projection(5, 10**9, True, 10**6, 10**5, 500.0, 900.0)
    with pytest.raises(llm.BudgetExceeded):
        run(client, reqs(5), projection=huge)
    assert client.messages.create_calls == [] and client.messages.batches.created == []


def test_a_bad_canary_stops_before_the_batch_is_built_but_is_still_archived_and_costed() -> None:
    client = fakes.FakeAnthropic(respond=lambda p: "not json at all")
    with pytest.raises(json.JSONDecodeError):  # ok() fails on it, standing in for the real check
        run(client, reqs(5))
    assert client.messages.batches.created == []
    archived = llm.RawArchive("step").load()["r-0"]
    assert archived.status == "invalid" and not archived.ok  # so it is retried, not treated as done
    assert llm.Ledger().committed_usd() > 0  # it was paid for


def test_a_rerun_sends_nothing_that_already_succeeded() -> None:
    client = fakes.FakeAnthropic()
    requests = reqs(5)
    run(client, requests)
    again = fakes.FakeAnthropic()
    outcome = run(again, requests)
    assert again.messages.create_calls == [] and again.messages.batches.created == []
    assert len(outcome.results) == 5


def test_failed_requests_are_retried_on_the_next_run_and_a_success_is_never_overwritten() -> None:
    requests = reqs(4)
    run(fakes.FakeAnthropic(fail_ids=("r-2",)), requests)
    archived = llm.RawArchive("step").load()
    assert not archived["r-2"].ok and archived["r-1"].ok

    retry = fakes.FakeAnthropic()
    run(retry, requests)
    # r-2 alone is left: it goes out as the canary, so no batch is needed.
    assert [c["messages"][0]["content"] for c in retry.messages.create_calls] == ["user text 2"]
    assert llm.RawArchive("step").load()["r-2"].ok

    later_failure = llm.Result("r-1", "errored", None, 0, 0, "overloaded_error", "b9")
    llm.RawArchive("step").append([later_failure])
    assert llm.RawArchive("step").load()["r-1"].ok


def test_a_pending_batch_is_resumed_not_resubmitted() -> None:
    requests = reqs(5)
    slow = fakes.FakeAnthropic(batch_ready=False)
    clock = iter([0, 0, 10, 20, 9999, 9999, 9999])
    first = run(slow, requests, wait_seconds=30, clock=lambda: next(clock))
    assert first.pending_batch == "msgbatch_1" and llm.RawArchive("step").state_path.exists()
    assert len(slow.messages.batches.created) == 1

    slow.batch_ready = True  # the batch finishes while we were away
    second = run(slow, requests, wait_seconds=30)
    assert len(slow.messages.batches.created) == 1  # still just the one submission
    assert set(second.results) == {f"r-{i}" for i in range(5)} and all(r.ok for r in second.results.values())
    assert not llm.RawArchive("step").state_path.exists()


def test_a_truncated_answer_is_not_a_success() -> None:
    client = fakes.FakeAnthropic()
    client.messages.create = lambda **kw: fakes._message('{"items": [', 10, 700, stop_reason="max_tokens")
    outcome = run(client, reqs(3))
    assert outcome.results["r-0"].status == "truncated" and not outcome.results["r-0"].ok
    assert client.messages.batches.created == []  # the canary failed, so nothing was batched


@pytest.mark.parametrize(
    "text, expected",
    [('{"items": []}', {"items": []}), ('```json\n{"items": []}\n```', {"items": []}), ("nope", None), (None, None)],
)
def test_json_text_parsing_tolerates_a_code_fence(text, expected) -> None:
    assert llm.parse_json_text(text) == expected
