"""Consistency pins for the golden competitor-threshold calibration snapshot.

Every derived aggregate stored in
``tests/golden/competitor_threshold_calibration_snapshot.json`` must be
reproducible from the per-row similarity samples stored next to it, using the
same pure functions the harness used to produce it. This guards hand or
offline edits of the rows (a dropped row with a stale aggregate fails here).

The well-described aggregate is not checked here: the snapshot does not record
which rows have a description, so it cannot be recomputed from the file alone.

CI-strip safe: stdlib + the pure helper module only.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pierre.matching._competitor_threshold_calibration import (
    separation_report,
    zero_fp_threshold,
)

_SNAPSHOT = (
    Path(__file__).resolve().parent.parent
    / "golden"
    / "competitor_threshold_calibration_snapshot.json"
)


@pytest.fixture(scope="module")
def snapshot() -> dict:
    return json.loads(_SNAPSHOT.read_text())


def _sims(rows: list[dict]) -> list[float]:
    return [r["similarity"] for r in rows]


def test_row_counts_match_stored_lists(snapshot):
    for case in snapshot["cases"]:
        assert case["positive_rows_found"] == len(case["positive_matches"])
        assert case["negative_easy_rows_found"] == len(case["negative_easy_matches"])
        assert case["negative_hard_rows_found"] == len(case["negative_hard_matches"])
        assert case["well_described_positive_rows_found"] <= case["positive_rows_found"]
        assert case["well_described_negative_rows_found"] <= (
            case["negative_easy_rows_found"] + case["negative_hard_rows_found"]
        )


def test_matched_names_were_requested(snapshot):
    for case in snapshot["cases"]:
        requested = {n.lower() for n in case["positive_company_names_requested"]}
        for row in case["positive_matches"]:
            assert row["company_name"].lower() in requested


def test_per_case_zero_fp_floor_reproduces(snapshot):
    for case in snapshot["cases"]:
        expected = zero_fp_threshold(
            _sims(case["positive_matches"]),
            _sims(case["negative_easy_matches"]) + _sims(case["negative_hard_matches"]),
        )
        assert case["zero_fp_floor_this_case"] == expected, case["pitch_id"]


def test_overall_separation_reports_reproduce(snapshot):
    cases = snapshot["cases"]
    pos = [v for c in cases for v in _sims(c["positive_matches"])]
    easy = [v for c in cases for v in _sims(c["negative_easy_matches"])]
    hard = [v for c in cases for v in _sims(c["negative_hard_matches"])]
    assert snapshot["overall_separation_vs_all_negatives"] == separation_report(pos, easy + hard)
    assert snapshot["separation_vs_hard_negatives_only"] == separation_report(pos, hard)
    assert snapshot["separation_vs_easy_negatives_only"] == separation_report(pos, easy)
