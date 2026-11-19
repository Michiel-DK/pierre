"""Render the agent-eval report from the committed traces and the committed (slimmed) wilco grades.

Standard library only: no model, no key, no wilco. Wilco's verdicts are read from ``grades/*.json``; everything else
(the auditor's before/after counts, rounds, terminations, the stage table, the predictions check, tokens) is
counted here from the traces. Nothing a model said is turned into a verdict: the auditor's claim verdicts are counted
and reported as an imperfect judge, never used to pass or fail a session.
"""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

MAX_REPAIR_ROUNDS = 2  # pierre.agent.repair.MAX_REPAIR_ROUNDS; a test pins the two equal

STAGES: Dict[str, str] = {
    "tools_called": "selection", "tools_not_called": "selection", "tools_allowed": "selection", "redundancy": "selection",
    "args_match": "arguments",
    "error_recovery": "execution", "termination": "execution", "max_steps": "execution", "state_match": "execution",
    "no_injected_write": "execution",
    "answer_contains": "output handling", "answer_contains_any": "output handling", "answer_not_contains": "output handling",
    "facts_not_contradicted": "output handling", "grounded_answer": "output handling", "answer_present": "output handling",
    "scope_coverage": "output handling",
}
STAGE_ORDER = ("selection", "arguments", "execution", "output handling", "unmapped")
LOOP_STOPS = ("iteration_cap", "max_tokens", "refusal", "unexpected_pause_turn", "timeout")
# The claim auditor's calibration bound (docs/calibration/claim_auditor_calibration_report.md): 0 of 38 legitimate claims
# flagged fabricated, and the worst capture caught 17 of 19 fabricated claims.
AUDITOR_FP_CEILING = 3 / 38  # rule of three on 0 of 38: about 7.9%
AUDITOR_RECALL_POINT = 19 / 19
AUDITOR_RECALL_WORST = 17 / 19


def wilson(k: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def fmt_ci(k: int, n: int) -> str:
    if n == 0:
        return "0 of 0"
    lo, hi = wilson(k, n)
    return f"{k} of {n} ({100 * lo:.0f}% to {100 * hi:.0f}%)"


def _pct(k: int, n: int) -> str:
    return f"{100 * k / n:.0f}%" if n else "n/a"


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def load_traces(traces_dir: Path) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """model -> session id -> records in turn order."""
    out: Dict[str, Dict[str, List[Dict[str, Any]]]] = defaultdict(dict)
    for model_dir in sorted(Path(traces_dir).iterdir()):
        if not model_dir.is_dir():
            continue
        for p in sorted(model_dir.glob("*.trace.jsonl")):
            recs = sorted(load_jsonl(p), key=lambda r: r["turn"])
            out[model_dir.name][recs[0]["session_id"]] = recs
    return out


def concat_traces(traces_dir: Path, model: str) -> str:
    """The exact text handed to ``wilco grade --traces``: the model's sessions in file-name order."""
    parts = [p.read_text(encoding="utf-8") for p in sorted((Path(traces_dir) / model).glob("*.trace.jsonl"))]
    return "".join(parts)


def draft_trace(traces_dir: Path, model: str) -> str:
    """The shadow trace: each turn's FIRST draft in place of its final answer, with everything else unchanged.

    Approximation, stated in the report: the tool calls and results of the repair rounds stay in the record (the trace does
    not say which round made which call), and ``steps`` is the first round's. A turn with no repair round is identical."""
    lines: List[str] = []
    for p in sorted((Path(traces_dir) / model).glob("*.trace.jsonl")):
        for rec in load_jsonl(p):
            rounds = (rec.get("meta") or {}).get("rounds") or []
            if len(rounds) > 1:
                rec = json.loads(json.dumps(rec))
                rec["output_parsed"] = {"answer": rounds[0]["answer"]}
                rec["steps"] = max(int(rounds[0].get("steps") or 1), 1)
                rec["meta"].pop("repair_cap", None)
                rec["termination"] = None
            lines.append(json.dumps(rec, sort_keys=True, ensure_ascii=False) + "\n")
    return "".join(lines)


def slim_grade(raw: Dict[str, Any], traces_text: str, label: str) -> Dict[str, Any]:
    """Wilco's result without the tool results it echoes, without machine paths, and with the sha of the file it graded."""
    import hashlib

    prov = raw.get("provenance") or {}
    sessions = []
    for s in raw["sessions"]:
        sessions.append({
            "session_id": s["session_id"], "case_id": s["case_id"], "split": s["split"], "repeat": s["repeat"],
            "passed": s["passed"], "first_failed_turn": s.get("first_failed_turn"), "root_check": s.get("root_check"),
            "failed_checks": s.get("failed_checks", []), "session_failed_checks": s.get("session_failed_checks", []),
            "session_failures": s.get("session_failures", []),
            "turns": [{"turn": t["turn"], "passed": t["passed"], "steps": t.get("steps"), "failures": t.get("failures", []),
                       "failed_checks": t.get("failed_checks", [])} for t in s["turns"]],
        })
    keep = ("agent", "provider", "model", "mode", "k", "train", "heldout", "pass_k", "pass_at_1", "flaky", "per_turn",
            "span_hours", "cases_in_exam", "cases_not_graded", "not_verified")
    out = {k: raw[k] for k in keep if k in raw}
    out["label"] = label
    out["provenance"] = {
        "cases_sha256": (prov.get("exam") or {}).get("cases_sha256"),
        "traces_sha256": hashlib.sha256(traces_text.encode("utf-8")).hexdigest(),
        "wilco_traces_sha256": (prov.get("traces") or {}).get("sha256"),
        "grader_hash": prov.get("grader_hash"),
    }
    out["sessions"] = sessions
    return out


# ---------------------------------------------------------------------------------------------------------------------
# counting from the traces
# ---------------------------------------------------------------------------------------------------------------------


def _audits(rec: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], int]:
    """(first-round audit, last audited round's audit, number of repair rounds) of one turn record. An audit with an
    ``error`` counts as no audit."""
    rounds = (rec.get("meta") or {}).get("rounds") or []
    ok = [r.get("audit") for r in rounds]
    ok = [a if a and not a.get("error") else None for a in ok]
    first = ok[0] if ok else None
    last = next((a for a in reversed(ok) if a), None)
    return first, last, max(len(rounds) - 1, 0)


