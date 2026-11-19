"""The scheduled canary: a fixed small set of held-out cases, run live, one row of health metrics per run. Stdlib only.

pierre has no users, so there is no organic traffic to watch. The canary is scheduled traffic: the same few held-out cases
on a timer, so the input is the same in every 5-case row; a change between those rows comes from the model, the SDK,
the tools, the prompt, or from sampling alone (one run per row, k = 1).
The telemetry and the alerts built on this row are real; the traffic is not organic.

``select_cases`` is the rule that picks the cases (first N held-out ids in string order). ``build_row`` turns the trace
records of one run, and its wilco grade, into the metrics row; ``run_canary`` is the whole run behind ``make canary``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from pierre.agent import cost as cost_mod
from pierre.agent import eval_report
from pierre.agent.eval_run import Session, estimate, run_all

MODEL = "claude-sonnet-5-5"
N_CASES = 5
CAP_FACTOR = 1.5  # the same cap rule as the live eval: the estimate times 1.5
_BILLED_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


# --- which cases ---------------------------------------------------------------------------------------------------------


def select_cases(cases: Sequence[Dict[str, Any]], n: int = N_CASES) -> List[str]:
    """The rule: the held-out case ids, sorted as plain strings, first ``n``. It reads no grade and no trace."""
    return sorted(c["id"] for c in cases if c.get("split") == "heldout")[:n]


def read_case_list(path: Path) -> List[str]:
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def validate_case_list(ids: Iterable[str], cases: Dict[str, Dict[str, Any]]) -> None:
    """A canary case must exist and be held-out. The list is committed, so a change to the case file never changes it silently."""
    for cid in ids:
        if cid not in cases:
            raise ValueError(f"canary case {cid!r} is unknown: it is not in the case file")
        if cases[cid].get("split") != "heldout":
            raise ValueError(f"canary case {cid!r} is not held-out (split {cases[cid].get('split')!r})")


# --- statistics ----------------------------------------------------------------------------------------------------------


def percentile(values: Sequence[float], p: int) -> Optional[float]:
    """Nearest rank: the smallest value with at least ``p`` percent of the values at or below it. None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = -(-p * len(ordered) // 100)  # ceil(p * n / 100) in integers
    return ordered[max(rank, 1) - 1]


def _rate(k: int, n: int) -> Optional[float]:
    return round(k / n, 4) if n else None


# --- records -------------------------------------------------------------------------------------------------------------


def load_records(traces_dir: Path, case_ids: Optional[Sequence[str]] = None, repeat: int = 0) -> List[Dict[str, Any]]:
    """Every turn record under ``traces_dir`` (one model's files) for the named cases (all when None) and one repeat."""
    out: List[Dict[str, Any]] = []
    for p in sorted(Path(traces_dir).rglob("*.trace.jsonl")):
        for rec in eval_report.load_jsonl(p):
            if rec.get("repeat", 0) == repeat and (case_ids is None or rec["case_id"] in case_ids):
                out.append(rec)
    return sorted(out, key=lambda r: (r["session_id"], r["turn"]))


def _usage_in(u: Dict[str, Any]) -> int:
    return sum(int(u.get(k) or 0) for k in _BILLED_KEYS)


def turn_cost_usd(rec: Dict[str, Any]) -> Optional[float]:
    """The turn's cost in USD: ``meta.cost`` when the run wrote it (live runs since the cost table), otherwise derived from the
    usage records at the dated list prices, the auditor priced as the fixed judge model. None when any model has no price."""
    meta = rec.get("meta") or {}
    if "cost" in meta:
        return None if meta["cost"] is None else float(meta["cost"]["total_usd"])
    from pierre.agents._claim_auditor import DEFAULT_AUDIT_MODEL

    total = 0.0
    for model, usages in ((rec["model"], meta.get("usage") or []), (DEFAULT_AUDIT_MODEL, meta.get("auditor_usage") or [])):
        for u in usages:
            c = cost_mod.usage_cost(model, u)
            if c is None:
                return None
            total += c
    return total


def _causes(records: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """Terminated turns by cause (``timeout``, ``max_tokens``, ...), for the alert rules; ``{}`` when none ended that way."""
    out: Dict[str, int] = {}
    for r in records:
        cause = (r.get("termination") or {}).get("cause")
        if cause:
            out[cause] = out.get(cause, 0) + 1
    return dict(sorted(out.items()))


def _joined(values: Iterable[Optional[str]]) -> Optional[str]:
    seen = sorted({v for v in values if v})
    return ",".join(seen) if seen else None


# --- the row -------------------------------------------------------------------------------------------------------------


def pass_count(grade: Dict[str, Any], case_ids: Sequence[str], repeat: int = 0) -> Tuple[int, int]:
    """(passed, graded) over the named cases' sessions of one repeat. A named case with no graded session is an error."""
    by_case = {s["case_id"]: s for s in grade.get("sessions", []) if s.get("repeat", 0) == repeat and s["case_id"] in case_ids}
    missing = [c for c in case_ids if c not in by_case]
    if missing:
        raise ValueError(f"no graded session for {missing}")
    return sum(1 for s in by_case.values() if s["passed"]), len(by_case)


def build_row(records: Sequence[Dict[str, Any]], grade: Dict[str, Any], date: str, case_ids: Sequence[str]) -> Dict[str, Any]:
    n = len(records)
    passed, graded = pass_count(grade, case_ids)
    costs = [turn_cost_usd(r) for r in records]
    priced = None not in costs  # a turn without a price makes the figure null, never a partial one
    lat = [r["meta"]["latency_ms"]["turn"] for r in records if (r["meta"].get("latency_ms") or {}).get("turn") is not None]
    audited = flagged = repaired = 0
    for r in records:
        first, _last, n_rep = eval_report._audits(r)
        repaired += 1 if n_rep else 0
        if first is not None:
            audited += 1
            flagged += 1 if first["fabricated"] else 0
    p50 = percentile(costs, 50) if priced else None  # type: ignore[arg-type]
    p95 = percentile(costs, 95) if priced else None  # type: ignore[arg-type]
    lat95 = percentile(lat, 95) if len(lat) == n else None  # latency of every turn, or none
    return {
        "date": date,
        "pass_count": passed,
        "cases_graded": graded,
        "turns": n,
        "cost_per_turn_usd_p50": None if p50 is None else round(p50, 6),
        "cost_per_turn_usd_p95": None if p95 is None else round(p95, 6),
        "latency_p95_ms": None if lat95 is None else round(lat95, 1),
        "repair_loop_rate": _rate(repaired, n),
        "termination_rate": _rate(sum(1 for r in records if r.get("termination")), n),
        "termination_causes": _causes(records),
        "repair_cap_rate": _rate(sum(1 for r in records if (r["meta"] or {}).get("repair_cap")), n),
        "fabricated_flag_rate": _rate(flagged, audited),
        "model": _joined(r.get("model") for r in records),
        "sdk": _joined((r.get("runtime") or {}).get("sdk") for r in records),
        "case_ids": list(case_ids),
        "alerts": [],
    }


def append_row(path: Path, row: Dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")


def _dates(path: Path) -> List[str]:
    return [json.loads(x)["date"] for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()] if Path(path).exists() else []


# --- estimates ---------------------------------------------------------------------------------------------------------


def estimate_from_traces(records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """What one run of these sessions costs, counted from existing traces: calls, billed input tokens (cache included) and
    list-price USD. Billed tokens are the provider's; the budget ledger counts chars / 4 instead (see ``budget``)."""
    agent = sum(len(r["meta"].get("usage") or []) for r in records)
    aud = sum(len(r["meta"].get("auditor_usage") or []) for r in records)
    costs = [turn_cost_usd(r) for r in records]
    return {
        "sessions": len({r["session_id"] for r in records}),
        "turns": len(records),
        "agent_calls": agent,
        "auditor_calls": aud,
        "calls": agent + aud,
        "billed_input_tokens": sum(_usage_in(u) for r in records for u in (r["meta"].get("usage") or []) + (r["meta"].get("auditor_usage") or [])),
        "output_tokens": sum(int(u.get("output_tokens") or 0) for r in records for u in (r["meta"].get("usage") or []) + (r["meta"].get("auditor_usage") or [])),
        "cost_usd": None if None in costs else round(sum(costs), 4),  # type: ignore[arg-type]
    }


def budget(cases: Dict[str, Dict[str, Any]], case_ids: Sequence[str], model: str = MODEL) -> Dict[str, int]:
    """The run's call and token cap: the live eval's pre-run estimate for these sessions (k=1) times 1.5."""
    return estimate(cases, [Session(c, model, 0) for c in case_ids])


# --- the run -------------------------------------------------------------------------------------------------------------


def run_canary(
    client: Any,
    cases: Dict[str, Dict[str, Any]],
    case_ids: Sequence[str],
    out_dir: Path,
    date: str,
    grader: Callable[[str], Dict[str, Any]],
    metrics_path: Path,
    workers: int = 3,
    model: str = MODEL,
) -> Dict[str, Any]:
    """Run the cases once each (k=1) live, grade the traces, append one row. A budget abort propagates and appends nothing."""
    if date in _dates(metrics_path):
        raise ValueError(f"metrics.jsonl already has a row for {date}: one row per run date")
    traces_dir = Path(out_dir) / date
    sessions = [Session(c, model, 0) for c in case_ids]
    run_all(client, cases, sessions, traces_dir, workers=workers, live=True)
    grade = grader(eval_report.concat_traces(traces_dir, model))
    row = build_row(load_records(traces_dir / model, case_ids), grade, date, case_ids)
    append_row(metrics_path, row)
    return row
