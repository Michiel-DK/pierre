"""Pure stats helpers for the pitch↔company competitor-threshold calibration.

Background
----------
``PORTFOLIO_SEMANTIC_CONFLICT_THRESHOLD`` (0.80) and
``PORTFOLIO_SEMANTIC_ADJACENT_THRESHOLD`` (0.75) in the source system's
config were calibrated on **company↔company** embedding pairs (both
``RETRIEVAL_DOCUMENT`` task type — e.g. two fintech companies at 0.80).
Production's per-portfolio-company semantic-competitor query instead
compares **pitch↔company** — the pitch is embedded with ``embed_query``
(``RETRIEVAL_QUERY``), the portfolio company with ``embed_documents``
(``RETRIEVAL_DOCUMENT``). Mixed task types sit on a measurably lower
distribution, so the 0.80/0.75 thresholds fire zero times in production
(the original evidence: no production row reached 0.80).

This module holds the pure-Python distribution math used by
``scripts/analysis/competitor_threshold_calibration_harness.py`` to decide
whether a *correctly calibrated* threshold exists for the pitch↔company
distribution — i.e. whether known-competitor (positive) similarities and
known-non-competitor (negative) similarities separate into a band a single
threshold can sit in.

CI-strip safe: stdlib only (``statistics``), no database driver or numpy
import. The harness lives in ``scripts/analysis/``; this module is importable
anywhere.
"""
from __future__ import annotations

