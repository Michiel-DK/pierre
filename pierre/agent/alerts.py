"""The four canary alert rules, over the series of rows in ``canary/metrics.jsonl``. Stdlib only, no model call.

pierre has no users, so the series is a scheduled canary (5 fixed held-out cases on weekdays), not organic traffic. The
rules and their numbers live in ``canary/thresholds.json``, dated in ``canary/thresholds_added.json`` (the date of the first
row; the commit order within that day is in a private source repository):

* ``pass_drop``: the pass count is at least ``drop`` below the median of the previous 7 comparable rows, on
  ``consecutive_runs`` runs in a row (each run against its own previous 7). One bad run at n=5 is noise.
* ``cost``: the cost per turn (the row's p50) is more than ``factor`` x the median of the previous 7 comparable rows.
  Rows, not calendar days: the canary runs on weekdays only.
* ``termination``: the newest row has a turn that ended on a listed cause (``timeout``, ``max_tokens``).
* ``changed``: the model id or the SDK version differs from the previous row. Not a failure: exit code 3 = run
  ``make canary FULL=1``.

Comparable rows are those with the same ``case_ids`` as the row being judged, so a ``FULL=1`` row (21 cases) is neither
judged against 5-case rows nor part of their baseline. With fewer than ``baseline_rows`` comparable previous rows the
two median rules are not evaluated (a median of two or three runs at n=5 is noise) and a note says so; the termination
and change rules need no baseline and always run.
"""
from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

EXIT_OK, EXIT_FAIL, EXIT_CHANGED = 0, 1, 3
MIN_COSTED = 4  # of the 7 baseline rows, at least this many must carry a cost for the cost rule to run


@dataclass
class Result:
    alerts: List[str] = field(default_factory=list)  # the reasons, written into the newest row
    notes: List[str] = field(default_factory=list)  # what was not evaluated and why; printed, not stored
    failed: bool = False

    @property
    def exit_code(self) -> int:
        if self.failed:
            return EXIT_FAIL
        return EXIT_CHANGED if self.alerts else EXIT_OK


def load_thresholds(path: Path) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_rows(path: Path) -> List[Dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def _baseline(rows: Sequence[Dict[str, Any]], i: int, n: int) -> List[Dict[str, Any]]:
    """The last ``n`` rows before index ``i`` that ran the same cases as row ``i``."""
    same = [r for r in rows[:i] if r.get("case_ids") == rows[i].get("case_ids")]
    return same[-n:]


def _pass_bad(rows: Sequence[Dict[str, Any]], i: int, th: Dict[str, Any]) -> Optional[bool]:
    base = _baseline(rows, i, th["baseline_rows"])
    if len(base) < th["baseline_rows"]:
        return None
    return rows[i]["pass_count"] <= statistics.median(r["pass_count"] for r in base) - th["pass_drop"]["drop"]


def _rule_pass(rows, th, res: Result) -> None:
    need, runs = th["baseline_rows"], th["pass_drop"]["consecutive_runs"]
    last = len(rows) - 1
    have = len(_baseline(rows, last, need))
    if have < need:
        res.notes.append(f"pass_drop: not evaluated, the baseline has {have} of {need} comparable previous rows")
        return
    window = range(last - runs + 1, last + 1)
    verdicts = [_pass_bad(rows, i, th) if i >= 0 else None for i in window]
    if verdicts[-1] and any(v is None for v in verdicts):
        res.notes.append("pass_drop: the newest run is low, but the run before it has no full baseline, so two in a row cannot be judged yet")
    if all(v is True for v in verdicts):
        d = th["pass_drop"]["drop"]
        res.alerts.append(f"pass_drop: pass count {d} or more below the median of the previous {need} rows on {runs} runs in a row "
                          f"({', '.join(str(rows[i]['pass_count']) for i in window)} of {rows[last].get('cases_graded')})")
        res.failed = True


def _rule_cost(rows, th, res: Result) -> None:
    field_, factor, need = th["cost"]["field"], th["cost"]["factor"], th["baseline_rows"]
    last = len(rows) - 1
    base = _baseline(rows, last, need)
    if len(base) < need:
        res.notes.append(f"cost: not evaluated, the baseline has {len(base)} of {need} comparable previous rows")
        return
    now = rows[last].get(field_)
    costs = [r[field_] for r in base if r.get(field_) is not None]
    if now is None or len(costs) < MIN_COSTED:
        res.notes.append(f"cost: not evaluated, the newest cost is null or fewer than {MIN_COSTED} baseline rows carry one (a null means a model had no price row)")
        return
    med = statistics.median(costs)
    if now > factor * med:
        res.alerts.append(f"cost: {field_} {now:.6f} is above {factor} x the median of the previous {need} rows ({med:.6f})")
        res.failed = True


def _rule_termination(rows, th, res: Result) -> None:
    newest = rows[-1]
    causes = newest.get("termination_causes")
    if causes is None:
        if (newest.get("termination_rate") or 0) > 0:
            res.alerts.append("termination: the row counts terminated turns but records no cause (cause unknown)")
            res.failed = True
        return
    hit = {c: n for c, n in causes.items() if c in th["terminations"]["causes"] and n}
    if hit:
        res.alerts.append("termination: " + ", ".join(f"{c} x{n}" for c, n in sorted(hit.items())))
        res.failed = True


def _rule_changed(rows, th, res: Result) -> None:
    if len(rows) < 2:
        return
    prev, new = rows[-2], rows[-1]
    for f in th["change"]["fields"]:
        if prev.get(f) != new.get(f):
            res.alerts.append(f"changed: {f} {prev.get(f)} -> {new.get(f)} (not a failure: run `make canary FULL=1`)")


def check(rows: Sequence[Dict[str, Any]], th: Dict[str, Any]) -> Result:
    """Judge the newest row of ``rows`` (oldest first) against the rows before it."""
    res = Result()
    if not rows:
        return res
    _rule_pass(rows, th, res)
    _rule_cost(rows, th, res)
    _rule_termination(rows, th, res)
    _rule_changed(rows, th, res)
    return res
