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


# --- a changed prompt invalidates old answers ------------------------------


def reworded(requests, suffix=" (new wording)"):
    return [llm.LlmRequest(r.custom_id, r.system, r.user + suffix, r.max_tokens, r.schema) for r in requests]


def test_current_results_only_include_answers_to_the_same_request() -> None:
    requests = reqs(3)
    run(fakes.FakeAnthropic(), requests)
    archived = llm.RawArchive("step").load()
    assert set(llm.current_results(archived, requests, CFG.model)) == {"r-0", "r-1", "r-2"}
    assert llm.current_results(archived, reworded(requests), CFG.model) == {}  # same ids, different wording
    changed_schema = [llm.LlmRequest(r.custom_id, r.system, r.user, r.max_tokens, {**SCHEMA, "title": "v2"}) for r in requests]
    assert llm.current_results(archived, changed_schema, CFG.model) == {}


def test_editing_a_prompt_sends_the_requests_again_instead_of_reusing_the_old_answers() -> None:
    requests = reqs(4)
    run(fakes.FakeAnthropic(), requests)
    again = fakes.FakeAnthropic()
    outcome = run(again, reworded(requests))
    assert len(again.messages.create_calls) == 1 and len(again.messages.batches.created[0]) == 3  # canary + the other three
    assert all(r.fingerprint == req.fingerprint(CFG.model) for req, r in zip(reworded(requests), (outcome.results[q.custom_id] for q in requests)))


def test_results_archived_before_fingerprints_existed_are_stale(tmp_path) -> None:
    archive = llm.RawArchive("step")
    archive.append([llm.Result("r-0", "succeeded", '{"items": []}', 10, 5, None, "old_batch")])  # what the pilot wrote
    assert llm.RawArchive("step").load()["r-0"].fingerprint is None
    client = fakes.FakeAnthropic()
    outcome = run(client, reqs(1))
    assert len(client.messages.create_calls) == 1  # r-0 was sent again, as the canary
    assert outcome.results["r-0"].fingerprint == reqs(1)[0].fingerprint(CFG.model)


def test_a_batch_that_was_pending_while_the_prompt_changed_is_stored_under_the_wording_it_was_sent_with() -> None:
    original = reqs(5)
    slow = fakes.FakeAnthropic(batch_ready=False)
    clock = iter([0, 0, 10, 9999, 9999])
    run(slow, original, wait_seconds=30, clock=lambda: next(clock))
    slow.batch_ready = True
    outcome = run(slow, reworded(original))  # someone edited the prompt in the meantime
    archived = llm.RawArchive("step").load()
    assert archived["r-3"].fingerprint == original[3].fingerprint(CFG.model)  # the old wording
    assert "r-3" not in outcome.results  # so it does not count as an answer to the new wording


# --- temperature ------------------------------------------------------------


def test_every_request_is_built_at_temperature_zero() -> None:
    params = reqs(1)[0].params("claude-haiku-4-5")
    assert params["temperature"] == 0 and llm.TEMPERATURE == 0


def test_the_canary_and_the_batch_are_both_sent_at_temperature_zero() -> None:
    client = fakes.FakeAnthropic()
    run(client, reqs(4))
    (canary,) = client.messages.create_calls
    assert "temperature" not in canary and canary["extra_body"] == {"temperature": 0}  # a direct call: create() has no such keyword in anthropic 1.x
    assert [item["params"]["temperature"] for item in client.messages.batches.created[0]] == [0, 0, 0]  # a batch request keeps it in params


def test_create_kwargs_are_params_with_the_temperature_moved_to_extra_body() -> None:
    request = reqs(1)[0]
    params, kwargs = request.params("claude-haiku-4-5"), request.create_kwargs("claude-haiku-4-5")
    assert params["temperature"] == 0 and "temperature" not in kwargs
    assert {k: v for k, v in kwargs.items() if k != "extra_body"} == {k: v for k, v in params.items() if k != "temperature"}
    assert kwargs["extra_body"] == {"temperature": 0} and "temperature" in params  # params itself is not changed


# --- against the real SDK, offline: the fake client takes any keyword, which is how a bad one got through once ------------


def test_every_keyword_of_the_canary_call_is_one_the_installed_sdk_accepts() -> None:
    """The canary failed once with `TypeError: Messages.create() got an unexpected keyword argument 'temperature'`, and every test
    passed, because the fake client's create() takes anything. This asks the SDK."""
    import inspect

    import anthropic

    accepted = set(inspect.signature(anthropic.resources.messages.Messages.create).parameters)
    assert set(reqs(1)[0].create_kwargs("claude-haiku-4-5")) <= accepted


def _real_client(seen: list[dict]):
    """The real SDK, and a transport that records the request body and answers like the API."""
    import anthropic
    import httpx2

    message = {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-4-5", "stop_reason": "end_turn", "stop_sequence": None,
               "content": [{"type": "text", "text": "{\"items\":[]}"}], "usage": {"input_tokens": 1, "output_tokens": 1}}

    def handler(request):
        seen.append({"path": request.url.path, "body": json.loads(request.content)})
        return httpx2.Response(200, json=message)

    return anthropic.Anthropic(api_key="test-key", max_retries=0, http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))


