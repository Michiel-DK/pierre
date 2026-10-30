"""LLM cost calculation helpers for Gemini models.

Compute cost from model name + token counts. In the source system the API
instrumentation layer uses this to record per-call and per-run LLM cost.
"""
from __future__ import annotations

from typing import Any, Optional


# ── Per-model pricing (USD per 1M tokens) ─────────────────────────────────────
#
# Source of truth: https://ai.google.dev/gemini-api/docs/pricing (paid tier,
# standard non-batch). Kept in sync manually; the pin test in
# `tests/unit/test_llm_cost_pricing_pin.py` will fail loudly on drift.
#
# Last verified: 2026-05-25.

_MODEL_PRICING: dict[str, dict[str, float]] = {
    # Gemini 2.5 Flash — $0.30 input / $2.50 output / $3.50 thinking per 1M.
    # Bumped 2026-05-25 from the stale $0.15 / $0.60 (which undercounted
    # realised cost by ~4×).
    "gemini-2.5-flash": {
        "input": 0.30,
        "output": 2.50,
        "thinking": 3.50,
    },
    "models/gemini-2.5-flash": {
        "input": 0.30,
        "output": 2.50,
        "thinking": 3.50,
    },
    # Gemini 2.5 Pro — $1.25 input / $10.00 output / $3.50 thinking per 1M
    # (≤200K context tier; long-context tier is more expensive but we don't
    # route there today).
    "gemini-2.5-pro": {
        "input": 1.25,
        "output": 10.00,
        "thinking": 3.50,
    },
    "models/gemini-2.5-pro": {
        "input": 1.25,
        "output": 10.00,
        "thinking": 3.50,
    },
    # Gemini Embedding — $0.15 per 1M input tokens (no output / thinking).
    "gemini-embedding-001": {
        "input": 0.15,
        "output": 0.0,
        "thinking": 0.0,
    },
    "models/gemini-embedding-001": {
        "input": 0.15,
        "output": 0.0,
        "thinking": 0.0,
    },
    # ── Gemini 3.1 Flash Lite (added 2026-05-26) ─────────────────────────────
    # Gemini 3.1 Flash Lite — pitch-classifier replacement for 2.5-flash.
    #
    # Rate source: https://ai.google.dev/gemini-api/docs/pricing — paid tier,
    # standard non-batch, ≤128K context window. ASSUMED rate: no published price
    # was confirmed when this was added, so it follows the 2.5/2.0 lite variants
    # (input ~$0.10, output ~$0.40).
    # If Google publishes an updated rate, bump both entries below AND the
    # pin test in ``tests/unit/test_llm_cost_pricing_pin.py`` in the same
    # change, so recorded cost never silently diverges from real cost.
    "gemini-3.1-flash-lite": {
        "input": 0.10,
        "output": 0.40,
        "thinking": 1.00,
    },
    "models/gemini-3.1-flash-lite": {
        "input": 0.10,
        "output": 0.40,
        "thinking": 1.00,
    },
}

# Fallback pricing when the model name is not recognised. Defaults to the
# 2.5-flash rate so unknown-model logging at least won't undercount.
_DEFAULT_PRICING: dict[str, float] = {
    "input": 0.30,
    "output": 2.50,
    "thinking": 3.50,
}


def compute_llm_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    thinking_tokens: int = 0,
) -> float:
    """Compute cost in USD for a single LLM call.

    Args:
        model: Model name (e.g. "gemini-2.5-flash", "models/gemini-2.5-pro").
        input_tokens: Number of prompt/input tokens.
        output_tokens: Number of completion/output tokens.
        thinking_tokens: Number of thinking/reasoning tokens (0 if not used).

    Returns:
        Cost in USD (float).
    """
    pricing = _MODEL_PRICING.get(model, _DEFAULT_PRICING)
    cost = (
        (input_tokens / 1_000_000) * pricing["input"]
        + (output_tokens / 1_000_000) * pricing["output"]
        + (thinking_tokens / 1_000_000) * pricing["thinking"]
    )
    return cost


def extract_usage_from_response(response: Any) -> dict[str, int]:
    """Extract token counts from a Gemini SDK response object.

    Returns a dict with keys: input_tokens, output_tokens, thinking_tokens.
    All values default to 0 if the response lacks usage metadata.
    """
    result = {"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0}
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return result
    result["input_tokens"] = getattr(usage, "prompt_token_count", 0) or 0
    result["output_tokens"] = getattr(usage, "candidates_token_count", 0) or 0
    result["thinking_tokens"] = getattr(usage, "thoughts_token_count", 0) or 0
    return result
