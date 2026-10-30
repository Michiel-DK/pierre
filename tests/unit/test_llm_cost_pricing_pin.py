"""Pin Gemini per-1M-token prices to the values published by Google.

Guards telemetry honesty: recorded LLM cost must match real cost. If anyone bumps the
``_MODEL_PRICING`` dict in ``pierre/utils/llm_cost.py`` without consulting
Google's published rates, this test fails and forces them to update both
sides together — preventing a repeat of a pricing-table drift where
``gemini-2.5-flash`` was logged at ~4× under-cost for months, until
2026-05-25.

Source of truth (verified 2026-05-25):
  https://ai.google.dev/gemini-api/docs/pricing  (paid tier, standard
  non-batch).

CI-strip safe: imports only ``pierre.utils.llm_cost``, which itself depends
only on stdlib + ``typing``. No database-driver transit.
"""
from __future__ import annotations

import pytest

from pierre.utils.llm_cost import _MODEL_PRICING, compute_llm_cost


# ── Canonical per-model prices (USD per 1M tokens) ────────────────────────────
#
# Each tuple is (input, output, thinking). Mirror of Google's published
# rates as of 2026-05-25. To change a value here, you MUST also:
#   1. Verify the new rate on https://ai.google.dev/gemini-api/docs/pricing
#   2. Update ``pierre/utils/llm_cost.py::_MODEL_PRICING``
#   3. Add a one-line note in this docstring's "Last verified" line above

_EXPECTED_PRICES: dict[str, tuple[float, float, float]] = {
    "gemini-2.5-flash":          (0.30, 2.50, 3.50),
    "models/gemini-2.5-flash":   (0.30, 2.50, 3.50),
    "gemini-2.5-pro":            (1.25, 10.00, 3.50),
    "models/gemini-2.5-pro":     (1.25, 10.00, 3.50),
    "gemini-embedding-001":      (0.15, 0.0, 0.0),
    "models/gemini-embedding-001": (0.15, 0.0, 0.0),
    # Added 2026-05-26: Gemini 3.1
    # Flash Lite. Assumed lite-tier rate (see the note in llm_cost.py).
    "gemini-3.1-flash-lite":         (0.10, 0.40, 1.00),
    "models/gemini-3.1-flash-lite":  (0.10, 0.40, 1.00),
}


@pytest.mark.parametrize("model_name,expected", sorted(_EXPECTED_PRICES.items()))
def test_model_pricing_matches_google_published_rates(
    model_name: str, expected: tuple[float, float, float]
) -> None:
    """Pin _MODEL_PRICING to Google's published rates.

    If this fails, a price drifted. Either:
      (a) Google changed their rates — update ``_EXPECTED_PRICES`` here AND
          ``_MODEL_PRICING`` in ``pierre/utils/llm_cost.py`` in the same PR, OR
      (b) Someone changed ``_MODEL_PRICING`` without updating the pin —
          revert the change or coordinate with the operator.
    """
    assert model_name in _MODEL_PRICING, (
        f"{model_name} missing from _MODEL_PRICING. If a model was removed "
        f"intentionally, drop the entry from _EXPECTED_PRICES in this test."
    )
    actual = _MODEL_PRICING[model_name]
    expected_input, expected_output, expected_thinking = expected
    assert actual["input"] == expected_input, (
        f"{model_name} input price drift: pin says ${expected_input}/M, "
        f"_MODEL_PRICING says ${actual['input']}/M"
    )
    assert actual["output"] == expected_output, (
        f"{model_name} output price drift: pin says ${expected_output}/M, "
        f"_MODEL_PRICING says ${actual['output']}/M"
    )
    assert actual["thinking"] == expected_thinking, (
        f"{model_name} thinking price drift: pin says ${expected_thinking}/M, "
        f"_MODEL_PRICING says ${actual['thinking']}/M"
    )


def test_no_unexpected_models_in_pricing_table() -> None:
    """If a new model is added to _MODEL_PRICING, this test fails until the
    pin table is updated. Forces explicit operator sign-off on the rate."""
    extra = set(_MODEL_PRICING.keys()) - set(_EXPECTED_PRICES.keys())
    assert not extra, (
        f"_MODEL_PRICING has model(s) not pinned in this test: {sorted(extra)}. "
        f"Add each to _EXPECTED_PRICES with the Google-published rate."
    )


def test_flash_cost_is_no_longer_undercounted() -> None:
    """Regression guard for the Flash pricing-table drift.

    Before 2026-05-25, gemini-2.5-flash was priced at $0.15 input / $0.60
    output — undercounting realised cost by ~4×. The fix bumped to $0.30 /
    $2.50. This test asserts a 1M-input + 1M-output call costs the post-fix
    amount ($2.80) and would still fail if anyone reverts.
    """
    cost = compute_llm_cost(
        "gemini-2.5-flash",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    # 0.30 + 2.50 = 2.80
    assert abs(cost - 2.80) < 1e-9, (
        f"Expected $2.80 for 1M+1M tokens on Flash (post-fix "
        f"correct rate), got ${cost}. Did someone revert the pricing fix?"
    )


def test_embedding_model_has_zero_output_cost() -> None:
    """gemini-embedding-001 returns embeddings, not tokens — output_tokens
    should always be 0 in practice. Pin the rate at 0 to make sure nothing
    silently bills for phantom output tokens on this model."""
    cost = compute_llm_cost(
        "gemini-embedding-001",
        input_tokens=1_000_000,
        output_tokens=0,
    )
    assert abs(cost - 0.15) < 1e-9
