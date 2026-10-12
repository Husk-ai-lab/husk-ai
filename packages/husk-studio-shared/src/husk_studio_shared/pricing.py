"""Static LLM pricing for cost computation. USD per 1K tokens (input, output).

Best-effort, expand as needed. Prefix matching falls back when an exact model
string isn't found (e.g. "gpt-4o-2024-08-06" → "gpt-4o").
"""

from __future__ import annotations

# (price_per_1k_input, price_per_1k_output) in USD.
_PRICING_PER_1K: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-4o": (0.0025, 0.01),
    "gpt-4o-mini": (0.00015, 0.0006),
    "gpt-4.1": (0.002, 0.008),
    "gpt-4.1-mini": (0.0004, 0.0016),
    "gpt-4-turbo": (0.01, 0.03),
    "gpt-3.5-turbo": (0.0005, 0.0015),
    "o1": (0.015, 0.06),
    "o1-mini": (0.003, 0.012),
    "o3": (0.002, 0.008),
    "o3-mini": (0.0011, 0.0044),
    # Anthropic
    "claude-3-5-sonnet": (0.003, 0.015),
    "claude-3-5-haiku": (0.0008, 0.004),
    "claude-3-opus": (0.015, 0.075),
    "claude-3-haiku": (0.00025, 0.00125),
    "claude-opus-4": (0.015, 0.075),
    "claude-sonnet-4": (0.003, 0.015),
    "claude-haiku-4-5": (0.001, 0.005),
    "claude-opus-4-7": (0.015, 0.075),
    # Groq (USD per 1K tokens, list price as of late 2025; free-tier inference
    # available under rate limits — these prices reflect what Groq charges
    # paid customers and are the right rate to attribute to bypassed token
    # work, matching what an organisation would have actually paid).
    "llama-3.1-8b-instant": (0.00005, 0.00008),
    "llama-3.3-70b-versatile": (0.00059, 0.00079),
    "llama-3.1-70b-versatile": (0.00059, 0.00079),
    "llama3-70b-8192": (0.00059, 0.00079),
    "llama3-8b-8192": (0.00005, 0.00008),
    "mixtral-8x7b-32768": (0.00024, 0.00024),
    "gemma2-9b-it": (0.0002, 0.0002),
    "openai/gpt-oss-20b": (0.0001, 0.0001),
    "openai/gpt-oss-120b": (0.00015, 0.00015),
    # OpenRouter (the IDs used by the published benchmark run). Same Llama
    # models as the Groq entries above and treated as directly comparable, so
    # we attribute the same per-1K list rates. With these present the in-DB
    # `total_cost_usd` reflects the list-price cost of the *recorded* tokens
    # (~$0.59 for the canonical run); the higher provider-billed figure quoted
    # in the paper (~$2.95 / €2.73) is the actual OpenRouter charge across the
    # whole measurement campaign (routing premium + retries + earlier runs).
    "meta-llama/llama-3.1-8b-instruct": (0.00005, 0.00008),
    "meta-llama/llama-3.3-70b-instruct": (0.00059, 0.00079),
    "meta-llama/llama-3.1-70b-instruct": (0.00059, 0.00079),
}


def cost_usd(
    model: str | None,
    tokens_in: int | None,
    tokens_out: int | None,
) -> float | None:
    """Best-effort cost in USD. Returns None for unknown models."""
    if not model or (not tokens_in and not tokens_out):
        return None
    prices = _PRICING_PER_1K.get(model)
    if prices is None:
        for k, v in _PRICING_PER_1K.items():
            if model.startswith(k):
                prices = v
                break
    if prices is None:
        return None
    p_in, p_out = prices
    cost = ((tokens_in or 0) * p_in + (tokens_out or 0) * p_out) / 1000.0
    return round(cost, 6)
