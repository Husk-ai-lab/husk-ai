"""OpenAI cost & token utilities.

Prices in USD per 1K tokens (input, output). Update when OpenAI revises pricing.
"""

from __future__ import annotations

# Snapshot of public pricing as of 2026-05; update via a release task.
_PRICE_PER_1K: dict[str, tuple[float, float]] = {
    "gpt-4o": (0.0025, 0.01),
    "gpt-4o-mini": (0.00015, 0.0006),
    "gpt-4-turbo": (0.01, 0.03),
    "gpt-4": (0.03, 0.06),
    "gpt-3.5-turbo": (0.0005, 0.0015),
    "o1-preview": (0.015, 0.06),
    "o1-mini": (0.003, 0.012),
}


def cost_usd(model: str, tokens_in: int | None, tokens_out: int | None) -> float | None:
    if not model or tokens_in is None or tokens_out is None:
        return None
    key = next((k for k in _PRICE_PER_1K if model.startswith(k)), None)
    if key is None:
        return None
    p_in, p_out = _PRICE_PER_1K[key]
    return round((tokens_in / 1000.0) * p_in + (tokens_out / 1000.0) * p_out, 6)
