"""Pure verdict/rendering helpers for the threshold-reachability test.

Stdlib-only (unit tests must import this without database drivers on the
path). No DB, no LLM, no ``pierre`` import. Companion to the source system's
reachability harness (not included here), which does the DB/embedding
plumbing (the production-pairing SQL) and calls into this module only for the
pure math + table rendering.

The bug this harness catches: a threshold calibrated on one distribution
(e.g. company<->company pairs) silently applied to another (pitch<->company)
at the production call site, so the threshold is dead code that still looks
like a working feature (PORTFOLIO_SEMANTIC_CONFLICT_THRESHOLD=0.80 sat dead for ~1 month: no row in
the production corpus ever crossed it). The mechanical catch: for
every threshold constant, does ANY row in the real corpus, compared the same
way production compares it, actually cross the bar?
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# A threshold whose observed max sits within this margin BELOW it is "nearly
# dead" even though it technically has >=1 crossing row — worth a WARN, not
# a silent PASS.
_NEAR_DEAD_MARGIN = 0.02


def verdict_for(count_at_or_above: int, observed_max: Optional[float], threshold: float,
                 near_dead_margin: float = _NEAR_DEAD_MARGIN) -> str:
    """Classify one threshold's reachability as FAIL / WARN / PASS.

    Order matters: a dead threshold (count==0) has ``observed_max < threshold``,
    so ``threshold - observed_max`` is POSITIVE and could also satisfy a naive
    "within margin" check — FAIL must be checked first or a fully-dead
    threshold gets mislabeled WARN.

    >>> verdict_for(0, 0.7463, 0.80)
    'FAIL'
    >>> verdict_for(3, 0.79, 0.80)
    'WARN'
    >>> verdict_for(120, 0.91, 0.80)
    'PASS'
    >>> verdict_for(0, None, 0.80)
    'FAIL'
    """
    if count_at_or_above <= 0:
        return "FAIL"
    if observed_max is not None and (observed_max - threshold) <= near_dead_margin:
        return "WARN"
    return "PASS"


def build_row(
    *,
    name: str,
    threshold: float,
    count_at_or_above: int,
    observed_max: Optional[float],
    n_total: int,
    comparison: str,
    note: str = "",
) -> Dict[str, Any]:
    """Assemble one reachability-table row with its verdict attached."""
    return {
        "name": name,
        "threshold": threshold,
        "count_at_or_above": count_at_or_above,
        "observed_max": observed_max,
        "n_total": n_total,
        "comparison": comparison,
        "note": note,
        "verdict": verdict_for(count_at_or_above, observed_max, threshold),
    }


def bonus_regression_note(prev_row: Optional[Dict[str, Any]], cur_row: Dict[str, Any]) -> Optional[str]:
    """Bonus catch: a threshold that used to be reachable (PASS/WARN in a
    previously-committed snapshot) and stopped (FAIL now) — a silent
    regression (embedding-model swap, data drift) nothing else detects.
    Returns None when there's nothing to flag.

    >>> bonus_regression_note({"verdict": "PASS"}, {"verdict": "FAIL", "name": "X"})
    'X: was reachable in the committed snapshot, is now FAIL — data drift or an embedding-model swap likely broke it, not just a bad calibration.'
    >>> bonus_regression_note({"verdict": "FAIL"}, {"verdict": "FAIL", "name": "X"}) is None
    True
    >>> bonus_regression_note(None, {"verdict": "FAIL", "name": "X"}) is None
    True
    """
    if prev_row is None:
        return None
    if prev_row.get("verdict") in ("PASS", "WARN") and cur_row.get("verdict") == "FAIL":
        return (
            f"{cur_row['name']}: was reachable in the committed snapshot, is now "
            f"FAIL — data drift or an embedding-model swap likely broke it, not "
            f"just a bad calibration."
        )
    return None


def _fmt(x: Optional[float], digits: int = 4) -> str:
    return "n/a" if x is None else f"{x:.{digits}f}"


def render_markdown(rows: List[Dict[str, Any]], *, captured_at: Optional[str] = None) -> str:
    """Render the threshold · value · rows>= · observed max · verdict table.

    Pure string assembly — mirrors claim_auditor_calibration_harness.py's
    --render-md ergonomics (no DB / embedding calls needed to reproduce the
    doc from an already-captured snapshot).
    """
    lines: List[str] = []
    lines.append("# Threshold reachability report")
    lines.append("")
    lines.append(
        "> Measured by the source system's reachability harness on its private corpus; that "
        "harness needs the original database and is not part of this repo. The committed snapshot "
        "(`tests/golden/threshold_reachability_snapshot.json`) is the record. Re-render with "
        "`scripts/analysis/render_threshold_reachability.py`; do not hand-edit."
    )
    if captured_at:
        lines.append(">")
        lines.append(f"> Snapshot captured: **{captured_at}**")
    lines.append("")
    lines.append("## What this measures")
    lines.append("")
    lines.append(
        "For each threshold constant, the **production comparison** — same "
        "task pairing, same text shapes the real call path uses — run over "
        "the real local corpus. `rows >=` counts how many real comparisons "
        "actually cross the bar; `observed max` is the highest value seen. "
        "**FAIL** = zero rows ever cross it (dead code that looks healthy — "
        "a false-negative machine in production). **WARN** = at least one "
        "row crosses, but the observed max sits within ~0.02 of the "
        "threshold (barely reachable — one embedding refresh from dead)."
    )
    lines.append("")
    lines.append("## Results")
    lines.append("")
    lines.append("| Threshold | Value | Rows ≥ threshold | Observed max | Verdict | Comparison |")
    lines.append("|---|---|---|---|---|---|")
    for row in rows:
        verdict = row["verdict"]
        badge = {"FAIL": "🔴 FAIL", "WARN": "🟡 WARN", "PASS": "🟢 PASS"}.get(verdict, verdict)
        lines.append(
            f"| `{row['name']}` | {row['threshold']:.2f} | "
            f"{row['count_at_or_above']} / {row['n_total']} | "
            f"{_fmt(row['observed_max'])} | {badge} | {row['comparison']} |"
        )
    lines.append("")
    fails = [r for r in rows if r["verdict"] == "FAIL"]
    warns = [r for r in rows if r["verdict"] == "WARN"]
    if fails:
        lines.append("## Dead thresholds (FAIL)")
        lines.append("")
        for r in fails:
            note = f" {r['note']}" if r.get("note") else ""
            lines.append(
                f"- **`{r['name']}`** ({r['threshold']:.2f}): 0 of {r['n_total']} rows "
                f"cross it; observed max {_fmt(r['observed_max'])}.{note}"
            )
        lines.append("")
    if warns:
        lines.append("## Nearly-dead thresholds (WARN)")
        lines.append("")
        for r in warns:
            note = f" {r['note']}" if r.get("note") else ""
            lines.append(
                f"- **`{r['name']}`** ({r['threshold']:.2f}): {r['count_at_or_above']} of "
                f"{r['n_total']} rows cross it; observed max {_fmt(r['observed_max'])} "
                f"is within 0.02 of the threshold.{note}"
            )
        lines.append("")
    lines.append("## How to reproduce")
    lines.append("")
    lines.append(
        "Re-measuring needs the original database and is not possible from this "
        "repo. The committed snapshot is the record; the verdict logic is tested "
        "without a database:"
    )
    lines.append("")
    lines.append("```bash")
    lines.append("python -m pytest tests/unit/test_threshold_reachability_helpers.py")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)
