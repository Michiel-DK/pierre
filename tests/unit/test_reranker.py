"""Unit tests for pierre/matching/reranker.py.

Two groups:

1. ``TestReorderByGrade`` / ``TestBundleText`` / ``TestBuildBatchPrompt`` —
   PURE logic only. No google-genai, no sqlalchemy, no network. This group
   is the CI-strip-safe deliverable: the module imports cleanly with
   google-genai and database drivers absent.

2. ``TestGradeCandidatesFallback`` — fault injection on the impure
   orchestrator. Uses a fake ``client_factory`` / fake ``client`` (never a
   real Gemini call) to prove the documented "grader failure -> fall back
   LOUD to incoming order, never a silent empty result" contract, both for total failure (can't
   construct a client) and partial failure (one batch raises).
"""
from __future__ import annotations

import logging

import pytest

from pierre.matching.reranker import (
    RerankGrade,
    UNGRADED_SENTINEL,
    bundle_text,
    build_batch_prompt,
    grade_candidates,
    reorder_by_grade,
)


# ---------------------------------------------------------------------------
# Pure: reorder_by_grade
# ---------------------------------------------------------------------------


class TestReorderByGrade:
    def test_sorts_descending_by_grade(self):
        ids = ["a", "b", "c"]
        grades = {"a": 1, "b": 3, "c": 0}
        assert reorder_by_grade(ids, grades) == ["b", "a", "c"]

    def test_empty_grades_dict_preserves_input_order_exactly(self):
        # This IS the documented "grader failed completely" fallback path —
        # no special-casing in the impl, it falls out of the tie-break.
        ids = ["x", "y", "z", "w"]
        assert reorder_by_grade(ids, {}) == ids

    def test_ties_on_grade_preserve_original_relative_order(self):
        ids = ["a", "b", "c", "d"]
        grades = {"a": 1, "b": 1, "c": 2, "d": 1}
        # c (grade 2) floats to front; a/b/d (all grade 1) keep their
        # original relative order among themselves.
        assert reorder_by_grade(ids, grades) == ["c", "a", "b", "d"]

    def test_ungraded_candidates_sort_below_grade_zero(self):
        ids = ["graded_zero", "ungraded"]
        grades = {"graded_zero": 0}
        assert reorder_by_grade(ids, grades) == ["graded_zero", "ungraded"]

    def test_ungraded_candidates_keep_relative_order_among_themselves(self):
        # Partial batch failure: b and d never got graded. They must not
        # be silently dropped, and must not swap relative order.
        ids = ["a", "b", "c", "d"]
        grades = {"a": 2, "c": 0}
        assert reorder_by_grade(ids, grades) == ["a", "c", "b", "d"]

    def test_never_drops_or_duplicates_a_candidate(self):
        ids = ["a", "b", "c", "d", "e"]
        grades = {"a": 3, "c": 1}
        out = reorder_by_grade(ids, grades)
        assert sorted(out) == sorted(ids)
        assert len(out) == len(ids)

    def test_ungraded_sentinel_is_below_the_valid_grade_floor(self):
        assert UNGRADED_SENTINEL < 0

    def test_empty_input_returns_empty(self):
        assert reorder_by_grade([], {}) == []


# ---------------------------------------------------------------------------
# Pure: bundle_text / build_batch_prompt
# ---------------------------------------------------------------------------


class TestBundleText:
    def test_renders_firm_slug_and_name(self):
        text = bundle_text({"firm_id": "acme_vc", "name": "Acme VC"})
        assert "firm_id=" not in text  # rendered as "slug=" per prototype format
        assert "slug=acme_vc" in text
        assert "name=Acme VC" in text

    def test_no_portfolio_companies_shows_placeholder(self):
        text = bundle_text({"firm_id": "x", "portfolio": []})
        assert "no portfolio companies on file" in text

    def test_portfolio_companies_are_rendered_and_capped_at_15(self):
        cos = [{"name": f"Co{i}", "sector": "health", "description": "d"} for i in range(20)]
        text = bundle_text({"firm_id": "x", "portfolio": cos})
        assert "Co0" in text
        assert "Co14" in text
        assert "Co19" not in text  # capped at first 15

    def test_missing_optional_fields_do_not_raise(self):
        # A bundle built from a sparse DB row (no focus/industries/etc).
        bundle_text({"firm_id": "sparse"})


class TestBuildBatchPrompt:
    def test_includes_pitch_description_and_profile(self):
        prompt = build_batch_prompt(
            "A fintech startup for SMB lending",
            {"sectors": ["fintech"], "stages": ["seed"], "geography": ["EU"]},
            [{"firm_id": "f1", "name": "Firm One"}],
        )
        assert "A fintech startup for SMB lending" in prompt
        assert "sectors=['fintech']" in prompt
        assert "slug=f1" in prompt

    def test_handles_none_project_profile(self):
        prompt = build_batch_prompt("desc", None, [{"firm_id": "f1"}])
        assert "sectors=None" in prompt


