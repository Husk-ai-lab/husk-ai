"""Provider abstraction: httpx call path via MockTransport, and key never logged.

Mirrors packages/husk-studio-sandbox/tests/test_cassette_sdk.py: a mock transport proves
the request shape without touching the network.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from husk_studio_backend.debugger.providers import (
    AnthropicProvider,
    OpenAIProvider,
    OpenRouterProvider,
    ProviderError,
    available_providers,
    get_provider,
)

_SECRET = "sk-super-secret-key-123"


def test_anthropic_call_shape_and_no_key_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["x-api-key"] = request.headers.get("x-api-key")
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"content": [{"type": "text", "text": '{"ok":true}'}]})

    prov = AnthropicProvider()
    with caplog.at_level(logging.DEBUG):
        out = prov.complete(
            system="sys",
            user="usr",
            model="claude-sonnet-4",
            api_key=_SECRET,
            max_output_tokens=128,
            transport=httpx.MockTransport(handler),
        )

    assert out == '{"ok":true}'
    assert seen["x-api-key"] == _SECRET
    assert "anthropic.com" in str(seen["url"])
    # The key must never reach the logs.
    assert _SECRET not in caplog.text


def test_openai_call_shape(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == f"Bearer {_SECRET}"
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "hello"}}]}
        )

    out = OpenAIProvider().complete(
        system="s",
        user="u",
        model="gpt-4o-mini",
        api_key=_SECRET,
        max_output_tokens=64,
        transport=httpx.MockTransport(handler),
    )
    assert out == "hello"
    assert _SECRET not in caplog.text


def test_openrouter_call_shape(caplog: pytest.LogCaptureFixture) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    out = OpenRouterProvider().complete(
        system="s",
        user="u",
        model="meta-llama/llama-3.3-70b-instruct",
        api_key=_SECRET,
        max_output_tokens=64,
        transport=httpx.MockTransport(handler),
    )
    assert out == "ok"
    assert seen["authorization"] == f"Bearer {_SECRET}"
    assert "openrouter.ai" in str(seen["url"])
    # OpenRouter expects the classic `max_tokens` field, not `max_completion_tokens`.
    assert '"max_tokens": 64' in str(seen["body"]) or '"max_tokens":64' in str(seen["body"])
    assert _SECRET not in caplog.text


def test_openrouter_is_registered() -> None:
    assert "openrouter" in available_providers()
    assert get_provider("openrouter").name == "openrouter"


def test_provider_error_on_non_2xx() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="invalid api key")

    with pytest.raises(ProviderError):
        AnthropicProvider().complete(
            system="s",
            user="u",
            model="claude-sonnet-4",
            api_key="bad",
            max_output_tokens=10,
            transport=httpx.MockTransport(handler),
        )


def test_unknown_provider_raises() -> None:
    with pytest.raises(ProviderError):
        get_provider("does-not-exist")


def test_list_models_exposes_context_window() -> None:
    models = AnthropicProvider().list_models()
    assert any(m["id"] == "claude-sonnet-4" for m in models)
    assert all("context_window" in m and "max_output" in m for m in models)
