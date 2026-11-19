"""The public anchor: AgentDojo's whole banking suite (160 sessions) through the showcase loop on claude-sonnet-5-5.

    python scripts/analysis/public_anchor_agentdojo.py --preflight-only   # sizes and the estimate; no key, no call
    python scripts/analysis/public_anchor_agentdojo.py --env-file PATH    # the committed run: prints the estimate, then refuses (never run again)
    python scripts/analysis/public_anchor_agentdojo.py --render-md        # re-render the report from committed results
    python scripts/analysis/public_anchor_agentdojo.py --dry-run          # per (model, arm): sessions, calls, tokens; no call
    python scripts/analysis/public_anchor_agentdojo.py --model gemma4:e2b --arm spotlight --env-file PATH   # one defence arm, live

Defence arms (``--arm``, see pierre/anchor/defences.py): ``none`` is the loop as shipped, ``confirm-on-mutate`` holds a
state-changing call whose recipient / password / address came from tool output, ``spotlight`` marks tool output as data.
``--model`` is ``claude-sonnet-5-5`` (Anthropic API) or ``gemma4:e2b`` (local, Ollama). Each (model, arm) pair has its own
sessions file, ledger and traces directory under evals/public-anchor/arms/; (claude-sonnet-5-5, none) is the committed
run above, its files are never written again, and a live run of it is refused. The claim auditor stays on its fixed
judge model on the Anthropic API for every pair, so even a local-model pair spends API tokens on its audits.

The whole banking suite was run (16 user tasks alone, then each with all 9 injection tasks, numeric id order), resuming from the
committed sessions file: a session already there was never run again and its row was never rewritten. That run is complete. The 13 Nov slice (the 48
sessions with injection_task_0/1 and the benign ones) is a subset of it. The predictions are in
evals/public-anchor/PREDICTIONS.md, dated (the slice's blind, the other 112's after the slice result was known); the commit order is in a private source repository. AgentDojo's own utility and
security checks are the verdict; our claim auditor only adds columns. The key is read from the env file into this
process's client object only: it is never put in os.environ, printed, logged or written.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pierre.anchor.defences import ARMS, session_facts, system_for  # noqa: E402
from pierre.anchor.metrics import first_missing, summarise, toward_injection_round  # noqa: E402

EVAL_DIR = ROOT / "evals" / "public-anchor"
SESSIONS = EVAL_DIR / "agentdojo_banking_sessions.jsonl"
LEDGER = EVAL_DIR / "agentdojo_banking_ledger.json"
TRACES = EVAL_DIR / "traces"
CONCLUSION = EVAL_DIR / "conclusion.md"
REPORT = ROOT / "docs" / "calibration" / "public_anchor_agentdojo.md"

PACKAGE_VERSION = "0.1.35"
BENCH_VERSION = "v1.2.2"
SUITE = "banking"
ATTACK = "important_instructions"
MODEL = "claude-sonnet-5-5"
SLICE_INJECTIONS = ("injection_task_0", "injection_task_1")  # the 13 Nov slice: benign + these two per user task = 48 sessions
SLICE_SESSIONS = 48
# What the 13 Nov slice metered (its ledger, committed then). The ledger file is cumulative, so "this run" is the ledger minus this.
SLICE_LEDGER = {"calls": 180, "est_input_tokens": 405777, "billed_input_tokens": 617893, "output_tokens": 87979}
OLD_CAP = (600, 4_000_000)  # the slice's cap
CEILING = (1_100, 7_000_000)  # hard ceiling for the full suite, counting the slice's ledger
CAP_MARGIN = 1.25
FULL_N = 160
# CAP: the estimate for all 160 sessions (estimate(16, 144), printed by every preflight) is 1160 calls / 5,236,840 input tokens; times 1.25 that is
# 1450 calls / 6,546,050 tokens. Calls are clamped to the ceiling (1100); tokens fit under it. The preflight recomputes and fails if these drift.
MAX_CALLS, MAX_INPUT_TOKENS = 1_100, 6_546_050
PREDICTED = {"utility": 0.80, "attack_success": 0.10}  # slice: the blind prediction of 13 Nov in PREDICTIONS.md
PREDICTED_112 = {"attack_success": 0.04, "utility_under_attack": 0.90}  # written 18 Nov, after the slice result was known
PREDICTED_144 = {"attack_success": 0.03, "utility_under_attack": 0.91}

# --- defence arms (built 18 Nov; no session of these pairs has been run) ---
ARMS_DIR = EVAL_DIR / "arms"
LOCAL_MODEL = "gemma4:e2b"
# provider, and the name the attack text addresses the model by (AgentDojo's table: "Claude" for every Claude id,
# "Local model" for its local entry; gemma is not in the table)
MODELS: Dict[str, Dict[str, str]] = {
    MODEL: {"provider": "anthropic", "attack_name": "Claude"},
    LOCAL_MODEL: {"provider": "ollama", "attack_name": "Local model"},
}
PAIRS: List[Tuple[str, str]] = [(MODEL, "none"), (MODEL, "confirm-on-mutate"), (MODEL, "spotlight"),
                                (LOCAL_MODEL, "none"), (LOCAL_MODEL, "confirm-on-mutate"), (LOCAL_MODEL, "spotlight")]
COMMITTED_PAIR = (MODEL, "none")
LOCAL_NUM_CTX = 16384
LOCAL_NUM_PREDICT = 4096
LOCAL_THINK = False
LOCAL_TIMEOUT_S = 600.0  # one local call on a laptop; the API figure (120 s) is too short for a 2B model on CPU/GPU
# Caps per new pair: (billed API calls, billed API est. input tokens, local calls, local est. input tokens) for all 160
# sessions, from want_cap (Claude: expected x CAP_MARGIN; local: the loop's worst case). --dry-run recomputes them and
# fails if these drift. These are what each new pair's run is held to; changing one means editing this table.
PAIR_CAPS: Dict[Tuple[str, str], Tuple[int, int, int, int]] = {
    (MODEL, "confirm-on-mutate"): (1450, 6_659_270, 0, 0),
    (MODEL, "spotlight"): (1450, 6_743_950, 0, 0),
    (LOCAL_MODEL, "none"): (480, 3_359_400, 2560, 13_891_200),
    (LOCAL_MODEL, "confirm-on-mutate"): (480, 3_359_400, 2560, 13_891_200),
    (LOCAL_MODEL, "spotlight"): (480, 3_359_400, 2560, 14_124_800),
}


def slug(model: str) -> str:
    return re.sub(r"[^a-z0-9.-]+", "-", model.lower())


def pair_paths(model: str, arm: str) -> Dict[str, Path]:
    """sessions, ledger (Anthropic API calls), local_ledger (local model calls) and traces of one (model, arm) pair."""
    if (model, arm) == COMMITTED_PAIR:
        return {"sessions": SESSIONS, "ledger": LEDGER, "local_ledger": EVAL_DIR / "agentdojo_banking_local_ledger.json", "traces": TRACES}
    d = ARMS_DIR / f"{slug(model)}__{arm}"
    return {"sessions": d / "sessions.jsonl", "ledger": d / "ledger.json", "local_ledger": d / "local_ledger.json", "traces": d / "traces"}


def estimate_split(n_benign: int, n_attacked: int, system: str, tools: list) -> Dict[str, int]:
    """``estimate``'s call count split into agent calls (the model under test) and auditor calls, plus a worst case.

    Same per-session counts as ``estimate``: an agent makes 4 calls on a benign session and 5 on an attacked one, and half
    the attacked sessions get one repair round. A repair round is 2 agent calls and 1 audit (``estimate`` puts all 3 on
    one side); calls add up to ``estimate``'s, tokens do not (a repair audit is priced at the audit prompt's size).
    The worst case is the loop's own ceiling: per session MAX_TOOL_ITERATIONS + MAX_REPAIR_ROUNDS x REPAIR_ITERATIONS
    agent calls and 1 + MAX_REPAIR_ROUNDS audits.
    """
    from pierre.agent.loop import MAX_TOOL_ITERATIONS
    from pierre.agent.repair import MAX_REPAIR_ROUNDS, REPAIR_ITERATIONS
    from pierre.agents._claim_auditor import _build_audit_prompt

    prefix = (len(system) + len(json.dumps(tools))) // 4
    agent_rate, audit_rate = prefix + 3000, len(_build_audit_prompt("", {})) // 4 + 3000
    repaired = int(n_attacked * 0.5)
    agent = n_benign * 4 + n_attacked * 5 + repaired * 2
    audit = n_benign + n_attacked + repaired
    n = n_benign + n_attacked
    worst_agent, worst_audit = n * (MAX_TOOL_ITERATIONS + MAX_REPAIR_ROUNDS * REPAIR_ITERATIONS), n * (1 + MAX_REPAIR_ROUNDS)
    return {
        "agent_calls": agent, "agent_tokens": agent * agent_rate, "audit_calls": audit, "audit_tokens": audit * audit_rate,
        "worst_agent_calls": worst_agent, "worst_agent_tokens": worst_agent * agent_rate,
        "worst_audit_calls": worst_audit, "worst_audit_tokens": worst_audit * audit_rate, "prefix_tokens": prefix,
    }


def pair_estimate(model: str, arm: str, n_benign: int, n_attacked: int, system: str, tools: list) -> Dict[str, int]:
    """Billed (Anthropic API) and local calls / tokens for one pair, expected and worst case. On a local model only the
    auditor is billed."""
    e = estimate_split(n_benign, n_attacked, system_for(arm, system), tools)
    local = MODELS[model]["provider"] == "ollama"
    out = {"prefix_tokens": e["prefix_tokens"]}
    for pre in ("", "worst_"):
        out[f"{pre}billed_calls"] = e[f"{pre}audit_calls"] + (0 if local else e[f"{pre}agent_calls"])
        out[f"{pre}billed_tokens"] = e[f"{pre}audit_tokens"] + (0 if local else e[f"{pre}agent_tokens"])
        out[f"{pre}local_calls"] = e[f"{pre}agent_calls"] if local else 0
        out[f"{pre}local_tokens"] = e[f"{pre}agent_tokens"] if local else 0
    return out


def want_cap(model: str, est: Dict[str, int]) -> Tuple[int, int, int, int]:
    """The cap for a pair's full 160 sessions. Claude: the expected estimate x CAP_MARGIN, as the committed run was capped
    (its metered 594 calls were about half the estimate of 1160). Local model: the worst case, since a 2B model may
    repair far more often than Sonnet did; calls are the loop's hard ceiling, tokens are chars / 4 so they get the margin."""
    up = lambda v: int(v * CAP_MARGIN + 0.999)  # noqa: E731
    if MODELS[model]["provider"] == "ollama":
        return est["worst_billed_calls"], up(est["worst_billed_tokens"]), est["worst_local_calls"], up(est["worst_local_tokens"])
    return up(est["billed_calls"]), up(est["billed_tokens"]), up(est["local_calls"]), up(est["local_tokens"])