def turn_stats(records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    s: Dict[str, Any] = Counter()
    s["turns"] = len(records)
    for rec in records:
        first, last, n_rep = _audits(rec)
        rounds = (rec.get("meta") or {}).get("rounds") or []
        if any((r.get("audit") or {}).get("error") for r in rounds):
            s["audit_error_turns"] += 1
        s["repair_rounds_%d" % n_rep] += 1
        if n_rep:
            s["turns_repaired"] += 1
        if (rec.get("meta") or {}).get("repair_cap"):
            s["repair_cap_turns"] += 1
        cause = (rec.get("termination") or {}).get("cause")
        s["term:" + (cause or "end_turn")] += 1
        if first and last:
            s["audited"] += 1
            for tag, a in (("d", first), ("f", last)):
                s[tag + "_claims"] += len(a["claims"])
                s[tag + "_fab"] += a["fabricated"]
                s[tag + "_unv"] += a["unverifiable"]
                s[tag + "_ver"] += a["verified"]
                s[tag + "_turns_fab"] += 1 if a["fabricated"] else 0
                s[tag + "_turns_unsup"] += 1 if (a["fabricated"] or a["unverifiable"]) else 0
            # The known false positive of the audit context before 19 Nov 2026: a first-draft claim flagged `fabricated`
            # that names a firm the turn's lookup_firm listed under other_matches, a key the auditor did not receive.
            others = {n for c, r in zip(rec.get("tool_calls") or [], rec.get("tool_results") or [])
                      if c.get("tool") == "lookup_firm" and isinstance(r, dict) for n in r.get("other_matches") or [] if n}
            fab = [c for c in first["claims"] if c.get("verdict") == "fabricated"]
            known = [c for c in fab if any(n in (c.get("claim_text") or "") for n in others)]
            s["d_fab_other_matches"] += len(known)
            if fab and len(known) == len(fab):
                s["d_turns_fab_other_matches"] += 1
                s["turns_repaired_other_matches"] += 1 if n_rep else 0
            if first["fabricated"]:
                s["fab_turns_d"] += 1
                s["fab_cleared"] += 0 if last["fabricated"] else 1
            if n_rep and first["verified"] > last["verified"]:
                s["verified_lost"] += 1
        for call_usage, role in ((m, "agent") for m in (rec.get("meta") or {}).get("usage") or []):
            s["agent_in"] += sum(int(call_usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
            s["agent_out"] += int(call_usage.get("output_tokens") or 0)
            s["agent_calls"] += 1
        for u in (rec.get("meta") or {}).get("auditor_usage") or []:
            s["aud_in"] += int(u.get("input_tokens") or 0)
            s["aud_out"] += int(u.get("output_tokens") or 0)
            s["aud_calls"] += 1
    return dict(s)


def stage_of(check: str) -> str:
    return STAGES.get(check, "unmapped")


def failed_session_table(grade: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for s in grade["sessions"]:
        if s["passed"]:
            continue
        checks = sorted({c["check"] for c in s["failed_checks"]} | {c["check"] for c in s["session_failed_checks"]})
        rows.append({"session": s["session_id"], "case": s["case_id"], "split": s["split"], "repeat": s["repeat"],
                     "turn": s["first_failed_turn"], "root": s["root_check"], "checks": checks})
    return rows


# ---------------------------------------------------------------------------------------------------------------------
# predictions (the numbers and ranges are the ones in agent_eval_predictions.md; a test pins the ids and ranges equal)
# ---------------------------------------------------------------------------------------------------------------------

Pred = Tuple[str, str, Optional[Tuple[float, float]], str]  # id, text, (lo, hi) or None, predicted point as written


def _passed(grade: Dict[str, Any], key: str, split: str) -> int:
    return int(grade[key][split]["passed"])


def evaluate_predictions(ctx: Dict[str, Any]) -> List[Dict[str, str]]:
    son, opu = ctx["grades"].get("claude-sonnet-5-5"), ctx["grades"].get("claude-opus-5-5")
    st_all = ctx["stats_all"]
    rows: List[Dict[str, str]] = []

    def numeric(pid: str, text: str, pred: str, lo: float, hi: float, value: Optional[float], shown: str) -> None:
        if value is None:
            rows.append({"id": pid, "what": text, "pred": pred, "obs": "n/a", "verdict": "not measurable"})
            return
        rows.append({"id": pid, "what": text, "pred": pred, "obs": shown,
                     "verdict": "inside range" if lo <= value <= hi else "refuted (outside range)"})

    def boolean(pid: str, text: str, pred: str, value: Optional[bool], shown: str) -> None:
        verdict = "not measurable" if value is None else ("held" if value else "refuted")
        rows.append({"id": pid, "what": text, "pred": pred, "obs": shown, "verdict": verdict})

    if son:
        numeric("P1", "Sonnet train pass@1", "8 of 11 (6 to 10)", 6, 10, _passed(son, "pass_at_1", "train"), f"{_passed(son, 'pass_at_1', 'train')} of {son['pass_at_1']['train']['total']}")
        numeric("P2", "Sonnet train pass^3", "6 of 11 (4 to 9)", 4, 9, _passed(son, "pass_k", "train"), f"{_passed(son, 'pass_k', 'train')} of {son['pass_k']['train']['total']}")
        numeric("P3", "Sonnet held-out pass@1", "6 of 10 (4 to 8)", 4, 8, _passed(son, "pass_at_1", "heldout"), f"{_passed(son, 'pass_at_1', 'heldout')} of {son['pass_at_1']['heldout']['total']}")
        numeric("P4", "Sonnet held-out pass^3", "4 of 10 (2 to 7)", 2, 7, _passed(son, "pass_k", "heldout"), f"{_passed(son, 'pass_k', 'heldout')} of {son['pass_k']['heldout']['total']}")
        gap = [_passed(son, "pass_k", sp) < _passed(son, "pass_at_1", sp) for sp in ("train", "heldout")]
        boolean("P9", "Sonnet pass^3 below pass@1 on at least one split", "yes", any(gap), f"train {gap[0]}, held-out {gap[1]}")
    if opu:
        numeric("P5", "Opus train pass@1", "9 of 11 (7 to 10)", 7, 10, _passed(opu, "pass_at_1", "train"), f"{_passed(opu, 'pass_at_1', 'train')} of {opu['pass_at_1']['train']['total']}")
        numeric("P6", "Opus held-out pass@1", "7 of 10 (5 to 9)", 5, 9, _passed(opu, "pass_at_1", "heldout"), f"{_passed(opu, 'pass_at_1', 'heldout')} of {opu['pass_at_1']['heldout']['total']}")
    if son and opu:
        lower = all(g["pass_at_1"]["heldout"]["score"] < g["pass_at_1"]["train"]["score"] for g in (son, opu))
        boolean("P7", "held-out pass@1 score below train for both models", "yes", lower,
                "; ".join(f"{m} {g['pass_at_1']['train']['score']:.2f} train, {g['pass_at_1']['heldout']['score']:.2f} held-out" for m, g in (("Sonnet", son), ("Opus", opu))))
        overlap = []
        for sp in ("train", "heldout"):
            a, b = wilson(_passed(son, "pass_at_1", sp), son["pass_at_1"][sp]["total"]), wilson(_passed(opu, "pass_at_1", sp), opu["pass_at_1"][sp]["total"])
            overlap.append(a[0] <= b[1] and b[0] <= a[1])
        boolean("P8", "Opus and Sonnet pass@1 intervals overlap on both splits", "yes", all(overlap), f"train overlap {overlap[0]}, held-out overlap {overlap[1]}")
    n = st_all.get("turns", 0)
    if n:
        numeric("P10", "share of turns with at least one repair round", "15% (5% to 30%)", 0.05, 0.30, st_all.get("turns_repaired", 0) / n, f"{fmt_ci(st_all.get('turns_repaired', 0), n)}")
        numeric("P11", "turns that end at the repair cap", "3 (0 to 8)", 0, 8, st_all.get("repair_cap_turns", 0), str(st_all.get("repair_cap_turns", 0)))
    a = st_all.get("audited", 0)
    if a:
        numeric("P12", "share of audited turns with fabricated claims in the first draft", "12% (4% to 25%)", 0.04, 0.25, st_all["d_turns_fab"] / a, fmt_ci(st_all["d_turns_fab"], a))
        numeric("P13", "share of audited turns with fabricated claims in the final answer", "3% (0% to 8%)", 0.0, 0.08, st_all["f_turns_fab"] / a, fmt_ci(st_all["f_turns_fab"], a))
        diff = (st_all["f_unv"] - st_all["d_unv"]) / a
        boolean("P14", "unverifiable claims per audited turn, first draft vs final: no change (operationalised here as an absolute difference under 0.25 per turn; the predictions file named no number)",
                "no change", abs(diff) < 0.25, f"draft {st_all['d_unv'] / a:.2f}, final {st_all['f_unv'] / a:.2f}, difference {diff:+.2f}")
    if ctx.get("damage") is not None:
        numeric("P15", "repair damage: turns passing on the first draft and failing on the final answer", "1 (0 to 3)", 0, 3, ctx["damage"]["lost"], str(ctx["damage"]["lost"]))
    sess_stops = ctx["sessions_with_loop_stop"]
    numeric("P16", "sessions ending on iteration_cap, max_tokens or refusal", "2 of 84 (0 to 6)", 0, 6, sess_stops, f"{sess_stops} of {ctx['n_sessions']}")
    stages = ctx["root_stage_counts"]
    if stages:
        top = max(stages.items(), key=lambda kv: kv[1])
        tied = [k for k, v in stages.items() if v == top[1]]
        boolean("P17", "most common stage of a failed session is output handling", "output handling", tied == ["output handling"], ", ".join(f"{k} {v}" for k, v in sorted(stages.items(), key=lambda kv: -kv[1])))
    rcd = ctx["recent_climate_args_fail"]
    boolean("P18", "`recent-climate-deals` fails `args_match` on at least one model", "yes", rcd > 0, f"{rcd} session(s)")
    if ctx.get("billed_ratio"):
        r = ctx["billed_ratio"]
        numeric("P19", "billed input tokens over the estimate (metered)", "1.3x to 1.8x", 1.3, 1.8, r, f"{r:.2f}x")
    return rows


# ---------------------------------------------------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------------------------------------------------

SHORT = {"claude-sonnet-5-5": "Sonnet 5.5", "claude-opus-5-5": "Opus 5.5"}


def _split_line(grade: Dict[str, Any], key: str, split: str) -> str:
    d = grade[key][split]
    return fmt_ci(d["passed"], d["total"])


# The hand-read analysis below was written for this one run (run_summary.json runs[-1].at). On any other run it is not
# printed, so a re-run cannot carry last run's commentary.
HAND_READ_RUN_AT = "2026-11-13T12:11:46.541142+00:00"


def render(data_dir: Path, cases_path: Path, wilco_sha: str) -> str:
    data_dir = Path(data_dir)
    traces = load_traces(data_dir / "traces")
    cases = {c["id"]: c for c in json.loads(Path(cases_path).read_text())["cases"]}
    summary = json.loads((data_dir / "run_summary.json").read_text())
    grades: Dict[str, Dict[str, Any]] = {}
    drafts: Dict[str, Dict[str, Any]] = {}
    for model in traces:
        g, d = data_dir / "grades" / f"{model}.json", data_dir / "grades" / f"{model}.draft.json"
        if g.exists():
            grades[model] = json.loads(g.read_text())
        if d.exists():
            drafts[model] = json.loads(d.read_text())
    models = [m for m in ("claude-sonnet-5-5", "claude-opus-5-5") if m in traces] + sorted(m for m in traces if m not in SHORT)
    stats = {m: turn_stats([r for recs in traces[m].values() for r in recs]) for m in models}
    stats_all: Dict[str, Any] = Counter()
    for m in models:
        stats_all.update(stats[m])
    stats_all = dict(stats_all)

    # repair damage / gain from the shadow grade
    damage = None
    if drafts and set(drafts) == set(grades):
        lost = gained = same = 0
        rows = []
        for m in models:
            fin = {(s["session_id"], t["turn"]): t["passed"] for s in grades[m]["sessions"] for t in s["turns"]}
            dra = {(s["session_id"], t["turn"]): t["passed"] for s in drafts[m]["sessions"] for t in s["turns"]}
            for key, f in fin.items():
                d = dra.get(key)
                if d is None or d == f:
                    same += 1
                elif d and not f:
                    lost += 1
                    rows.append((m, key[0], key[1], "draft passed, final failed"))
                else:
                    gained += 1
                    rows.append((m, key[0], key[1], "draft failed, final passed"))
        damage = {"lost": lost, "gained": gained, "same": same, "rows": rows}

    failed = {m: failed_session_table(grades[m]) for m in grades}
    root_stage = Counter(stage_of(r["root"]) for m in failed for r in failed[m] if r["root"])
    all_check_stage: Dict[str, Counter] = {m: Counter() for m in grades}
    for m in grades:
        for s in grades[m]["sessions"]:
            for c in {(c["check"]) for t in s["turns"] for c in t["failed_checks"]} | {c["check"] for c in s["session_failed_checks"]}:
                all_check_stage[m][c] += 1
    n_sessions = sum(len(v) for v in traces.values())
    loop_stop_sessions = sum(1 for m in traces for recs in traces[m].values() if any(((r.get("termination") or {}).get("cause") in LOOP_STOPS) for r in recs))
    rc = sum(1 for m in failed for r in failed[m] if r["case"] == "recent-climate-deals" and "args_match" in r["checks"])
    ledger = summary.get("metered_ledger") or {}
    ratio = (ledger["billed_input_tokens"] / ledger["est_input_tokens"]) if ledger.get("est_input_tokens") else None
    ctx = {"grades": grades, "stats_all": stats_all, "damage": damage, "sessions_with_loop_stop": loop_stop_sessions, "n_sessions": n_sessions,
           "root_stage_counts": dict(root_stage), "recent_climate_args_fail": rc, "billed_ratio": ratio}

    L: List[str] = []
    w = L.append
    w("# Agent eval: the pierre firm agent, live, graded by wilco")
    w("")
    w("> **Auto-generated** by `scripts/analysis/agent_eval.py render-md` (run by `make reports`) from the committed traces in `data/agent_eval/traces/`,")
    w("> the committed wilco grades in `data/agent_eval/grades/` and `data/agent_eval/run_summary.json`. No model is called. Do not hand-edit; the one hand-written section is in the generator and prints only for the run it describes.")
    w("> Predictions were committed before the run (that order is in a private source repository; the predictions file says where): [agent_eval_predictions.md](agent_eval_predictions.md).")
    w("")
    w("## What ran")
    w("")
    est = summary.get("estimate") or {}
    w(f"- Cases: {len(cases)} ({sum(1 for c in cases.values() if c['split'] == 'train')} train, {sum(1 for c in cases.values() if c['split'] == 'heldout')} held-out), "
      f"`evals/pierre-firm-agent/cases.json` (review status `{json.loads(Path(cases_path).read_text())['review']['status']}`).")
    w("- Models: " + "; ".join(f"`{m}` at k={max((r[0]['repeat'] for r in traces[m].values()), default=0) + 1}" for m in models)
      + f". {n_sessions} sessions, {stats_all['turns']} turns. The auditor is `claude-opus-5-5` for both agent models. Opus at k=1 means its pass^k equals its pass@1.")
    w(f"- Live, on the fictional in-memory firm store. Agent effort `medium`, repair cap {MAX_REPAIR_ROUNDS} rounds, tool-iteration cap 8 per draft.")
    w(f"- Graded by `wilco grade --traces` at wilco commit `{wilco_sha}` (public, pinned in `pyproject.toml`). The wilco checks decide pass/fail; nothing a model said is a verdict.")
    w("- Wilson 95% intervals, on cases (the k runs of one case are not independent). Train and held-out are never summed. The intervals are wide: at n = 10 or 11 a gap under about 4 cases cannot show.")
    w("- One deviation from the pre-run plan: a turn that ends at the repair cap carries `meta.repair_cap` and is **not** a `termination` in the trace, because wilco force-fails any turn with a termination and the auditor (a model) would then have decided a verdict. Those turns are graded by the deterministic checks like any other.")
    w("")

    w("## Scores (wilco, deterministic checks)")
    w("")
    w("| model | split | pass@1 (cases) | pass^k (cases passing every run) | per-turn pass rate (turn 0) |")
    w("|---|---|---|---|---|")
    for m in models:
        g = grades.get(m)
        if not g:
            continue
        for sp in ("train", "heldout"):
            pt = g["per_turn"].get(sp, {}).get("0", {})
            w(f"| {SHORT.get(m, m)} (k={g['k']}) | {sp} | {_split_line(g, 'pass_at_1', sp)} | {_split_line(g, 'pass_k', sp)} | {fmt_ci(pt.get('passed', 0), pt.get('total', 0))} |")
    w("")
    w("Count of cases passed, then the Wilson 95% interval. `pass@1` is the mean session verdict times n, as a case count; `pass^k` counts a case only if all k sessions passed.")
    w("")
    flaky = {m: g.get("flaky") for m, g in grades.items() if g.get("flaky")}
    if flaky:
        w("Cases that passed in some runs and failed in others (wilco `flaky`): " + "; ".join(f"{SHORT.get(m, m)} train {f['train']}, held-out {f['heldout']}" for m, f in flaky.items()) + ".")
        w("")

    w("### Per case")
    w("")
    hdr = "| case | split | " + " | ".join(f"{SHORT.get(m, m)} r{r}" for m in grades for r in range(grades[m]["k"])) + " |"
    w(hdr)
    w("|---|---|" + "---|" * sum(g["k"] for g in grades.values()))
    for cid, c in cases.items():
        cells = []
        for m in grades:
            by = {s["repeat"]: s for s in grades[m]["sessions"] if s["case_id"] == cid}
            for r in range(grades[m]["k"]):
                s = by.get(r)
                cells.append("n/a" if s is None else ("pass" if s["passed"] else f"FAIL {s['root_check']}"))
        w(f"| `{cid}` | {c['split']} | " + " | ".join(cells) + " |")
    w("")

    w("## Predictions vs observed")
    w("")
    w("A prediction outside its written range is refuted. The file with the predictions is not edited by this report.")
    w("")
    w("| # | quantity | predicted | observed | verdict |")
    w("|---|---|---|---|---|")
    for r in evaluate_predictions(ctx):
        w(f"| {r['id']} | {r['what']} | {r['pred']} | {r['obs']} | {r['verdict']} |")
    w("")
    if stats_all.get("d_turns_fab_other_matches"):
        k, n_fab = stats_all["d_turns_fab_other_matches"], stats_all.get("d_turns_fab", 0)
        w(f"Note, 19 Nov 2026: P10 and P12 keep their scores as measured, but {k} of their {n_fab} turns are a known false positive of the audit context. "
          "In those turns the first draft's only `fabricated` claim names a firm that `lookup_firm` listed under `other_matches` "
          "(for example \"The name also matches another firm, Tessaly Growth.\"), and the audit context of this run did not pass `other_matches` to the auditor, "
          "so the true claim had no evidence. The repair round of each of those turns repaired a draft whose only flagged claim was true. The audit context now passes it; "
          "this run was not repeated. See the corrected rate below.")
        w("")

    w("## The fact-checker and the repair loop (pierre's own numbers)")
    w("")
    w("The auditor is a model and an imperfect judge. Its calibration on the 57-claim set: 0 of 38 legitimate claims flagged `fabricated` (a true rate up to about 8% is not excluded), and the worst of two captures caught 17 of 19 fabricated claims (a true recall down to about 69%, Wilson 95%). On agent-built contexts only a 12-claim spot check exists, which bounds nothing. Its verdicts below are counts, never pass/fail.")
    w("")
    w("### Unsupported claims, before vs after repair")
    w("")
    w("Turns where the auditor ran and did not error. 'Unsupported' = `fabricated` or `unverifiable`. Only `fabricated` claims trigger a repair; `unverifiable` ones are reported and left alone.")
    w("")
    w("| model | audited turns | turns with `fabricated`: draft | final | turns with unsupported: draft | final | claims `fabricated`: draft | final | claims `unverifiable`: draft | final |")
    w("|---|---|---|---|---|---|---|---|---|---|")
    for m in models + ["all"]:
        s = stats_all if m == "all" else stats[m]
        a = s.get("audited", 0)
        w(f"| {'all' if m == 'all' else SHORT.get(m, m)} | {a} | {fmt_ci(s.get('d_turns_fab', 0), a)} | {fmt_ci(s.get('f_turns_fab', 0), a)} | {fmt_ci(s.get('d_turns_unsup', 0), a)} | {fmt_ci(s.get('f_turns_unsup', 0), a)} | "
          f"{s.get('d_fab', 0)} of {s.get('d_claims', 0)} | {s.get('f_fab', 0)} of {s.get('f_claims', 0)} | {s.get('d_unv', 0)} | {s.get('f_unv', 0)} |")
    w("")
    errs = stats_all.get("audit_error_turns", 0)
    w(f"Turns with an auditor error (counted as not audited): {errs}. Turns with no tool call yet are not audited by design.")
    w("")
    w("### Rounds against the cap, and termination")
    w("")
    w("| model | turns | 0 repair rounds | 1 | 2 (the cap) | turns ending at the cap (`meta.repair_cap`) | draft had `fabricated` and the final does not | verified claims lost in a repaired turn |")
    w("|---|---|---|---|---|---|---|---|")
    for m in models + ["all"]:
        s = stats_all if m == "all" else stats[m]
        w(f"| {'all' if m == 'all' else SHORT.get(m, m)} | {s['turns']} | {s.get('repair_rounds_0', 0)} | {s.get('repair_rounds_1', 0)} | {s.get('repair_rounds_2', 0)} | {s.get('repair_cap_turns', 0)} | "
          f"{s.get('fab_cleared', 0)} of {s.get('fab_turns_d', 0)} | {s.get('verified_lost', 0)} |")
    w("")
    terms = sorted({k for s in stats.values() for k in s if k.startswith("term:")})
    w("| model | " + " | ".join(t[5:] for t in terms) + " |")
    w("|---|" + "---|" * len(terms))
    for m in models:
        w(f"| {SHORT.get(m, m)} | " + " | ".join(str(stats[m].get(t, 0)) for t in terms) + " |")
    w("")
    w("Turn terminations from the trace (`end_turn` = the model ended its turn). `crash:*` is an exception in the session, recorded as a result.")
    w("")
    w("### Repair damage")
    w("")
    if damage is None:
        w("Not measured: the shadow grade of the first drafts is missing.")
    else:
        w("Each turn's first draft was graded by wilco in place of its final answer (same checks, same tool calls; the tool calls made in repair rounds stay in the record, because the trace does not say which round made them, so this can only flatter the draft on tool checks). Compared turn by turn:")
        w("")
        w(f"- draft passed, final failed (**repair damage**): **{damage['lost']}** turn(s)")
        w(f"- draft failed, final passed (repair gain): **{damage['gained']}** turn(s)")
        w(f"- same verdict: {damage['same']} turn(s) (turns without a repair round are identical by construction)")
        for row in damage["rows"]:
            w(f"  - {SHORT.get(row[0], row[0])} `{row[1]}` turn {row[2]}: {row[3]}")
    w("")
    w("### The auditor's corrected rate of fabricated claims")
    w("")
    d_claims, d_fab = stats_all.get("d_claims", 0), stats_all.get("d_fab", 0)
    if d_claims:
        obs = d_fab / d_claims
        point = obs / AUDITOR_RECALL_POINT
        hi = obs / AUDITOR_RECALL_WORST
        lo = max(0.0, (obs - AUDITOR_FP_CEILING) / AUDITOR_RECALL_POINT)
        w(f"The share of first-draft claims the auditor called `fabricated`: **{d_fab} of {d_claims} ({100 * obs:.1f}%)**, Wilson 95% {100 * wilson(d_fab, d_claims)[0]:.1f}% to {100 * wilson(d_fab, d_claims)[1]:.1f}% (claims within one answer are not independent, so read that as narrower than it is).")
        w("")
        w("Correcting for the auditor's own errors with the 57-claim calibration (observed = true x recall + (1 - true) x false-positive rate):")
        known = stats_all.get("d_fab_other_matches", 0)
        w(f"- point (recall 19 of 19, false positives 0 of 38): true rate about **{100 * point:.1f}%**" + (" if every flag were real; see the note below" if known else ""))
        w(f"- upper (the worst capture's recall 17 of 19): up to about **{100 * hi:.1f}%**")
        w(f"- lower (false positives at the rule-of-three ceiling of {100 * AUDITOR_FP_CEILING:.1f}%): down to about **{100 * lo:.1f}%**")
        w("")
        w("This is a bound built from a 57-claim, author-labelled set on a different kind of context (single-firm report contexts, not tool-result contexts). It is an imperfect judge's rate with its calibration slack, not a measurement of how often the agent invents facts.")
        if known:
            w("")
            w(f"Note, 19 Nov 2026: {known} of the {d_fab} flagged claims are a known false positive of the audit context (`other_matches` of `lookup_firm` did not reach the auditor; see the note under the predictions table). "
              f"The calibration above did not cover that cause, so the corrections read all {d_fab} flags as real, and about {100 * point:.1f}% is an upper bound, not a true rate. "
              f"Without them, {d_fab - known} of {d_claims} first-draft claims stays flagged, so at most {d_fab - known} of {d_claims} is a fabrication the auditor caught.")
    else:
        w("No audited claims, so no rate.")
    w("")

    w("## Failures: first failing turn, and stage")
    w("")
    w("Stage mapping, fixed before the run (see the predictions file): selection = `tools_called`, `tools_not_called`, `tools_allowed`, `redundancy`; arguments = `args_match`; execution = `error_recovery`, `termination`, `max_steps`, `state_match`, `no_injected_write`; output handling = `answer_*`, `facts_not_contradicted`, `grounded_answer`, `answer_present`, `scope_coverage`. A check not in the list would show as `unmapped`.")
    w("")
    w("### Stage x failure (failed sessions filed under wilco's root check)")
    w("")
    w("| stage | " + " | ".join(f"{SHORT.get(m, m)} failed sessions" for m in grades) + " | checks that failed in them (any turn, each counted once per session) |")
    w("|---|" + "---|" * len(grades) + "---|")
    for stg in STAGE_ORDER:
        counts = [sum(1 for r in failed[m] if r["root"] and stage_of(r["root"]) == stg) for m in grades]
        checks = Counter()
        for m in grades:
            for c, v in all_check_stage[m].items():
                if stage_of(c) == stg:
                    checks[c] += v
        if sum(counts) == 0 and not checks and stg == "unmapped":
            continue
        w(f"| {stg} | " + " | ".join(str(c) for c in counts) + " | " + (", ".join(f"`{c}` {v}" for c, v in sorted(checks.items())) or "none") + " |")
    w("")
    w("### First failing turn per failed session")
    w("")
    w("| model | case | split | run | first failing turn | root check | stage | every check that failed |")
    w("|---|---|---|---|---|---|---|---|")
    for m in grades:
        for r in failed[m]:
            w(f"| {SHORT.get(m, m)} | `{r['case']}` | {r['split']} | r{r['repeat']} | {r['turn']} | `{r['root']}` | {stage_of(r['root'])} | {', '.join('`%s`' % c for c in r['checks'])} |")
    if not any(failed.values()):
        w("| no failed session | | | | | | | |")
    w("")

    run_at = ((summary.get("runs") or [{}])[-1]).get("at")
    w("### Reading the `grounded_answer` failures (hand-written, for the run of " + HAND_READ_RUN_AT[:19] + "Z only)")
    if run_at != HAND_READ_RUN_AT:
        w("")
        w(f"Not redone for this run ({run_at}): the hand-read analysis applies to the run of {HAND_READ_RUN_AT[:19]}Z only. Read the failure table above.")
        w("")
    else:
        w("")
        w("`grounded_answer` is the biggest single failure source, so the answers behind its distinct failure strings were read. The check looks for numbers, dates and names that no tool result contains in a recognised format. Of the 8 distinct failure strings (`grounded_answer` and the `error_recovery` one) in the Sonnet and Opus grades:")
        w("")
        w("- Correct restatements the check could not match: `3.0`, `0.5` (cheques written as EUR 500K to 3.0M for a stored 500000 to 3000000), `1.5` (stored 1500000 written as 1.5M), `2026-01-01` (the start of the six-month window, computed from the demo date `2026-06-30`), and `2021`, `2025` (the years missing from a list of portfolio years). These are format and derivation misses of a strict check, not invented facts.")
        w("- A real slip: the Sonnet answer for `recent-deals-empty-result` writes the window as `2026-12-30 to 2026-06-30`; the start date is wrong.")
        w("- A check false positive: `tool-failure-timeout` flags the sentence-initial word `Because` as an invented name.")
        w("")
        w("So the Sonnet pass rate is lower than its factual accuracy on these cases, and the gap is the check's strictness. No case was edited for this. It is a finding about `grounded_answer` that I have not acted on yet: I have not decided whether the check should accept restatements such as 1.5M for 1500000. It is not a reason to change a score.")
        w("")
    w("## Tokens and calls (metered)")
    w("")
    w("Tokens and call counts only.")
    w("")
    if est:
        w(f"- Estimate before the run: {est['calls']} calls and {est['est_input_tokens']:,} estimated input tokens (chars / 4); cap = estimate x 1.5 = {est['cap_calls']} calls / {est['cap_input_tokens']:,} tokens.")
    if ledger:
        w(f"- Metered (the budget ledger, every attempt counted): **{ledger['calls']} calls**, {ledger['est_input_tokens']:,} estimated input tokens, **{ledger['billed_input_tokens']:,} billed input tokens**, {ledger['output_tokens']:,} output tokens. Billed over estimate: {ratio:.2f}x.")
    w("- From the traces (calls that returned, by role):")
    w("")
    w("| model | agent calls | agent input | agent output | auditor calls | auditor input | auditor output |")
    w("|---|---|---|---|---|---|---|")
    for m in models:
        s = stats[m]
        w(f"| {SHORT.get(m, m)} | {s.get('agent_calls', 0)} | {s.get('agent_in', 0):,} | {s.get('agent_out', 0):,} | {s.get('aud_calls', 0)} | {s.get('aud_in', 0):,} | {s.get('aud_out', 0):,} |")
    w("")
    w("## What this does not show")
    w("")
    for line in (grades[models[0]]["not_verified"] if models and models[0] in grades else []):
        w(f"- (wilco, from the case file) {line}")
    w("- One of those lines (the one about the same model family writing the cases) ends: read the scores as a self-test until the operator review is filled in. The review **is** filled in (`REVIEWED`, 13 Nov 2026), so that sentence in `cases.json` is stale. I left it, so that the cases stay exactly as reviewed and graded; whether to update it is still open. (Only the file's `reviewed_by` line was reworded after the run, on 13 Nov 2026; that moved its hash, and the traces were re-graded with no score changing. No case changed.)")
    w("- `recent-climate-deals` wants `sector: climate` in the call; an agent that fetches the whole window and filters it itself gets a correct answer and still fails `args_match`. That is a strictness choice in the case, not the agent's error; I have not yet decided whether to relax it.")
    w("- Opus ran at k=1, so nothing here says how stable Opus is. Sonnet at k=3 says it for Sonnet on 21 cases only.")
    w("- Every case was written by the same model family that sat the exam; the operator read them before the run.")
    w("- The auditor is `claude-opus-5-5` judging both models, so Opus answers are judged by their own family.")
    w("- Fictional data, 30 firms: a pass says nothing about real firm data, and nothing here is a real-world match outcome.")
    w("")
    return "\n".join(L) + "\n"
