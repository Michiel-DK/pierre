"""CI-strip-safe guards for the claim-auditor calibration.

Two jobs, both runnable in a stripped-down environment (no database drivers,
no google.genai):

1. Pure-math unit tests for the calibration scoring
   (``pierre.agents._claim_auditor_calibration``) and the Policy-B band wrapper
   (``pierre.agents._claim_auditor.report_grounding_band`` →
   ``pierre.utils.grounding.groundedness_to_band``).

2. Structural guards on the committed fixture + snapshot artifacts — the
   behavioural guardrail is the harness itself
   (``scripts/analysis/claim_auditor_calibration_harness.py --check``), which
   needs Gemini and so can't run here. This pins the artifact SHAPE (pure JSON
   reads) and the fair-inference stress requirement so a hollow/empty fixture
   set can't slip through.

The imports below are all CI-strip safe: ``_claim_auditor_calibration`` is pure
stdlib; ``pierre.utils.grounding`` is pure stdlib; ``report_grounding_band`` lives
in ``_claim_auditor`` which imports only stdlib at module top.
"""
from __future__ import annotations

import json
from pathlib import Path

from pierre.agents._claim_auditor import report_grounding_band
from pierre.agents._claim_auditor_calibration import (
    confusion_matrix,
    effective_verdict,
    fabricated_false_positive_rate,
    fabricated_recall,
    per_verdict_precision_recall,
)

_GOLDEN = Path(__file__).resolve().parent.parent / "golden"
_FIXTURES = _GOLDEN / "claim_auditor_calibration_fixtures.json"
_SNAPSHOT = _GOLDEN / "claim_auditor_calibration_snapshot.json"


# ---------------------------------------------------------------------------
# report_grounding_band — Policy B (supported=verified, total=verified+fabricated)
# ---------------------------------------------------------------------------


def test_band_none_when_audit_absent():
    # Explicit contract: audit didn't run → None, NEVER "low".
    assert report_grounding_band(None) is None
    assert report_grounding_band({}) is None


def test_band_high_all_verified():
    # 5 verified, 0 fabricated → 5/5 = 1.0 → high. Unverifiable excluded.
    assert report_grounding_band({"verified": 5, "fabricated": 0, "unverifiable": 3}) == "high"


def test_band_unverifiable_excluded_from_denominator():
    # Policy B: a report that is all-verified + many unverifiable is STILL high —
    # fair inference must not drag the band down.
    assert report_grounding_band({"verified": 2, "fabricated": 0, "unverifiable": 10}) == "high"


def test_band_medium_half_fabricated():
    # 3 verified, 3 fabricated → 3/6 = 0.5 → medium.
    assert report_grounding_band({"verified": 3, "fabricated": 3, "unverifiable": 0}) == "medium"


def test_band_low_mostly_fabricated():
    # 1 verified, 3 fabricated → 1/4 = 0.25 → low.
    assert report_grounding_band({"verified": 1, "fabricated": 3, "unverifiable": 0}) == "low"


def test_band_none_when_only_unverifiable():
    # verified+fabricated == 0 → total<=0 → None (nothing checkable to score).
    assert report_grounding_band({"verified": 0, "fabricated": 0, "unverifiable": 7}) is None


def test_band_tolerates_missing_keys():
    # Defensive: missing keys coerce to 0.
    assert report_grounding_band({"verified": 4}) == "high"  # 4/4
    assert report_grounding_band({"fabricated": 2}) == "low"  # 0/2


# ---------------------------------------------------------------------------
# effective_verdict — most-severe aggregation
# ---------------------------------------------------------------------------


def test_effective_verdict_most_severe():
    assert effective_verdict(["verified", "verified", "verified"]) == "verified"
    assert effective_verdict(["verified", "unverifiable"]) == "unverifiable"
    assert effective_verdict(["verified", "fabricated", "unverifiable"]) == "fabricated"
    assert effective_verdict(["unverifiable", "unverifiable"]) == "unverifiable"


def test_effective_verdict_empty_is_no_claim():
    assert effective_verdict([]) == "no_claim"


def test_effective_verdict_ignores_unknown_tokens():
    assert effective_verdict(["weird", "verified"]) == "verified"
    assert effective_verdict(["weird", "garbage"]) == "no_claim"


# ---------------------------------------------------------------------------
# confusion matrix + headline metrics
# ---------------------------------------------------------------------------


def test_confusion_matrix_shape_is_stable():
    matrix = confusion_matrix([("verified", "verified"), ("fabricated", "fabricated")])
    assert set(matrix.keys()) == {"verified", "fair_inference", "fabricated"}
    for row in matrix.values():
        assert set(row.keys()) == {"verified", "unverifiable", "fabricated", "no_claim"}
    assert matrix["verified"]["verified"] == 1
    assert matrix["fabricated"]["fabricated"] == 1


def test_fabricated_fp_rate_counts_only_should_survive_labels():
    # fair_inference flagged fabricated = a false positive; fabricated→fabricated
    # is NOT counted in the FP denominator.
    pairs = [
        ("fair_inference", "fabricated"),  # FP
        ("fair_inference", "unverifiable"),  # ok
        ("verified", "verified"),  # ok
        ("verified", "fabricated"),  # FP
        ("fabricated", "fabricated"),  # not in denom
    ]
    # 2 FPs out of 4 should-survive fixtures.
    assert fabricated_false_positive_rate(pairs) == 0.5


