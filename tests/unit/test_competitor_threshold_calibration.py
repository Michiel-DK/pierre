"""Pure-helper tests for the pitch↔company competitor-threshold calibration
stats.

Covers ``pierre/matching/_competitor_threshold_calibration.py``: cosine
similarity, distribution summary stats, the precision-first zero-FP floor,
and the balanced-accuracy separation report. The harness that produces the
real measurement needed the original database + Gemini; this file only
covers the pure math.

CI-strip safe: imports only the pure helper module, which in
turn imports only stdlib (``statistics``).
"""
from __future__ import annotations

from pierre.matching._competitor_threshold_calibration import (
    cosine_similarity,
    distribution_stats,
    separation_report,
    zero_fp_threshold,
)


class TestCosineSimilarity:
    def test_identical_vectors(self):
        assert cosine_similarity([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == 1.0 or \
            abs(cosine_similarity([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) - 1.0) < 1e-9

    def test_orthogonal_vectors(self):
        assert abs(cosine_similarity([1.0, 0.0], [0.0, 1.0])) < 1e-9

    def test_opposite_vectors(self):
        assert abs(cosine_similarity([1.0, 0.0], [-1.0, 0.0]) - (-1.0)) < 1e-9

    def test_zero_vector_returns_zero_not_raise(self):
        assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0

    def test_length_mismatch_raises(self):
        try:
            cosine_similarity([1.0], [1.0, 2.0])
            assert False, "expected ValueError"
        except ValueError:
            pass

    def test_unnormalized_vectors_match_normalized_ratio(self):
        # Cosine similarity is scale-invariant — scaling one vector doesn't
        # change the result. Guards against an accidental dot-product-only
        # implementation (which WOULD be scale-sensitive).
        a = [1.0, 2.0, 3.0]
        b = [2.0, 4.0, 6.0]  # 2x scale of a — cosine sim must be ~1.0
        assert abs(cosine_similarity(a, b) - 1.0) < 1e-9


class TestDistributionStats:
    def test_empty_input(self):
        stats = distribution_stats([])
        assert stats["n"] == 0
        assert stats["mean"] is None

    def test_single_value(self):
        stats = distribution_stats([0.7])
        assert stats["n"] == 1
        assert stats["mean"] == 0.7
        assert stats["min"] == stats["max"] == 0.7
        assert stats["stdev"] == 0.0

    def test_basic_stats(self):
        stats = distribution_stats([0.60, 0.65, 0.70, 0.75, 0.80])
        assert stats["n"] == 5
        assert stats["min"] == 0.60
        assert stats["max"] == 0.80
        assert abs(stats["mean"] - 0.70) < 1e-9
        assert stats["median"] == 0.70

    def test_percentiles_within_range(self):
        stats = distribution_stats([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0])
        assert stats["min"] <= stats["p5"] <= stats["p25"] <= stats["median"] <= stats["p75"] <= stats["p95"] <= stats["max"]


class TestZeroFpThreshold:
    def test_clean_case_full_recall(self):
        result = zero_fp_threshold([0.60, 0.65, 0.70], [0.50, 0.55, 0.58])
        assert result["floor"] == 0.58
        assert result["threshold"] > 0.58
        assert result["recall"] == 1.0
        assert result["positives_retained"] == 3
        assert result["positives_missed"] == 0

    def test_overlap_reduces_recall(self):
        # One positive (0.55) sits below the negative ceiling (0.58) — it's
        # necessarily excluded by any zero-FP threshold.
        result = zero_fp_threshold([0.55, 0.65, 0.70], [0.50, 0.55, 0.58])
        assert result["positives_retained"] == 2
        assert result["positives_missed"] == 1
        assert abs(result["recall"] - 2 / 3) < 1e-9

    def test_no_negatives_uses_min_positive(self):
        result = zero_fp_threshold([0.6, 0.7], [])
        assert result["floor"] is None
        assert result["threshold"] == 0.6
        assert result["recall"] == 1.0

    def test_no_positives_returns_none_recall(self):
        result = zero_fp_threshold([], [0.5, 0.6])
        assert result["recall"] is None
        assert result["floor"] == 0.6

    def test_margin_is_respected(self):
        result = zero_fp_threshold([0.7], [0.5], margin=0.01)
        assert abs(result["threshold"] - 0.51) < 1e-9


class TestSeparationReport:
    def test_clean_separation(self):
        report = separation_report([0.72, 0.75, 0.80], [0.50, 0.55, 0.60])
        assert report["clean_separation"] is True
        assert report["gap"] > 0
        assert report["false_positives_at_candidate"] == 0
        assert report["false_negatives_at_candidate"] == 0

    def test_overlapping_distributions_not_clean(self):
        # This is the shape of the real threshold-calibration finding: hand-
        # labeled positives and negatives overlap once realistic negatives
        # are included (see the harness's candidate-pool flood check for the
        # full story — this unit test only covers the overlap-detection math).
        report = separation_report([0.55, 0.60, 0.72], [0.50, 0.58, 0.65])
        assert report["clean_separation"] is False
        assert report["gap"] <= 0

    def test_missing_positives_or_negatives(self):
        report = separation_report([], [0.5, 0.6])
        assert report["clean_separation"] is False
        assert report["candidate_threshold"] is None
        assert "need at least one positive" in report["reason"]

    def test_includes_zero_fp_view(self):
        report = separation_report([0.72, 0.75], [0.50, 0.55])
        assert "zero_fp" in report
        assert report["zero_fp"]["recall"] == 1.0
