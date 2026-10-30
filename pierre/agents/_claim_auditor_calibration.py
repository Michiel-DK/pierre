"""Pure scoring math for the claim-auditor calibration harness.

This is the OFFLINE golden-harness counterpart to the runtime auditor
(``_claim_auditor.audit_report_claims``). It owns ONLY the pure arithmetic —
per-fixture verdict aggregation, the confusion matrix, the headline
``fabricated`` false-positive rate, and per-verdict precision/recall. The
Gemini call + fixture I/O live in
``scripts/analysis/claim_auditor_calibration_harness.py``; this module is what
the CI-strip-safe unit test imports.

**CI-strip-safe**: imports ONLY the standard library + ``typing``, so the unit
test runs in a minimal dependency set.

Why a per-fixture AGGREGATION rule (not 1 claim → 1 verdict)
-----------------------------------------------------------
A probe run confirmed the auditor extracts MULTIPLE atomic audits from a
single narrative sentence (e.g. "<firm> has backed <company>, a quantum
photonics company" → three ``verified`` audits). So a fixture's
ground-truth label can't be compared to a single audit verdict. Instead each
fixture's EFFECTIVE verdict is the *most severe* verdict the auditor assigned
to any sub-claim it extracted:

    fabricated  >  unverifiable  >  verified

This models the real downstream consequence: the softener
(``_soften_fabricated_clauses``) strips a span if ANY part of it is
``fabricated``. So "did the auditor effectively flag this fixture as
fabricated?" == "would this fixture's text get stripped from the report?" —
which is exactly the operator's concern (fair-inference content being
stripped).

The label vocabulary (ground truth) and the auditor verdict vocabulary differ
by one name — ``fair_inference`` (label) maps to ``unverifiable`` (verdict). The headline metric does NOT require fair_inference
to land exactly on ``unverifiable``; it only requires it to NOT land on
``fabricated`` (a verified-grounding upgrade is fine — it survives softening).
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# Ground-truth fixture labels.
LABELS = ("verified", "fair_inference", "fabricated")
# Auditor verdict vocabulary + a synthetic bucket for "auditor extracted no
# checkable claim from the fixture" (0 audits emitted).
VERDICTS = ("verified", "unverifiable", "fabricated", "no_claim")

# Severity order for the most-severe aggregation. Higher = more severe.
_SEVERITY = {"verified": 0, "unverifiable": 1, "fabricated": 2}


def effective_verdict(audit_verdicts: Iterable[str]) -> str:
    """Reduce a fixture's per-sub-claim audit verdicts to one effective verdict.

    Returns the MOST SEVERE verdict (``fabricated`` > ``unverifiable`` >
    ``verified``). Unknown verdict strings are ignored. When the iterable is
    empty (auditor extracted no checkable claim), returns ``"no_claim"``.
    """
    worst: Optional[str] = None
    worst_sev = -1
    for v in audit_verdicts:
        sev = _SEVERITY.get(v)
        if sev is None:
            continue
        if sev > worst_sev:
            worst_sev = sev
            worst = v
    return worst if worst is not None else "no_claim"


def _label_should_not_be_fabricated(label: str) -> bool:
    """True for labels whose fixtures must SURVIVE softening (not stripped).

    ``verified`` and ``fair_inference`` are both legitimate, grounded-or-
    derivable content. Only the ``fabricated`` label is expected to be
    stripped. This is the denominator population for the false-positive rate.
    """
    return label in ("verified", "fair_inference")


def confusion_matrix(pairs: Iterable[tuple]) -> dict:
    """Build the (label × effective-verdict) confusion matrix.

    ``pairs`` is an iterable of ``(label, effective_verdict)`` tuples. Returns
    a nested dict ``{label: {verdict: count}}`` covering every LABELS × VERDICTS
    cell (zero-filled) so the shape is stable across runs.
    """
    matrix = {lab: {v: 0 for v in VERDICTS} for lab in LABELS}
    for label, verdict in list(pairs):  # materialize — safe if a generator slips in
        if label not in matrix:
            continue
        if verdict not in matrix[label]:
            # Prompt drift could introduce an out-of-vocabulary verdict (e.g.
            # "partially_verified"). Don't let it accumulate invisibly in the
            # no_claim bucket — warn so the operator notices the vocabulary
            # moved (python-reviewer MEDIUM).
            logger.warning(
                "confusion_matrix: out-of-vocabulary verdict %r (label=%r) — "
                "counting as no_claim. Auditor verdict vocabulary may have drifted.",
                verdict,
                label,
            )
            verdict = "no_claim"
        matrix[label][verdict] += 1
    return matrix


def fabricated_false_positive_rate(pairs: Iterable[tuple]) -> Optional[float]:
    """Headline metric: of the fixtures that should SURVIVE softening
    (label ∈ {verified, fair_inference}), what fraction did the auditor
    effectively flag ``fabricated`` (i.e. would be stripped)?

    Returns ``None`` when there are no such fixtures (can't divide by zero).
    Lower is better; the operator's standing concern is this number being
    non-trivial on ``fair_inference`` claims.
    """
    denom = 0
    false_pos = 0
    for label, verdict in list(pairs):  # materialize — safe if a generator slips in
        if not _label_should_not_be_fabricated(label):
            continue
        denom += 1
        if verdict == "fabricated":
            false_pos += 1
    if denom == 0:
        return None
    return false_pos / denom


def fabricated_recall(pairs: Iterable[tuple]) -> Optional[float]:
    """Of the fixtures genuinely labeled ``fabricated``, what fraction did the
    auditor effectively catch (effective verdict == ``fabricated``)?

    Returns ``None`` when there are no ``fabricated`` fixtures. This is the
    counterweight to the FP-rate — loosening the auditor to drive FP-rate to 0
    must not also tank recall (it would stop catching real fabrications).
    """
    denom = 0
    caught = 0
    for label, verdict in list(pairs):  # materialize — safe if a generator slips in
        if label != "fabricated":
            continue
        denom += 1
        if verdict == "fabricated":
            caught += 1
    if denom == 0:
        return None
    return caught / denom


def per_verdict_precision_recall(pairs: Iterable[tuple]) -> dict:
    """Precision/recall per auditor verdict, treating ``fair_inference`` as the
    ground truth for ``unverifiable``.

    Returns ``{verdict: {"precision": float|None, "recall": float|None,
    "tp": int, "fp": int, "fn": int}}`` for the three real verdicts
    (``verified`` / ``unverifiable`` / ``fabricated``). ``no_claim`` is not a
    target class — it only appears as a (mis)prediction and shows up in the
    confusion matrix.

    Label→verdict ground-truth map: ``verified``→``verified``,
    ``fair_inference``→``unverifiable``, ``fabricated``→``fabricated``.
    """
    label_to_gt = {
        "verified": "verified",
        "fair_inference": "unverifiable",
        "fabricated": "fabricated",
    }
    materialized = list(pairs)
    out: dict = {}
    for verdict in ("verified", "unverifiable", "fabricated"):
        tp = fp = fn = 0
        for label, pred in materialized:
            gt = label_to_gt.get(label)
            if pred == verdict and gt == verdict:
                tp += 1
            elif pred == verdict and gt != verdict:
                fp += 1
            elif pred != verdict and gt == verdict:
                fn += 1
        precision = tp / (tp + fp) if (tp + fp) else None
        recall = tp / (tp + fn) if (tp + fn) else None
        out[verdict] = {
            "precision": precision,
            "recall": recall,
            "tp": tp,
            "fp": fp,
            "fn": fn,
        }
    return out