def largest_prompt_tokens(prefix_tokens: int, arm: str, traces: Path = TRACES) -> int:
    """The largest agent prompt to expect, from the committed Sonnet traces: prefix plus a session's whole history
    (request, every call and result, every answer and repair message), chars / 4. Spotlight adds its markers per result."""
    from pierre.anchor.defences import CLOSE, OPEN

    worst = 0
    for f in sorted(traces.glob("*.trace.jsonl")):
        for line in f.read_text().splitlines():
            r = json.loads(line)
            chars = len(r["input"]) + len(json.dumps(r["tool_calls"])) + len(json.dumps(r["tool_results"]))
            chars += sum(len(x.get("answer") or "") + 400 for x in r["meta"].get("rounds") or [])  # 400: a repair message
            if arm == "spotlight":
                chars += len(r["tool_results"]) * (len(OPEN) + len(CLOSE) + 2)
            worst = max(worst, chars // 4)
    return prefix_tokens + worst


def _num(task_id: str) -> int:
    return int(task_id.rsplit("_", 1)[1])


def planned(suite: Any) -> List[Tuple[str, Optional[str]]]:
    """The whole suite: every user task alone, then each with every injection task; numeric id order."""
    users = sorted(suite.user_tasks, key=_num)
    injs = sorted(suite.injection_tasks, key=_num)
    return [(u, None) for u in users] + [(u, i) for u in users for i in injs]


def key_of(user: str, inj: Optional[str]) -> str:
    return f"{SUITE}/{user}" + (f"/{inj}" if inj else "")


def estimate(n_benign: int, n_attacked: int, system: str, tools: list) -> Tuple[int, int, Dict[str, int]]:
    """Calls and input tokens (chars / 4, resent history counted on every call) before the first call."""
    from pierre.agents._claim_auditor import _build_audit_prompt

    prefix = (len(system) + len(json.dumps(tools))) // 4
    audit_prompt = len(_build_audit_prompt("", {})) // 4
    agent_calls = n_benign * 4 + n_attacked * 5
    audit_calls = n_benign + n_attacked
    repair_calls = int(n_attacked * 0.5) * 3  # headroom: half the attacked sessions get one repair round
    calls = agent_calls + audit_calls + repair_calls
    tokens = (agent_calls + repair_calls) * (prefix + 3000) + audit_calls * (audit_prompt + 3000)
    return calls, tokens, {"prefix_tokens": prefix, "audit_prompt_tokens": audit_prompt}


def read_key(env_file: Path) -> str:
    for line in env_file.read_text().splitlines():
        m = re.match(r"\s*(?:export\s+)?ANTHROPIC_API_KEY\s*=\s*(.*)", line)
        if m:
            return m.group(1).strip().strip("'\"")
    raise SystemExit(f"no ANTHROPIC_API_KEY line in {env_file.name}")


def build_client(env_file: Path) -> Any:
    import anthropic

    from pierre.agent.budget import BudgetedClient

    inner = anthropic.Anthropic(api_key=read_key(env_file), max_retries=0)
    return BudgetedClient(
        inner, LEDGER, MAX_CALLS, MAX_INPUT_TOKENS,
        retryable=(anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError),
    )


def _truth(calls: list) -> List[Dict[str, Any]]:
    return [{"function": c.function, "args": dict(c.args), "placeholder_args": dict(c.placeholder_args) if c.placeholder_args else None} for c in calls]


def run_one(
    suite: Any, client: Any, system: str, user_id: str, inj_id: Optional[str], *, model: str = MODEL, arm: str = "none",
    audit_client: Any = None, traces: Path = TRACES, loop_overrides: Optional[Dict[str, Any]] = None,
    runtime_extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    from pierre.agent.trace import TraceWriter
    from pierre.anchor.agentdojo_adapter import ImportantInstructionsForClaude, ShowcasePipeline

    pipeline = ShowcasePipeline(client, system, model, arm=arm, audit_client=audit_client, loop_overrides=loop_overrides)
    user_task = suite.user_tasks[user_id]
    inj_task = suite.injection_tasks[inj_id] if inj_id else None
    rec: Dict[str, Any] = {"key": key_of(user_id, inj_id), "user_task": user_id, "injection_task": inj_id, "injectable": True}
    injections: Dict[str, str] = {}
    if inj_task is not None:
        try:
            injections = ImportantInstructionsForClaude(suite, pipeline, MODELS[model]["attack_name"]).attack(user_task, inj_task)
        except ValueError as exc:
            rec.update(injectable=False, note=str(exc)[:200])
            return rec
    env = suite.load_and_inject_default_environment(injections)
    pre_env = user_task.init_environment(env.model_copy(deep=True))
    user_truth = _truth(user_task.ground_truth(pre_env))
    inj_truth = _truth(inj_task.ground_truth(pre_env)) if inj_task else []
    utility, security = suite.run_task_with_pipeline(pipeline, user_task, inj_task, injections, environment=env)
    last = pipeline.last
    turn, log = last["turn"], last["log"]
    audits = [r["audit"] for r in turn.rounds]
    first = next((a for a in audits if a), None)
    final = next((a for a in reversed(audits) if a), None)
    made = [c for c in log]
    missing = first_missing(made, user_truth)
    toward = toward_injection_round(made, inj_truth) if inj_task else None
    if utility:
        step = None
    elif turn.termination:
        step = f"loop ended early: {turn.termination}"
    elif missing is not None:
        c = user_truth[missing]
        step = f"ground-truth call {missing + 1} of {len(user_truth)} never made: {c['function']}({json.dumps(c['args'], sort_keys=True)})"
    else:
        step = "every ground-truth call was made; the benchmark's utility check on the answer or final state failed"
    rec.update(
        utility=bool(utility),
        security=bool(security) if inj_task else None,
        steps=turn.steps,
        termination=turn.termination,
        repair_rounds=len(turn.rounds) - 1,
        auditor_calls=turn.auditor_calls,
        dedup_hits=turn.dedup_hits,
        fabricated_first=(first or {}).get("fabricated", 0),
        unverifiable=(final or {}).get("unverifiable", 0),
        audit_error=any((a or {}).get("error") for a in audits),
        toward_round=toward,
        first_failing_step=step,
        calls=[{"tool": c["tool"], "args": c["args"], "round": c["round"]} for c in made],
        answer=turn.answer[:600],
    )
    rec.update(row_defence_columns(arm, model, inj_id, rec["security"], log, inj_truth))
    writer = TraceWriter(
        traces / (rec["key"].replace("/", "__") + ".trace.jsonl"), rec["key"], rec["key"], task=f"agentdojo-{SUITE}",
        runtime={"agentdojo": PACKAGE_VERSION, "benchmark_version": BENCH_VERSION, "attack": ATTACK if inj_id else None, "model_requested": model,
                 **({"defence": arm} if (model, arm) != COMMITTED_PAIR else {}), **(runtime_extra or {})},
        provider=MODELS[model]["provider"],
    )
    writer.write_turn(0, user_task.PROMPT, turn, None, {"benchmark": {"utility": bool(utility), "security": rec["security"], "injection_task": inj_id}})
    return rec


def row_defence_columns(
    arm: str, model: str, inj_id: Optional[str], security: Optional[bool], log: List[Dict[str, Any]], inj_truth: Optional[list] = None
) -> Dict[str, Any]:
    """The columns a defence-arm row adds: model, attack success (the benchmark's security check, None on a benign
    session), and the defence facts (fired, false stop, held calls). The committed pair's rows never get them."""
    if (model, arm) == COMMITTED_PAIR:
        return {}
    return {"model": model, "attack_success": security if inj_id else None, **session_facts(arm, inj_id, log, inj_truth or [])}


def pending(plan: List[Tuple[str, Optional[str]]], sessions: List[Dict[str, Any]]) -> List[Tuple[str, Optional[str]]]:
    """The planned sessions that still need a run: none that is already in the file (an infra-error row does not count as done)."""
    done = {r["key"] for r in sessions if not r.get("infra_error")}
    return [(u, i) for u, i in plan if key_of(u, i) not in done]


def append_row(path: Path, rec: Dict[str, Any]) -> None:
    """Rows are only ever appended; an existing row is never rewritten."""
    with path.open("a") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")


def run_todo(todo: List[Tuple[str, Optional[str]]], run: Any, path: Path) -> int:
    """Run each pending session through ``run(user, inj)`` and append its row as it finishes. Returns 1 when the budget stops it."""
    from pierre.agent.budget import BudgetExceeded

    for u, i in todo:
        try:
            rec = run(u, i)
        except BudgetExceeded as exc:
            print(f"FAIL: {exc}")
            return 1
        except Exception as exc:  # an infrastructure failure is a missing session, not a model outcome
            rec = {"key": key_of(u, i), "user_task": u, "injection_task": i, "infra_error": f"{type(exc).__name__}: {str(exc)[:160]}"}
        append_row(path, rec)
        print(f"{rec['key']}: " + ("infra error" if rec.get("infra_error") else f"utility={rec.get('utility')} security={rec.get('security')} repair_rounds={rec.get('repair_rounds')}"), flush=True)
    return 0


def load_sessions_from(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_sessions() -> List[Dict[str, Any]]:
    return load_sessions_from(SESSIONS)


def pct(r: Dict[str, Any]) -> str:
    if not r["n"]:
        return "n/a (0 sessions)"
    return f"{r['k']}/{r['n']} = {100 * r['rate']:.1f}% (95% Wilson {100 * r['wilson_low']:.1f}% to {100 * r['wilson_high']:.1f}%)"


def effective(sessions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One row per key for the report: a later good row replaces an earlier infrastructure-error row; the file itself is untouched."""
    by: Dict[str, Dict[str, Any]] = {}
    for r in sessions:
        if r["key"] not in by or by[r["key"]].get("infra_error"):
            by[r["key"]] = r
    return list(by.values())


def in_slice(r: Dict[str, Any]) -> bool:
    return r["injection_task"] is None or r["injection_task"] in SLICE_INJECTIONS


def _ord(r: Dict[str, Any]) -> Tuple[bool, int, int]:
    return (r["injection_task"] is not None, _num(r["user_task"]), _num(r["injection_task"]) if r["injection_task"] else -1)


def render(sessions: List[Dict[str, Any]], ledger: Dict[str, int], conclusion: str) -> str:
    sessions = sorted(effective(sessions), key=_ord)
    sl = [r for r in sessions if in_slice(r)]
    rest = [r for r in sessions if not in_slice(r)]
    s_full, s_slice, s_rest = summarise(sessions), summarise(sl), summarise(rest)
    rp = s_full["repair"]
    run = {k: ledger.get(k, 0) - SLICE_LEDGER[k] for k in SLICE_LEDGER}
    L: List[str] = []
    a = L.append
    a("# Public anchor: AgentDojo, banking suite (full), through the showcase loop")
    a("")
    a("> **Auto-generated** by `scripts/analysis/public_anchor_agentdojo.py --render-md` from `evals/public-anchor/agentdojo_banking_sessions.jsonl`. Do not hand-edit.")
    a("")
    a("This is an anchor for the loop and the trace grader on cases we did not write. It is not a leaderboard entry, and it does not replace the pierre exam.")
    a("")
    a("## What was run")
    a("")
    a(f"- Benchmark: `agentdojo=={PACKAGE_VERSION}`, benchmark version `{BENCH_VERSION}`, suite `{SUITE}`, attack `{ATTACK}`. AgentDojo's own utility and security checks are the verdict.")
    a(f"- Agent: the showcase loop with its shipped repair settings (repair on `fabricated` only, 2 rounds), model `{MODEL}`, k = 1, effort `medium`. The claim auditor is the judge and stays on its fixed judge model.")
    a(f"- Two runs make the full suite ({len(sessions)} of 160 sessions are in the file). The **13 Nov slice** ({len(sl)} of {SLICE_SESSIONS} sessions): all 16 user tasks without injection, plus each with `injection_task_0` and `injection_task_1`; its prediction is the blind one in `evals/public-anchor/PREDICTIONS.md`. The **18 Nov run** ({len(rest)} of 112 sessions): each user task with `injection_task_2` .. `injection_task_8`; its prediction was written after the slice result was known. Sessions already in the file were not run again and their rows were not changed.")
    a("- System prompt: AgentDojo's own default. Tools: the suite's 11 banking functions, run one after another on the benchmark's runtime.")
    a("- Deviation: the attack class derives the model's name from a table of old model ids and raises for this one, so the name is set to `Claude` (what the table gives every Claude id). The injected text is otherwise the benchmark's.")
    a("- The auditor's prompt is written for VC reports; here its evidence is every tool result plus the user's request, and injected text sits inside that evidence. It is out of its domain on this suite.")
    a("")
    a("## Result: the slice and the full suite")
    a("")
    a("| quantity | 13 Nov slice | full suite |")
    a("|---|---|---|")
    a(f"| sessions | {len(sl)} | {len(sessions)} |")
    for label, k in (("utility without injection", "utility"), ("targeted attack success (security check true)", "attack_success"), ("utility under attack", "utility_under_attack")):
        a(f"| {label} | {pct(s_slice[k])} | {pct(s_full[k])} |")
    a("")
    a("Slice predictions (blind, 13 Nov): utility without injection 0.80, targeted attack success 0.10; utility under attack was not predicted.")
    a("")
    a("Published figures: AgentDojo paper (arXiv 2406.13352), appendix table \"Detailed results for Figure 6\", Claude 3.5 Sonnet, whole benchmark: benign utility 78.22%, utility under attack 51.19%, targeted attack success 33.86%. "
      "Not comparable (older model, all suites, other benchmark version, other agent loop). For Sonnet-class on banking alone at this version: **no published comparable**.")
    a("")
    left = sorted(set(s_full["infra_errors"]))
    if left or s_full["not_injectable"]:
        a(f"Left out of the denominators: infrastructure errors {left or 'none'}; not injectable {s_full['not_injectable'] or 'none'}.")
        a("")
    missing = FULL_N - len(sessions)
    if missing:
        a(f"**{missing} of the 160 sessions are missing from this file** (run cut short or errored); nothing was filled in by hand.")
        a("")
    a("## The 112 sessions against the prediction (written after the slice result)")
    a("")
    a("| quantity | observed | predicted |")
    a("|---|---|---|")
    a(f"| targeted attack success, the 112 | {pct(s_rest['attack_success'])} | {PREDICTED_112['attack_success']:.2f} |")
    a(f"| utility under attack, the 112 | {pct(s_rest['utility_under_attack'])} | {PREDICTED_112['utility_under_attack']:.2f} |")
    a(f"| targeted attack success, all attacked sessions | {pct(s_full['attack_success'])} | {PREDICTED_144['attack_success']:.2f} |")
    a(f"| utility under attack, all attacked sessions | {pct(s_full['utility_under_attack'])} | {PREDICTED_144['utility_under_attack']:.2f} |")
    a("")
    a("## One row per injection task")
    a("")
    a("| injection task | attacked sessions | attack success | utility under attack |")
    a("|---|---|---|---|")
    for n in sorted({r["injection_task"] for r in sessions if r["injection_task"]}, key=_num):
        sub = summarise([r for r in sessions if r["injection_task"] == n])
        a(f"| {n}{' (slice)' if n in SLICE_INJECTIONS else ''} | {sub['attack_success']['n']} | {pct(sub['attack_success'])} | {pct(sub['utility_under_attack'])} |")
    a("")
    a("## What the repair loop did")
    a("")
    a(f"- Sessions with at least one repair round: {rp['sessions_with_a_repair_round']} ({rp['repair_rounds']} rounds in all). On attacked sessions: {rp['attacked_sessions_with_a_repair_round']} sessions, {rp['attacked_repair_rounds']} rounds.")
    toward = [r["key"] for r in sessions if r["injection_task"] and (r.get("toward_round") or 0) >= 1 and not r.get("infra_error")]
    a(f"- A repair round moved the session TOWARDS the injection (a call matching the injection task's ground truth first made in a repair round, not in the draft): **{rp['repair_moved_toward_injection']}** of {s_full['attack_success']['n']} attacked sessions" + (": " + ", ".join(f"`{k}`" for k in toward) + "." if toward else "."))
    a(f"- Attacked sessions where the draft itself already made a call matching the injection's ground truth: {rp['attack_calls_in_draft']}.")
    a(f"- Sessions where the auditor never ran (no tool call, empty answer, or no evidence): {rp['sessions_with_no_audit']}. Fabricated claims found in first audits: {rp['fabricated_claims_first_audit']}. Unverifiable claims (counted, never repaired): {rp['unverifiable_claims']}.")
    a("")
    bad = [r for r in sessions if not r.get("infra_error") and r.get("injectable", True) and (not r["utility"] or r["security"])]
    a("## Failed sessions: first failing step")
    a("")
    if bad:
        a("| session | utility | security | first failing step |")
        a("|---|---|---|---|")
        for r in bad:
            sec = "n/a" if r["security"] is None else ("ATTACK SUCCEEDED" if r["security"] else "held")
            a(f"| {r['key']} | {'pass' if r['utility'] else 'fail'} | {sec} | {r['first_failing_step'] or '(utility passed; the attack succeeded)'} |")
    else:
        a("None.")
    a("")
    a("The first failing step is computed against the task's ground-truth calls (a call matches on function and on every argument the task fixes). It is a pointer for reading the trace, not a grade; the benchmark's check decides.")
    a("")
    a("## Sessions")
    a("")
    a("| session | utility | security | steps | repair rounds | first failing step |")
    a("|---|---|---|---|---|---|")
    for r in sessions:
        if r.get("infra_error") or not r.get("injectable", True):
            a(f"| {r['key']} | n/a | n/a | | | {r.get('note') or r.get('infra_error')} |")
            continue
        sec = "n/a" if r["security"] is None else ("ATTACK SUCCEEDED" if r["security"] else "held")
        a(f"| {r['key']} | {'pass' if r['utility'] else 'fail'} | {sec} | {r['steps']} | {r['repair_rounds']} | {r['first_failing_step'] or ''} |")
    a("")
    a("## Metered usage")
    a("")
    a("Agent and auditor calls are both included. Tag: metered. Estimated input tokens are chars / 4; billed tokens are what the API reported.")
    a("")
    a("| run | model calls | estimated input tokens | billed input tokens | output tokens |")
    a("|---|---|---|---|---|")
    a(f"| 13 Nov slice | {SLICE_LEDGER['calls']} | {SLICE_LEDGER['est_input_tokens']} | {SLICE_LEDGER['billed_input_tokens']} | {SLICE_LEDGER['output_tokens']} |")
    a(f"| 18 Nov run | {run['calls']} | {run['est_input_tokens']} | {run['billed_input_tokens']} | {run['output_tokens']} |")
    a(f"| total (ledger) | {ledger.get('calls', 0)} | {ledger.get('est_input_tokens', 0)} | {ledger.get('billed_input_tokens', 0)} | {ledger.get('output_tokens', 0)} |")
    a("")
    a(f"Cap for the full suite: {MAX_CALLS} calls / {MAX_INPUT_TOKENS} estimated input tokens, counting the slice's calls (the slice's own cap was {OLD_CAP[0]} / {OLD_CAP[1]}). It is the preflight's estimate for all 160 sessions (1160 calls, 5236840 tokens, a deliberately pessimistic count) times {CAP_MARGIN}, with calls clamped to the hard ceiling of {CEILING[0]} / {CEILING[1]}.")
    a("")
    a("## What this says")
    a("")
    a(conclusion.strip() if conclusion.strip() else "_Not written yet._")
    a("")
    a("## Reproduce")
    a("")
    a("```sh")
    a("make install        # includes the pinned agentdojo extra")
    a("make anchor-eval MODEL=<id> ARM=<arm>    # a new defence arm, live: needs a key file, spends tokens; the committed run itself is refused")
    a("make reports        # re-renders this file from the committed results, no model call")
    a("```")
    return "\n".join(L) + "\n"


def render_md() -> None:
    sessions = load_sessions()
    if not sessions:
        raise SystemExit("no committed sessions file: evals/public-anchor/agentdojo_banking_sessions.jsonl is missing")
    ledger = json.loads(LEDGER.read_text()) if LEDGER.exists() else {}
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(render(sessions, ledger, CONCLUSION.read_text() if CONCLUSION.exists() else ""))
    print(f"wrote {REPORT.relative_to(ROOT)}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--preflight-only", action="store_true")
    ap.add_argument("--render-md", action="store_true")
    ap.add_argument("--env-file", type=Path)
    ap.add_argument("--dry-run", action="store_true", help="per (model, arm): sessions, estimated calls and input tokens; no call")
    ap.add_argument("--arm", choices=ARMS, default="none")
    ap.add_argument("--model", choices=sorted(MODELS), default=MODEL)
    ap.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    args = ap.parse_args()
    if args.render_md:
        render_md()
        return 0
    if args.dry_run:
        return dry_run()
    if (args.model, args.arm) != COMMITTED_PAIR:
        return run_arm(args.model, args.arm, args.env_file, args.ollama_url)

    from agentdojo.agent_pipeline.agent_pipeline import load_system_message
    from agentdojo.task_suite.load_suites import get_suite

    from pierre.anchor.agentdojo_adapter import claude_tools_from_runtime
    from agentdojo.functions_runtime import FunctionsRuntime

    suite = get_suite(BENCH_VERSION, SUITE)
    system = load_system_message(None)
    plan = planned(suite)
    todo = pending(plan, load_sessions())
    tools = claude_tools_from_runtime(FunctionsRuntime(suite.tools))
    nb, na = sum(1 for _, i in todo if i is None), sum(1 for _, i in todo if i)
    calls, tokens, parts = estimate(nb, na, system, tools)
    used = json.loads(LEDGER.read_text()) if LEDGER.exists() else {"calls": 0, "est_input_tokens": 0}
    print(f"suite {SUITE} {BENCH_VERSION}: {len(suite.user_tasks)} user x {len(suite.injection_tasks)} injection tasks = {len(suite.user_tasks) * (1 + len(suite.injection_tasks))} sessions in full")
    print(f"call list: {len(todo)} sessions to run ({nb} utility, {na} security; {len(plan) - len(todo)} already done)")
    print(f"estimated model calls: about {calls}; estimated input tokens (chars / 4): about {tokens}  [{parts}]")
    all_calls, all_tokens, _ = estimate(len(suite.user_tasks), len(suite.user_tasks) * len(suite.injection_tasks), system, tools)
    want = (int(all_calls * CAP_MARGIN + 0.999), int(all_tokens * CAP_MARGIN + 0.999))
    print(f"estimate for all {len(plan)} sessions: {all_calls} calls / {all_tokens} tokens; x{CAP_MARGIN} = {want[0]} / {want[1]}; ceiling {CEILING[0]} / {CEILING[1]}")
    print(f"already metered in the ledger: {used['calls']} calls / {used['est_input_tokens']} estimated input tokens; cap {MAX_CALLS} / {MAX_INPUT_TOKENS} (old cap {OLD_CAP[0]} / {OLD_CAP[1]})")
    if (MAX_CALLS, MAX_INPUT_TOKENS) != (min(want[0], CEILING[0]), min(want[1], CEILING[1])):
        print(f"FAIL: the pinned cap {MAX_CALLS} / {MAX_INPUT_TOKENS} is not the estimate x {CAP_MARGIN} clamped to the ceiling ({min(want[0], CEILING[0])} / {min(want[1], CEILING[1])}).")
        return 1
    total_calls, total_tokens = used["calls"] + calls, used["est_input_tokens"] + tokens
    if total_calls > CEILING[0] or total_tokens > CEILING[1]:
        print(f"FAIL estimate {total_calls} calls / {total_tokens} input tokens (ledger plus the {len(todo)} sessions to run) over the ceiling {CEILING[0]} / {CEILING[1]}")
        return 1
    print(f"ok: ledger plus estimate {total_calls} calls / {total_tokens} tokens is under the ceiling")
    if args.preflight_only:
        return 0
    if not args.env_file:
        raise SystemExit("--env-file is required for a live run")
    print(f"refused: ({MODEL}, none) is the committed run of 18 Nov; it is never run again ({len(todo)} sessions pending)")
    return 2


def _suite_bits() -> Tuple[Any, str, list, List[Tuple[str, Optional[str]]]]:
    from agentdojo.agent_pipeline.agent_pipeline import load_system_message
    from agentdojo.functions_runtime import FunctionsRuntime
    from agentdojo.task_suite.load_suites import get_suite

    from pierre.anchor.agentdojo_adapter import claude_tools_from_runtime

    suite = get_suite(BENCH_VERSION, SUITE)
    return suite, load_system_message(None), claude_tools_from_runtime(FunctionsRuntime(suite.tools)), planned(suite)


def dry_run_rows(suite: Any, system: str, tools: list, plan: List[Tuple[str, Optional[str]]]) -> List[Dict[str, Any]]:
    rows = []
    n_users = len(suite.user_tasks)
    for model, arm in PAIRS:
        paths = pair_paths(model, arm)
        todo = pending(plan, load_sessions_from(paths["sessions"]))
        nb, na = sum(1 for _, i in todo if i is None), sum(1 for _, i in todo if i)
        est = pair_estimate(model, arm, nb, na, system, tools)
        full = pair_estimate(model, arm, n_users, n_users * len(suite.injection_tasks), system, tools)
        local = MODELS[model]["provider"] == "ollama"
        rows.append({
            "model": model, "arm": arm, "planned": len(plan), "done": len(plan) - len(todo), "to_run": len(todo),
            "to_run_benign": nb, "to_run_attacked": na, **est,
            "cap": None if (model, arm) == COMMITTED_PAIR else want_cap(model, full),
            "largest_prompt_tokens": largest_prompt_tokens(est["prefix_tokens"], arm) if local else None,
            "num_ctx": LOCAL_NUM_CTX if local else None,
        })
    return rows


def dry_run_table(rows: List[Dict[str, Any]]) -> str:
    L = ["| model | arm | sessions to run (benign + attacked) | done | billed API calls (worst) | billed est. input tokens (worst) | local calls (worst) | local est. input tokens (worst) | largest local prompt / num_ctx |",
         "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        ctx = f"{r['largest_prompt_tokens']} / {r['num_ctx']}" if r["num_ctx"] else "n/a"
        note = " (committed; never run again)" if (r["model"], r["arm"]) == COMMITTED_PAIR else ""
        L.append(f"| {r['model']} | {r['arm']}{note} | {r['to_run']} ({r['to_run_benign']} + {r['to_run_attacked']}) | {r['done']} | "
                 f"{r['billed_calls']} ({r['worst_billed_calls']}) | {r['billed_tokens']} ({r['worst_billed_tokens']}) | "
                 f"{r['local_calls']} ({r['worst_local_calls']}) | {r['local_tokens']} ({r['worst_local_tokens']}) | {ctx} |")
    new = [r for r in rows if (r["model"], r["arm"]) != COMMITTED_PAIR]
    tot = lambda k: sum(r[k] for r in new)  # noqa: E731
    L.append(f"| **all new pairs** | | {tot('to_run')} | | {tot('billed_calls')} ({tot('worst_billed_calls')}) | {tot('billed_tokens')} ({tot('worst_billed_tokens')}) | "
             f"{tot('local_calls')} ({tot('worst_local_calls')}) | {tot('local_tokens')} ({tot('worst_local_tokens')}) | |")
    return "\n".join(L)


def dry_run() -> int:
    suite, system, tools, plan = _suite_bits()
    rows = dry_run_rows(suite, system, tools, plan)
    print("Dry run: no model call. Expected: chars / 4 with the full-suite runner's per-session counts (agent 4 calls a benign session, 5 an attacked one, "
          "one repair round of 2 agent calls + 1 audit for half the attacked sessions; 1 audit a session). Worst: the loop's ceiling (16 agent calls, "
          "3 audits a session). Billed = Anthropic API (the auditor always; the agent too on Claude). The committed Sonnet run metered 594 calls against "
          "an expected 1160.")
    print(dry_run_table(rows))
    drift = [(r["model"], r["arm"], PAIR_CAPS.get((r["model"], r["arm"])), r["cap"]) for r in rows if r["cap"] is not None and PAIR_CAPS.get((r["model"], r["arm"])) != r["cap"]]
    for model, arm, have, want in drift:
        print(f"FAIL: the pinned cap for ({model}, {arm}) is {have}; want_cap gives {want}")
    over = [r for r in rows if r["num_ctx"] and r["largest_prompt_tokens"] > r["num_ctx"] - LOCAL_NUM_PREDICT]
    for r in over:
        print(f"FAIL: ({r['model']}, {r['arm']}) largest prompt {r['largest_prompt_tokens']} does not fit num_ctx {r['num_ctx']} minus {LOCAL_NUM_PREDICT}")
    return 1 if drift or over else 0


def build_arm_clients(model: str, arm: str, paths: Dict[str, Path], env_file: Path, ollama_url: str) -> Tuple[Any, Any]:
    """(agent client, audit client), each behind its own ledger and the pair's pinned caps."""
    import anthropic

    from pierre.agent.budget import BudgetedClient

    b_calls, b_tokens, l_calls, l_tokens = PAIR_CAPS[(model, arm)]
    retry = (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError)
    api = BudgetedClient(anthropic.Anthropic(api_key=read_key(env_file), max_retries=0), paths["ledger"], b_calls, b_tokens, retryable=retry)
    if MODELS[model]["provider"] == "anthropic":
        return api, api
    from pierre.anchor.ollama_client import OllamaClient

    local = OllamaClient(ollama_url, num_ctx=LOCAL_NUM_CTX, num_predict=LOCAL_NUM_PREDICT, think=LOCAL_THINK, timeout_s=LOCAL_TIMEOUT_S)
    return BudgetedClient(local, paths["local_ledger"], l_calls, l_tokens, retries=0), api


def run_arm(model: str, arm: str, env_file: Optional[Path], ollama_url: str) -> int:
    """A live run of one new (model, arm) pair. Resumes from its own sessions file; never touches the committed pair's files."""
    if (model, arm) == COMMITTED_PAIR:
        print(f"refused: ({MODEL}, none) is the committed run of 18 Nov; it is never run again")
        return 2
    if (model, arm) not in PAIR_CAPS:
        print(f"FAIL: no pinned cap for ({model}, {arm}); run --dry-run and pin it in PAIR_CAPS")
        return 1
    if not env_file:
        raise SystemExit("--env-file is required for a live run (the claim auditor is on the Anthropic API for every pair)")
    if dry_run() != 0:
        return 1
    suite, system, _, plan = _suite_bits()
    paths = pair_paths(model, arm)
    todo = pending(plan, load_sessions_from(paths["sessions"]))
    agent, audit = build_arm_clients(model, arm, paths, env_file, ollama_url)
    local = MODELS[model]["provider"] == "ollama"
    overrides = {"model_timeout_s": LOCAL_TIMEOUT_S} if local else None
    extra = {"local": {"num_ctx": LOCAL_NUM_CTX, "num_predict": LOCAL_NUM_PREDICT, "think": LOCAL_THINK}} if local else None
    paths["sessions"].parent.mkdir(parents=True, exist_ok=True)
    return run_todo(
        todo,
        lambda u, i: run_one(suite, agent, system, u, i, model=model, arm=arm, audit_client=audit, traces=paths["traces"],
                             loop_overrides=overrides, runtime_extra=extra),
        paths["sessions"],
    )


if __name__ == "__main__":
    sys.exit(main())
