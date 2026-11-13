"""The 12-claim agent-context spot check: its spec is balanced, and its labels agree with the planted data."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "analysis"))

import agent_context_spot_check as spot  # noqa: E402


def test_spec_is_six_true_and_six_false_over_known_contexts():
    labels = [c[2] for c in spot.CLAIMS]
    assert labels.count("true") == 6 and labels.count("false") == 6
    assert {c[1] for c in spot.CLAIMS} <= set(spot.CONTEXTS)


def test_true_claims_are_supported_by_the_context_the_tools_produce():
    ctx = spot.build_context("quillmoor")["firm_profiles"][0]
    assert ctx["hq"] == "London, UK" and ctx["cheque_range"] == "EUR 500000-3000000" and ctx["portfolio_count"] == 6
    brask, gravick = spot.build_context("brask_gravick")["firm_profiles"]
    assert "Series A" in brask["stages"] and gravick["stages"] == ["Series A"]
    names = {c["name"] for c in spot.build_context("merrowfield_2024")["portfolio_companies"]}
    assert {"Umbrevia Bio", "Bexmirra Materials"} <= names and "Saltmere Energy" not in names
    assert "added brask-ventures" in spot.build_context("shortlist")["actions_taken"][0].lower()
    assert spot.build_context("ferrin_error")["tool_errors"]


def test_false_claims_contradict_the_planted_data():
    fit = spot.build_context("raise_fit")["firm_profiles"][0]["fit_note"]
    assert "too small" in fit
    shortlist = " ".join(spot.build_context("shortlist")["actions_taken"])
    assert "orrindale" not in shortlist.lower()
    gravick = spot.build_context("brask_gravick")["firm_profiles"][1]
    assert gravick["cheque_range"].endswith("8000000") or "8000000" in gravick["cheque_range"]
