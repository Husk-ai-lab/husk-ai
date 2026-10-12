"""Provider abstraction for the debugger's LLM calls.

Pure ``httpx`` (already a dependency) so we control exactly what is sent and
never log headers or bodies — no SDK that might log the key. Adding a provider is
a small class + one registry entry. Each provider exposes the chosen model's
context window via :mod:`husk_studio_shared.model_metadata`, which the context assembler
uses to size the budget.

Tests inject ``transport=httpx.MockTransport(...)`` to exercise the call path
without network, mirroring ``packages/husk-studio-sandbox/tests/test_cassette_sdk.py``.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

import httpx

from husk_studio_shared import model_metadata

log = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(120.0)


class ProviderError(RuntimeError):
    """A provider call failed (network, auth, or a non-2xx response)."""


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    def complete(
        self,
        *,
        system: str,
        user: str,
        model: str,
        api_key: str,
        max_output_tokens: int,
        transport: httpx.BaseTransport | None = None,
    ) -> str: ...

    def list_models(self) -> list[dict[str, Any]]: ...


def _models_payload(model_ids: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "id": m,
            "context_window": model_metadata.context_window(m),
            "max_output": model_metadata.max_output(m),
        }
        for m in model_ids
    ]


class AnthropicProvider:
    name = "anthropic"
    _URL = "https://api.anthropic.com/v1/messages"
    _KNOWN = [
        "claude-opus-4",
        "claude-sonnet-4",
        "claude-haiku-4-5",
        "claude-3-5-sonnet",
        "claude-3-5-haiku",
    ]

    def complete(
        self,
        *,
        system: str,
        user: str,
        model: str,
        api_key: str,
        max_output_tokens: int,
        transport: httpx.BaseTransport | None = None,
    ) -> str:
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": model,
            "max_tokens": max_output_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        data = _post(self._URL, headers, body, transport)
        try:
            blocks = data["content"]
            return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        except (KeyError, TypeError) as e:
            raise ProviderError(f"unexpected Anthropic response shape: {e}") from e

    def list_models(self) -> list[dict[str, Any]]:
        return _models_payload(self._KNOWN)


class OpenAIProvider:
    name = "openai"
    _URL = "https://api.openai.com/v1/chat/completions"
    _KNOWN = ["gpt-4.1", "gpt-4.1-mini", "gpt-4o", "gpt-4o-mini", "o3", "o3-mini"]

    def complete(
        self,
        *,
        system: str,
        user: str,
        model: str,
        api_key: str,
        max_output_tokens: int,
        transport: httpx.BaseTransport | None = None,
    ) -> str:
        headers = {
            "authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        }
        body = {
            "model": model,
            "max_completion_tokens": max_output_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        data = _post(self._URL, headers, body, transport)
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise ProviderError(f"unexpected OpenAI response shape: {e}") from e

    def list_models(self) -> list[dict[str, Any]]:
        return _models_payload(self._KNOWN)


class OpenRouterProvider:
    """OpenRouter: OpenAI-compatible API (same ``/chat/completions`` shape, Bearer
    auth) fronting many models. Husk's own benchmark runs on OpenRouter Llama, so
    supporting it here lets you debug a run with the same provider it ran on.

    Models are namespaced (``meta-llama/llama-3.3-70b-instruct``). Uses ``max_tokens``
    (OpenAI's classic field, which OpenRouter expects) rather than the newer
    ``max_completion_tokens``.
    """

    name = "openrouter"
    _URL = "https://openrouter.ai/api/v1/chat/completions"
    _KNOWN = [
        "meta-llama/llama-3.3-70b-instruct",
        "meta-llama/llama-3.1-70b-instruct",
        "meta-llama/llama-3.1-8b-instruct",
        "openai/gpt-4o",
        "openai/gpt-4o-mini",
        "anthropic/claude-3.5-sonnet",
    ]

    def complete(
        self,
        *,
        system: str,
        user: str,
        model: str,
        api_key: str,
        max_output_tokens: int,
        transport: httpx.BaseTransport | None = None,
    ) -> str:
        headers = {
            "authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        }
        body = {
            "model": model,
            "max_tokens": max_output_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        data = _post(self._URL, headers, body, transport)
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise ProviderError(f"unexpected OpenRouter response shape: {e}") from e

    def list_models(self) -> list[dict[str, Any]]:
        return _models_payload(self._KNOWN)


class RegoloProvider:
    """Regolo.ai — EU (Italy), GDPR, zero data-retention inference over an
    OpenAI-compatible ``/chat/completions`` API (Bearer auth, classic
    ``max_tokens``). Husk's default provider for the AI layer: the data sent for
    analysis stays in the EU. Adding it is the same one-class shape as the others.
    """

    name = "regolo"
    _URL = "https://api.regolo.ai/v1/chat/completions"
    # ponytail: seed with the open models Regolo serves today; verify/expand from
    # Regolo's live GET /v1/models rather than guessing more than we know.
    _KNOWN = [
        "Llama-3.3-70B-Instruct",
        "Llama-3.1-8B-Instruct",
        "Qwen2.5-Coder-32B-Instruct",
        "mistral-7b-instruct-v0.3",
    ]

    def complete(
        self,
        *,
        system: str,
        user: str,
        model: str,
        api_key: str,
        max_output_tokens: int,
        transport: httpx.BaseTransport | None = None,
    ) -> str:
        headers = {
            "authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        }
        body = {
            "model": model,
            "max_tokens": max_output_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        data = _post(self._URL, headers, body, transport)
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise ProviderError(f"unexpected Regolo response shape: {e}") from e

    def list_models(self) -> list[dict[str, Any]]:
        return _models_payload(self._KNOWN)


def _post(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    transport: httpx.BaseTransport | None,
) -> dict[str, Any]:
    """POST JSON and return the parsed body. Never logs headers or body."""
    try:
        with httpx.Client(transport=transport, timeout=_TIMEOUT) as client:
            resp = client.post(url, headers=headers, json=body)
    except httpx.HTTPError as e:
        # Log only the exception type — never the URL with creds or the body.
        raise ProviderError(f"request failed: {type(e).__name__}") from e
    if resp.status_code >= 400:
        # Provider error bodies do not contain the API key; include a short,
        # truncated snippet to aid debugging (status + provider message).
        raise ProviderError(f"provider returned {resp.status_code}: {resp.text[:300]}")
    data: dict[str, Any] = resp.json()
    return data


_PROVIDERS: dict[str, LLMProvider] = {
    RegoloProvider.name: RegoloProvider(),
    AnthropicProvider.name: AnthropicProvider(),
    OpenAIProvider.name: OpenAIProvider(),
    OpenRouterProvider.name: OpenRouterProvider(),
}


def get_provider(name: str) -> LLMProvider:
    try:
        return _PROVIDERS[name]
    except KeyError:
        raise ProviderError(
            f"unknown provider {name!r}; available: {sorted(_PROVIDERS)}"
        ) from None


def available_providers() -> list[str]:
    return sorted(_PROVIDERS)