# ---------------------------------------------------------------------------
# Impure: grade_candidates fault injection (fake client, no network)
# ---------------------------------------------------------------------------


class _RaisingClientFactory:
    """Simulates "Gemini client can't even be constructed" (e.g. missing
    API key, SDK import error surfaced at construction time)."""

    def __call__(self):
        raise RuntimeError("simulated: no GEMINI_API_KEY in environment")


class _FakeModels:
    def __init__(self, behavior):
        self._behavior = behavior  # callable(contents) -> fake response or raises

    def generate_content(self, model, contents, config):
        return self._behavior(contents)


class _FakeClient:
    def __init__(self, behavior):
        self.models = _FakeModels(behavior)


class _FakeResponse:
    def __init__(self, text, usage_metadata=None):
        self.text = text
        self.usage_metadata = usage_metadata


def _always_raises(contents):
    raise RuntimeError("simulated transient Gemini API failure")


class TestGradeCandidatesFallback:
    def test_empty_candidate_list_returns_empty_result_no_client_call(self):
        result = grade_candidates("pitch", {}, [], client_factory=_RaisingClientFactory())
        assert result.candidate_ids == []
        assert result.fallback_used is False

    def test_client_construction_failure_falls_back_to_incoming_order(self, caplog):
        bundles = [{"firm_id": "a"}, {"firm_id": "b"}, {"firm_id": "c"}]
        with caplog.at_level(logging.WARNING):
            result = grade_candidates(
                "pitch", {}, bundles, client_factory=_RaisingClientFactory()
            )
        # Never a silent empty result — always the full incoming id list.
        assert result.candidate_ids == ["a", "b", "c"]
        assert result.fallback_used is True
        assert result.n_graded == 0
        assert result.grades == {}
        assert any("falling back" in r.message for r in caplog.records)

    def test_grader_raises_on_every_call_falls_back_to_incoming_order(self, caplog):
        bundles = [{"firm_id": "a"}, {"firm_id": "b"}, {"firm_id": "c"}]
        fake_client = _FakeClient(behavior=_always_raises)
        with caplog.at_level(logging.WARNING):
            result = grade_candidates("pitch", {}, bundles, client=fake_client, max_workers=2)
        assert result.candidate_ids == ["a", "b", "c"]
        assert result.fallback_used is True
        assert result.cost_usd == 0.0
        assert any("falling back" in r.message or "failed after" in r.message for r in caplog.records)

    def test_successful_grading_reorders_by_grade(self):
        bundles = [{"firm_id": "a"}, {"firm_id": "b"}, {"firm_id": "c"}]

        def _good_response(contents):
            payload = (
                '[{"firm_slug": "a", "grade": 1, "reason": "meh"},'
                ' {"firm_slug": "b", "grade": 3, "reason": "great"},'
                ' {"firm_slug": "c", "grade": 0, "reason": "no"}]'
            )
            return _FakeResponse(text=payload)

        fake_client = _FakeClient(behavior=_good_response)
        result = grade_candidates("pitch", {}, bundles, client=fake_client, batch_size=6)
        assert result.candidate_ids == ["b", "a", "c"]
        assert result.fallback_used is False
        assert result.n_graded == 3
        assert result.grades["b"].grade == 3

    def test_partial_batch_failure_keeps_ungraded_candidates_in_place(self, caplog):
        # Two batches (batch_size=2): [a, b] succeeds, [c, d] raises.
        bundles = [{"firm_id": "a"}, {"firm_id": "b"}, {"firm_id": "c"}, {"firm_id": "d"}]
        call_count = {"n": 0}

        def _mixed(contents):
            call_count["n"] += 1
            if "slug=a" in contents:
                return _FakeResponse(
                    text='[{"firm_slug": "a", "grade": 3, "reason": "x"},'
                         ' {"firm_slug": "b", "grade": 0, "reason": "y"}]'
                )
            raise RuntimeError("simulated failure for the c/d batch")

        fake_client = _FakeClient(behavior=_mixed)
        with caplog.at_level(logging.WARNING):
            result = grade_candidates(
                "pitch", {}, bundles, client=fake_client, batch_size=2, max_workers=2
            )
        # a/b graded and reordered; c/d ungraded but PRESENT, keeping their
        # relative order (c before d) at the bottom.
        assert result.candidate_ids == ["a", "b", "c", "d"]
        assert result.fallback_used is False  # some grading succeeded
        assert result.n_graded == 2
        assert set(result.candidate_ids) == {"a", "b", "c", "d"}
        assert any("ungraded" in r.message for r in caplog.records)