import statistics
from typing import Sequence


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity between two equal-length vectors.

    Pure dot-product/magnitude implementation (no numpy) so this stays
    stdlib-only. Production vectors are already L2-normalised at write time,
    so in practice this reduces to a dot
    product — but this helper does not assume that, matching what pgvector's
    ``1 - (embedding <=> vec)`` computes regardless of normalisation.

    >>> round(cosine_similarity([1.0, 0.0], [1.0, 0.0]), 4)
    1.0
    >>> round(cosine_similarity([1.0, 0.0], [0.0, 1.0]), 4)
    0.0
    >>> round(cosine_similarity([1.0, 0.0], [-1.0, 0.0]), 4)
    -1.0
    """
    if len(a) != len(b):
        raise ValueError(f"vector length mismatch: {len(a)} vs {len(b)}")
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = sum(x * x for x in a) ** 0.5
    mag_b = sum(y * y for y in b) ** 0.5
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)


def distribution_stats(values: Sequence[float]) -> dict:
    """Summary stats for a similarity distribution: n, mean, median, min, max,
    stdev, and the 5th/25th/75th/95th percentiles (nearest-rank, no
    interpolation dependency — stdlib ``statistics.quantiles`` needs n>=2).

    Returns an all-``None`` shell (n=0) for an empty input rather than
    raising — callers should check ``n`` before trusting the other fields.

    >>> distribution_stats([])['n']
    0
    >>> stats = distribution_stats([0.60, 0.65, 0.70, 0.75, 0.80])
    >>> stats['n'], stats['min'], stats['max']
    (5, 0.6, 0.8)
    >>> round(stats['mean'], 4)
    0.7
    """
    values = list(values)
    n = len(values)
    if n == 0:
        return {
            "n": 0, "mean": None, "median": None, "min": None, "max": None,
            "stdev": None, "p5": None, "p25": None, "p75": None, "p95": None,
        }
    sorted_vals = sorted(values)

    def _percentile(p: float) -> float:
        # Nearest-rank method — deterministic, no interpolation, works for n=1.
        idx = min(n - 1, max(0, round(p * (n - 1))))
        return sorted_vals[idx]

    return {
        "n": n,
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "min": sorted_vals[0],
        "max": sorted_vals[-1],
        "stdev": statistics.pstdev(values) if n > 1 else 0.0,
        "p5": _percentile(0.05),
        "p25": _percentile(0.25),
        "p75": _percentile(0.75),
        "p95": _percentile(0.95),
    }


def zero_fp_threshold(
    positives: Sequence[float],
    negatives: Sequence[float],
    *,
    margin: float = 0.001,
) -> dict:
    """The precision-first calibration number for a penalty-driving tier.

    ``separation_report``'s balanced-accuracy view (Youden's J / a gap
    computed from ``min(positives) - max(negatives)``) is dominated by 1-2
    edge points and isn't the right target for a threshold that fires a
    -20% score penalty + a "direct competitor" narrative — a false positive
    there is much costlier than a false negative (a missed competitor stays
    silent; a wrongly-flagged one actively damages an unrelated firm's
    score). The right number is: **the lowest threshold at which ZERO
    labeled negatives fire**, and the recall (fraction of labeled positives
    still retained) at that threshold.

    Returns ``{floor, threshold, recall, n_positives, n_negatives,
    positives_retained, positives_missed}`` where ``floor`` is
    ``max(negatives)`` (any threshold at or below this fires on at least one
    known non-competitor) and ``threshold`` is ``floor + margin`` (the
    smallest value that clears it, since production compares with ``>=``).

    >>> r = zero_fp_threshold([0.60, 0.65, 0.70], [0.50, 0.55, 0.58])
    >>> r['floor'], round(r['threshold'], 3), r['recall']
    (0.58, 0.581, 1.0)
    >>> r2 = zero_fp_threshold([0.55, 0.65, 0.70], [0.50, 0.55, 0.58])
    >>> r2['recall']
    0.6666666666666666
    """
    positives = list(positives)
    negatives = list(negatives)
    if not negatives:
        floor = None
        threshold = min(positives) if positives else None
    else:
        floor = max(negatives)
        threshold = floor + margin
    if not positives or threshold is None:
        recall = None
        retained = missed = None
    else:
        retained = sum(1 for v in positives if v >= threshold)
        missed = len(positives) - retained
        recall = retained / len(positives)
    return {
        "floor": floor,
        "threshold": threshold,
        "recall": recall,
        "n_positives": len(positives),
        "n_negatives": len(negatives),
        "positives_retained": retained,
        "positives_missed": missed,
    }


def separation_report(positives: Sequence[float], negatives: Sequence[float]) -> dict:
    """Assess whether ``positives`` (known-competitor similarities) and
    ``negatives`` (known-non-competitor similarities) separate into a band a
    single threshold could sit in.

    A threshold ``t`` correctly classifies every labeled pair iff
    ``max(negatives) < t <= min(positives)``. That's only possible when
    ``min(positives) > max(negatives)`` — a clean gap. This function reports
    that gap (positive when clean, negative/zero when the distributions
    overlap) plus an overlap count (how many negatives would false-positive,
    and positives would false-negative, at the *midpoint* candidate
    threshold) so a human can see HOW BAD an overlap is, not just that one
    exists.

    >>> r = separation_report([0.72, 0.75, 0.80], [0.50, 0.55, 0.60])
    >>> r['clean_separation'], round(r['gap'], 2), round(r['candidate_threshold'], 3)
    (True, 0.12, 0.66)
    >>> r2 = separation_report([0.60, 0.72], [0.55, 0.70])
    >>> r2['clean_separation']
    False
    """
    positives = list(positives)
    negatives = list(negatives)
    pos_stats = distribution_stats(positives)
    neg_stats = distribution_stats(negatives)

    if pos_stats["n"] == 0 or neg_stats["n"] == 0:
        return {
            "positive_stats": pos_stats,
            "negative_stats": neg_stats,
            "clean_separation": False,
            "gap": None,
            "candidate_threshold": None,
            "false_positives_at_candidate": None,
            "false_negatives_at_candidate": None,
            "reason": "need at least one positive AND one negative to assess separation",
        }

    gap = pos_stats["min"] - neg_stats["max"]
    clean = gap > 0
    candidate = (pos_stats["min"] + neg_stats["max"]) / 2.0 if clean else None

    # Overlap detail even when not clean: how many mislabels at the best
    # achievable single threshold (Youden's J over the observed values —
    # pick the cut point among sorted values that maximizes
    # TPR - FPR). Kept simple/exhaustive since n is small (calibration
    # fixture sizes, not production-scale).
    all_candidates = sorted(set(positives) | set(negatives))
    best_t = all_candidates[0]
    best_j = -1.0
    for t in all_candidates:
        tp = sum(1 for v in positives if v >= t)
        fn = len(positives) - tp
        fp = sum(1 for v in negatives if v >= t)
        tn = len(negatives) - fp
        tpr = tp / len(positives) if positives else 0.0
        fpr = fp / len(negatives) if negatives else 0.0
        j = tpr - fpr
        if j > best_j:
            best_j = j
            best_t = t
    if candidate is None:
        candidate = best_t

    fp_at_candidate = sum(1 for v in negatives if v >= candidate)
    fn_at_candidate = sum(1 for v in positives if v < candidate)

    return {
        "positive_stats": pos_stats,
        "negative_stats": neg_stats,
        "clean_separation": clean,
        "gap": gap,
        "candidate_threshold": candidate,
        "false_positives_at_candidate": fp_at_candidate,
        "false_negatives_at_candidate": fn_at_candidate,
        # Precision-first view (see zero_fp_threshold docstring) — the
        # decision-relevant number for a penalty-driving tier, since a false
        # positive there is much costlier than a false negative.
        "zero_fp": zero_fp_threshold(positives, negatives),
        "reason": (
            "min(positives) > max(negatives) — clean band exists"
            if clean
            else "positive and negative distributions overlap — no threshold separates them cleanly"
        ),
    }