def test_the_real_sdk_sends_the_canary_with_temperature_zero_in_the_request_body() -> None:
    seen: list[dict] = []
    request = reqs(1)[0]
    _real_client(seen).messages.create(**request.create_kwargs("claude-haiku-4-5"))
    (sent,) = seen
    assert sent["path"] == "/v1/messages" and sent["body"]["temperature"] == 0
    assert {k: v for k, v in sent["body"].items() if k != "temperature"} == {k: v for k, v in request.params("claude-haiku-4-5").items() if k != "temperature"}


def test_the_real_sdk_forwards_temperature_zero_in_each_batch_request() -> None:
    seen: list[dict] = []
    requests = reqs(3)
    _real_client(seen).messages.batches.create(requests=[{"custom_id": r.custom_id, "params": r.params("claude-haiku-4-5")} for r in requests])
    (sent,) = seen
    assert sent["path"] == "/v1/messages/batches"
    assert [item["params"]["temperature"] for item in sent["body"]["requests"]] == [0, 0, 0]
    assert [item["params"] for item in sent["body"]["requests"]] == [r.params("claude-haiku-4-5") for r in requests]  # forwarded as they are


def test_counting_tokens_does_not_send_a_temperature() -> None:
    """count_tokens takes the model, system, messages and output format, and nothing to sample with."""
    seen: list[dict] = []

    class Counting(fakes.FakeAnthropic):
        def __init__(self):
            super().__init__()
            original = self.messages.count_tokens
            self.messages.count_tokens = lambda **kw: (seen.append(kw), original(**kw))[1]

    llm.count_input_tokens(Counting(), CFG, reqs(1))
    assert seen and all("temperature" not in kw for kw in seen)


def test_the_temperature_is_part_of_what_makes_an_answer_current(monkeypatch) -> None:
    """Answers given before the temperature was fixed are not reused as answers to the new requests."""
    request = reqs(1)[0]
    before = request.fingerprint("claude-haiku-4-5")
    monkeypatch.setattr(llm, "TEMPERATURE", 1)
    assert request.fingerprint("claude-haiku-4-5") != before


# --- a small stage is sent synchronously, not as a batch ---------------------------------------------------


SYNC = llm.LlmConfig(model="claude-haiku-4-5", budget_usd=10.0, price_input=1.0, price_output=5.0, batch_discount=0.5, sync_below=10)


def run_sync(client, requests, cfg=SYNC, **kw):
    kw.setdefault("projection", projection_for(requests))
    return llm.run_batch(client, cfg, "step", requests, canary_check=ok, sleep=lambda s: None, log=lambda m: None, **kw)


def test_fewer_requests_than_the_threshold_are_sent_one_by_one_with_no_batch() -> None:
    client = fakes.FakeAnthropic()
    requests = reqs(9)
    outcome = run_sync(client, requests)
    assert len(client.messages.create_calls) == 9 and client.messages.batches.created == []
    assert [c["messages"] for c in client.messages.create_calls] == [r.params(SYNC.model)["messages"] for r in requests]  # the same prompts
    assert set(outcome.results) == {r.custom_id for r in requests} and all(r.ok for r in outcome.results.values())
    assert set(llm.RawArchive("step").load()) == set(outcome.results)  # archived like any other answer
    assert not llm.RawArchive("step").state_path.exists()


def test_the_threshold_itself_and_above_goes_as_a_batch() -> None:
    client = fakes.FakeAnthropic()
    run_sync(client, reqs(10))
    assert len(client.messages.create_calls) == 1 and len(client.messages.batches.created[0]) == 9


def test_a_threshold_of_zero_never_goes_synchronous() -> None:
    client = fakes.FakeAnthropic()
    run_sync(client, reqs(3), cfg=CFG)
    assert len(client.messages.create_calls) == 1 and len(client.messages.batches.created[0]) == 2


def test_synchronous_requests_are_costed_at_full_price_in_the_same_ledger() -> None:
    client = fakes.FakeAnthropic(input_tokens=1000, output_tokens=100)
    run_sync(client, reqs(3))
    assert llm.Ledger().committed_usd() == pytest.approx(3 * (1000 * 1.0 + 100 * 5.0) / 1e6)  # no batch discount


def test_the_gate_prices_a_synchronous_stage_without_the_discount() -> None:
    client = fakes.FakeAnthropic()
    just_under_at_batch_price = llm.Projection(3, 1, True, 1, 1, 1.0, 6.00)  # 12.00 at full price: over the 10.00 budget
    with pytest.raises(llm.BudgetExceeded):
        run_sync(client, reqs(3), projection=just_under_at_batch_price)
    assert client.messages.create_calls == []


def test_a_rerun_of_a_synchronous_stage_sends_only_what_has_no_answer() -> None:
    requests = reqs(3)
    run_sync(fakes.FakeAnthropic(fail_ids=()), requests)
    again = fakes.FakeAnthropic()
    run_sync(again, requests)
    assert again.messages.create_calls == []


def test_one_bad_synchronous_answer_is_archived_as_invalid_and_does_not_stop_the_rest() -> None:
    answers = iter(["not json", json.dumps({"items": []}), json.dumps({"items": []})])
    client = fakes.FakeAnthropic(respond=lambda p: next(answers))
    outcome = run_sync(client, reqs(3))
    assert len(client.messages.create_calls) == 3
    assert outcome.results["r-0"].status == "invalid" and outcome.results["r-1"].ok and outcome.results["r-2"].ok
    assert any("r-0" in n for n in outcome.notes)


def test_the_real_config_sets_the_threshold_at_ten() -> None:
    from pipeline import common
    assert llm.LlmConfig.from_config(common.load_config()["llm"]).sync_below == 10
