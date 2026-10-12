"""Model-free replay proven through the real OpenAI SDK.

The benchmark's graph calls OpenRouter via the OpenAI SDK. This test wires that
exact SDK to a cassette-backed httpx transport: record one chat completion
against a mock provider, then replay the identical call with passthrough off and
assert the network is never touched and the content is byte-identical. That is
"replay without re-calling the model", end to end through the SDK.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from husk_studio_sandbox.cassette import CassetteStore, RecordingTransport, ReplayingTransport

pytest.importorskip("openai")
from openai import OpenAI  # noqa: E402


def _completion() -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "the answer is 42"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11},
    }


def test_openai_sdk_replays_without_touching_network(tmp_path: Path) -> None:
    store = CassetteStore(tmp_path / "cass")
    calls: list[str] = []

    def provider(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=_completion())

    messages = [{"role": "user", "content": "what is the answer?"}]

    # Record against the mock provider.
    rec = OpenAI(
        api_key="test",
        base_url="https://provider.test/v1",
        http_client=httpx.Client(transport=RecordingTransport(store, inner=httpx.MockTransport(provider))),
    )
    r1 = rec.chat.completions.create(model="test-model", messages=messages)
    assert r1.choices[0].message.content == "the answer is 42"
    assert len(calls) == 1 and len(store) == 1

    # Replay strictly: a served response with the mock never called proves the
    # model was not re-invoked.
    rep = OpenAI(
        api_key="test",
        base_url="https://provider.test/v1",
        http_client=httpx.Client(
            transport=ReplayingTransport(store, inner=httpx.MockTransport(provider), allow_passthrough=False)
        ),
    )
    r2 = rep.chat.completions.create(model="test-model", messages=messages)
    assert r2.choices[0].message.content == "the answer is 42"
    assert r2.usage.total_tokens == 11
    assert len(calls) == 1  # unchanged: replay touched no network