def test_fabricated_fp_rate_none_when_no_survive_fixtures():
    assert fabricated_false_positive_rate([("fabricated", "fabricated")]) is None


def test_fabricated_recall():
    pairs = [
        ("fabricated", "fabricated"),  # caught
        ("fabricated", "unverifiable"),  # missed
        ("verified", "verified"),  # irrelevant
    ]
    assert fabricated_recall(pairs) == 0.5
    assert fabricated_recall([("verified", "verified")]) is None


def test_per_verdict_precision_recall_maps_fair_inference_to_unverifiable():
    pairs = [
        ("fair_inference", "unverifiable"),  # tp for unverifiable
        ("verified", "unverifiable"),  # fp for unverifiable
        ("fair_inference", "verified"),  # fn for unverifiable
    ]
    pr = per_verdict_precision_recall(pairs)
    assert pr["unverifiable"]["tp"] == 1
    assert pr["unverifiable"]["fp"] == 1
    assert pr["unverifiable"]["fn"] == 1
    assert pr["unverifiable"]["precision"] == 0.5
    assert pr["unverifiable"]["recall"] == 0.5


# ---------------------------------------------------------------------------
# Fixture artifact shape + fair-inference stress guard
# ---------------------------------------------------------------------------


def test_fixtures_well_formed():
    spec = json.loads(_FIXTURES.read_text())
    assert spec["fixtures"], "no calibration fixtures defined"
    assert isinstance(spec.get("fp_rate_threshold"), (int, float))
    context_ids = set(spec["contexts"].keys())
    assert context_ids, "no frozen contexts defined"
    seen_ids = set()
    for fx in spec["fixtures"]:
        assert fx["id"] and fx["id"] not in seen_ids, f"missing/duplicate fixture id: {fx}"
        seen_ids.add(fx["id"])
        assert fx["label"] in ("verified", "fair_inference", "fabricated"), (
            f"{fx['id']}: bad label {fx['label']!r}"
        )
        assert fx["claim"].strip(), f"{fx['id']}: empty claim"
        assert fx["context_id"] in context_ids, (
            f"{fx['id']}: references unknown context {fx['context_id']!r}"
        )


def test_fixture_set_stresses_the_fair_inference_danger_zone():
    """The calibration is HOLLOW if it doesn't include enough fair_inference
    fixtures — those are the operator's exact concern (claims that should be
    `unverifiable` but get stamped `fabricated`). Assert the labeled set is
    weighted toward the danger zone, not a uniform 3-way split."""
    spec = json.loads(_FIXTURES.read_text())
    labels = [fx["label"] for fx in spec["fixtures"]]
    n_fair = labels.count("fair_inference")
    assert n_fair >= 12, (
        f"only {n_fair} fair_inference fixtures — the FP-rate signal is hollow "
        f"without enough fair-inference claims to stress the auditor's "
        f"over-flagging. Add more derivable-but-unsupported claims."
    )
    # Every label class must be represented (a confusion matrix needs all rows).
    assert labels.count("verified") >= 5
    assert labels.count("fabricated") >= 5


def test_snapshot_present_and_consistent():
    """The snapshot is regenerated by the harness; pin its shape + that the
    captured FP-rate is at or under the fixture threshold (the harness gate)."""
    assert _SNAPSHOT.exists(), (
        "calibration snapshot missing — regenerate with "
        "scripts/analysis/claim_auditor_calibration_harness.py"
    )
    snap = json.loads(_SNAPSHOT.read_text())
    spec = json.loads(_FIXTURES.read_text())
    # Snapshot must cover every fixture id.
    snap_ids = {p["id"] for p in snap["per_fixture"]}
    fixture_ids = {fx["id"] for fx in spec["fixtures"]}
    assert snap_ids == fixture_ids, (
        f"snapshot ids != fixture ids — regenerate the harness snapshot "
        f"(missing: {fixture_ids - snap_ids}, extra: {snap_ids - fixture_ids})"
    )
    assert "fabricated_false_positive_rate" in snap
    assert "confusion_matrix" in snap
    fp = snap["fabricated_false_positive_rate"]
    threshold = snap["fp_rate_threshold"]
    # The committed snapshot must itself pass the gate — a regression in the
    # auditor that pushes the FP-rate over threshold should not be committable
    # without this test going red. A None FP-rate is NOT a pass: it means the
    # capture scored zero verified/fair_inference fixtures (degenerate run), so
    # require a real number at or under threshold (silent-failure-hunter #2).
    assert fp is not None and fp <= threshold, (
        f"committed snapshot FP-rate {fp} is None or exceeds threshold {threshold} — "
        f"the auditor is over-flagging legitimate claims, or the capture was degenerate"
    )
    # No fixture may have errored to no_claim in the committed snapshot — that
    # would mean the captured FP-rate was measured on a corrupted run that
    # silently deflated it (silent-failure-hunter #1).
    assert not snap.get("no_claim_fixtures"), (
        f"committed snapshot has no_claim fixtures {snap.get('no_claim_fixtures')} — "
        f"the auditor errored on these; re-capture a clean snapshot"
    )
    # Recall floor must hold on the committed snapshot too — a low FP-rate is
    # hollow if the auditor stopped catching real fabrications (#3).
    recall = snap.get("fabricated_recall")
    recall_floor = snap.get("recall_floor", 0.8)
    assert recall is not None and recall >= recall_floor, (
        f"committed snapshot recall {recall} < floor {recall_floor} — "
        f"the auditor is missing real fabrications"
    )
