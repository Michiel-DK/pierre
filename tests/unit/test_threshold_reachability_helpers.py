"""Unit tests for scripts/analysis/_threshold_reachability_helpers.py.

Pure stdlib tests only — no DB, no database drivers, no repo imports beyond
the module under test (CI-strip dep set). Covers:
  - verdict_for's FAIL/WARN/PASS boundaries, with the ordering pin that
    matters most: a fully-dead threshold (count==0) must be FAIL even
    though its (observed_max - threshold) gap can also look "within margin"
    to a naive check.
  - build_row wires verdict_for into the assembled row correctly.
  - bonus_regression_note only fires PASS/WARN -> FAIL, never the reverse
    and never on a first-ever measurement (no previous row).
  - render_markdown doesn't crash on an empty/None-max row and includes
    every row's name.
"""
from __future__ import annotations

from scripts.analysis._threshold_reachability_helpers import (
    bonus_regression_note,
    build_row,
    render_markdown,
    verdict_for,
)


def test_fail_when_zero_rows_cross():
    # The canonical case this harness was built to catch: PORTFOLIO_SEMANTIC_
    # CONFLICT_THRESHOLD=0.80 measured on prod with observed max 0.7463 —
    # threshold - observed_max = 0.0537, well outside the 0.02 near-dead
    # margin, but the DEFINING symptom is count==0, which must dominate.
    assert verdict_for(0, 0.7463, 0.80) == "FAIL"


def test_fail_dominates_even_when_gap_looks_like_warn_range():
    # A dead threshold whose gap ALSO happens to fall inside the WARN margin
    # must still report FAIL, not WARN — ordering pin (advisor-flagged).
    assert verdict_for(0, 0.789, 0.80) == "FAIL"


def test_fail_when_observed_max_is_none():
    # No rows at all (denominator empty, or every row NULL) — degenerate but
    # must not crash and must not read as PASS.
    assert verdict_for(0, None, 0.80) == "FAIL"


def test_warn_when_barely_reachable():
    assert verdict_for(3, 0.79, 0.80) == "WARN"
    assert verdict_for(1, 0.801, 0.80) == "WARN"  # right at the margin boundary


def test_pass_when_comfortably_reachable():
    assert verdict_for(120, 0.91, 0.80) == "PASS"


def test_pass_boundary_just_outside_warn_margin():
    assert verdict_for(5, 0.821, 0.80) == "PASS"  # gap 0.021 > 0.02 margin


def test_build_row_attaches_verdict():
    row = build_row(
        name="PORTFOLIO_SEMANTIC_CONFLICT_THRESHOLD",
        threshold=0.80,
        count_at_or_above=0,
        observed_max=0.7463,
        n_total=37358,
        comparison="pitch vs portfolio company",
    )
    assert row["verdict"] == "FAIL"
    assert row["name"] == "PORTFOLIO_SEMANTIC_CONFLICT_THRESHOLD"
    assert row["n_total"] == 37358


def test_bonus_regression_note_fires_on_pass_to_fail():
    prev = {"verdict": "PASS"}
    cur = {"verdict": "FAIL", "name": "M14_EMBEDDING_CLUSTER_THRESHOLD"}
    note = bonus_regression_note(prev, cur)
    assert note is not None
    assert "M14_EMBEDDING_CLUSTER_THRESHOLD" in note
    assert "was reachable" in note


def test_bonus_regression_note_fires_on_warn_to_fail():
    prev = {"verdict": "WARN"}
    cur = {"verdict": "FAIL", "name": "X"}
    assert bonus_regression_note(prev, cur) is not None


def test_bonus_regression_note_silent_when_still_fail():
    prev = {"verdict": "FAIL"}
    cur = {"verdict": "FAIL", "name": "X"}
    assert bonus_regression_note(prev, cur) is None


def test_bonus_regression_note_silent_when_improving():
    prev = {"verdict": "FAIL"}
    cur = {"verdict": "PASS", "name": "X"}
    assert bonus_regression_note(prev, cur) is None


def test_bonus_regression_note_silent_with_no_previous_row():
    cur = {"verdict": "FAIL", "name": "X"}
    assert bonus_regression_note(None, cur) is None


def test_render_markdown_includes_every_row_name_and_handles_none_max():
    rows = [
        build_row(
            name="THRESH_A", threshold=0.8, count_at_or_above=0,
            observed_max=0.73, n_total=100, comparison="a vs b",
        ),
        build_row(
            name="THRESH_B", threshold=0.5, count_at_or_above=0,
            observed_max=None, n_total=0, comparison="c vs d",
        ),
    ]
    md = render_markdown(rows, captured_at="2026-07-18T00:00:00Z")
    assert "THRESH_A" in md
    assert "THRESH_B" in md
    assert "n/a" in md  # None max rendered safely, not a crash/None string
    assert "🔴 FAIL" in md
    assert "2026-07-18T00:00:00Z" in md
