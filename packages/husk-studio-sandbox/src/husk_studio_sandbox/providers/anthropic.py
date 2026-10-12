"""Anthropic cost & token utilities.

Prices in USD per 1M tokens (input, output). Update when Anthropic revises pricing.
"""

from __future__ import annotations

# Snapshot of public pricing as of 2026-05; update via a release task.
_PRICE_PER_1M: dict[str, tuple[float, float]] = {
    "claude-opus-4": (15.0, 75.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-4": (0.80, 4.0),
    "claude-3-5-sonnet": (3.0, 15.0),
    "claude-3-5-haiku": (0.80, 4.0),
    "claude-3-opus": (15.0, 75.0),
    "claude-3-sonnet": (3.0, 15.0),
    "claude-3-haiku": (0.25, 1.25),
}


def cost_usd(model: str, tokens_in: int | None, tokens_out: int | None) -> float | None:
    if not model or tokens_in is None or tokens_out is None:
        return None
    key = next((k for k in _PRICE_PER_1M if model.startswith(k)), None)
    if key is None:
        return None
    p_in, p_out = _PRICE_PER_1M[key]
    return round((tokens_in / 1_000_000.0) * p_in + (tokens_out / 1_000_000.0) * p_out, 6)
