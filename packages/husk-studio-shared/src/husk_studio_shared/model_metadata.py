"""Structural model capabilities: context window + max output tokens.

Distinct from :mod:`husk_studio_shared.pricing` (which is per-1K cost): this is what the
debugger's context assembler needs to know how much trace it can fit into a given
model's context window. Keyed identically to ``pricing`` with the same exact-then-
prefix fallback (so ``claude-opus-4-8`` resolves to the ``claude-opus-4`` family
and ``gpt-4o-2024-08-06`` to ``gpt-4o``).

Best-effort and conservative: an unknown model falls back to a safe small window
so the assembler never sends an over-budget request. Expand as new models ship.
"""

from __future__ import annotations

from typing import TypedDict


class ModelMeta(TypedDict):
    context_window: int  # total tokens the model accepts (input + output)
    max_output: int  # a sane default cap for the model's own answer


# Conservative defaults used when a model id is unknown. 128K window is the
# common floor for current frontier models; 4K output is plenty for a JSON report.
DEFAULT_CONTEXT_WINDOW = 128_000
DEFAULT_MAX_OUTPUT = 4_096


_MODEL_METADATA: dict[str, ModelMeta] = {
    # OpenAI
    "gpt-4o": {"context_window": 128_000, "max_output": 16_384},
    "gpt-4o-mini": {"context_window": 128_000, "max_output": 16_384},
    "gpt-4.1": {"context_window": 1_000_000, "max_output": 32_768},
    "gpt-4.1-mini": {"context_window": 1_000_000, "max_output": 32_768},
    "gpt-4-turbo": {"context_window": 128_000, "max_output": 4_096},
    "gpt-3.5-turbo": {"context_window": 16_385, "max_output": 4_096},
    "o1": {"context_window": 200_000, "max_output": 100_000},
    "o1-mini": {"context_window": 128_000, "max_output": 65_536},
    "o3": {"context_window": 200_000, "max_output": 100_000},
    "o3-mini": {"context_window": 200_000, "max_output": 100_000},
    # Anthropic
    "claude-3-5-sonnet": {"context_window": 200_000, "max_output": 8_192},
    "claude-3-5-haiku": {"context_window": 200_000, "max_output": 8_192},
    "claude-3-opus": {"context_window": 200_000, "max_output": 4_096},
    "claude-3-haiku": {"context_window": 200_000, "max_output": 4_096},
    "claude-opus-4": {"context_window": 200_000, "max_output": 32_000},
    "claude-sonnet-4": {"context_window": 200_000, "max_output": 64_000},
    "claude-haiku-4-5": {"context_window": 200_000, "max_output": 32_000},
    "claude-opus-4-7": {"context_window": 200_000, "max_output": 32_000},
    # OpenRouter (namespaced ids; OpenAI-compatible API). The benchmark runs on
    # the meta-llama models, so the debugger can target the same provider.
    "meta-llama/llama-3.3-70b-instruct": {"context_window": 131_072, "max_output": 8_192},
    "meta-llama/llama-3.1-70b-instruct": {"context_window": 131_072, "max_output": 8_192},
    "meta-llama/llama-3.1-8b-instruct": {"context_window": 131_072, "max_output": 8_192},
    "openai/gpt-4o": {"context_window": 128_000, "max_output": 16_384},
    "openai/gpt-4o-mini": {"context_window": 128_000, "max_output": 16_384},
    "anthropic/claude-3.5-sonnet": {"context_window": 200_000, "max_output": 8_192},
    # Regolo.ai (EU, OpenAI-compatible) — Husk's default AI-layer provider. Open
    # models; only the structural window/output is asserted here (public),
    # pricing is deliberately left unknown rather than guessed.
    "Llama-3.3-70B-Instruct": {"context_window": 128_000, "max_output": 8_192},
    "Llama-3.1-8B-Instruct": {"context_window": 128_000, "max_output": 8_192},
    "Qwen2.5-Coder-32B-Instruct": {"context_window": 32_768, "max_output": 8_192},
    "mistral-7b-instruct-v0.3": {"context_window": 32_768, "max_output": 8_192},
}


def _lookup(model: str | None) -> ModelMeta | None:
    if not model:
        return None
    meta = _MODEL_METADATA.get(model)
    if meta is not None:
        return meta
    for prefix, m in _MODEL_METADATA.items():
        if model.startswith(prefix):
            return m
    return None


def context_window(model: str | None) -> int:
    """Total context window for ``model`` in tokens (conservative fallback)."""
    meta = _lookup(model)
    return meta["context_window"] if meta else DEFAULT_CONTEXT_WINDOW


def max_output(model: str | None) -> int:
    """Sane default max output tokens for ``model`` (conservative fallback)."""
    meta = _lookup(model)
    return meta["max_output"] if meta else DEFAULT_MAX_OUTPUT


def is_known(model: str | None) -> bool:
    """True when we have explicit metadata for ``model`` (exact or prefix)."""
    return _lookup(model) is not None
