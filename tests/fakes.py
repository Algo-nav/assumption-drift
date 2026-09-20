"""A stand-in for the Anthropic client. Shaped like the SDK objects the pipeline reads; makes no network call."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Callable, Iterator


def _message(text: str, input_tokens: int, output_tokens: int, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
        stop_reason=stop_reason,
    )


def empty_items(params: dict) -> str:
    return json.dumps({"items": []})


class FakeBatches:
    def __init__(self, owner: "FakeAnthropic") -> None:
        self.owner = owner
        self.created: list[list[dict]] = []
        self.polls = 0

    def create(self, requests: list[dict]) -> SimpleNamespace:
        self.created.append(requests)
        return SimpleNamespace(id=f"msgbatch_{len(self.created)}", processing_status="in_progress")

    def retrieve(self, batch_id: str) -> SimpleNamespace:
        self.polls += 1
        ended = self.owner.batch_ready and self.polls > self.owner.polls_before_end
        return SimpleNamespace(
            id=batch_id,
            processing_status="ended" if ended else "in_progress",
            request_counts=SimpleNamespace(processing=0 if ended else 1),
        )

    def results(self, batch_id: str) -> Iterator[SimpleNamespace]:
        requests = self.created[int(batch_id.rsplit("_", 1)[1]) - 1]
        for item in requests:
            cid = item["custom_id"]
            if cid in self.owner.fail_ids:
                yield SimpleNamespace(custom_id=cid, result=SimpleNamespace(type="errored", error=SimpleNamespace(type="overloaded_error")))
            else:
                text = self.owner.respond(item["params"])
                yield SimpleNamespace(
                    custom_id=cid,
                    result=SimpleNamespace(type="succeeded", message=_message(text, self.owner.input_tokens, self.owner.output_tokens)),
                )


class FakeMessages:
    def __init__(self, owner: "FakeAnthropic") -> None:
        self.owner = owner
        self.batches = FakeBatches(owner)
        self.count_calls = 0
        self.create_calls: list[dict] = []

    def count_tokens(self, **kwargs) -> SimpleNamespace:
        if not self.owner.authenticated:
            raise TypeError('"Could not resolve authentication method. Expected one of api_key, auth_token, or credentials to be set."')
        self.count_calls += 1
        return SimpleNamespace(input_tokens=self.owner.input_tokens)

    def create(self, **kwargs) -> SimpleNamespace:
        self.create_calls.append(kwargs)
        return _message(self.owner.respond(kwargs), self.owner.input_tokens, self.owner.output_tokens)


class FakeAnthropic:
    def __init__(
        self,
        respond: Callable[[dict], str] = empty_items,
        *,
        authenticated: bool = True,
        input_tokens: int = 1000,
        output_tokens: int = 100,
        fail_ids: tuple[str, ...] = (),
        batch_ready: bool = True,
        polls_before_end: int = 0,
    ) -> None:
        self.respond = respond
        self.authenticated = authenticated
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.fail_ids = set(fail_ids)
        self.batch_ready = batch_ready
        self.polls_before_end = polls_before_end
        self.messages = FakeMessages(self)
