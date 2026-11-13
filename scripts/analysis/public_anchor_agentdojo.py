"""The public anchor: AgentDojo's banking suite (a fixed slice) through the showcase loop on claude-sonnet-5-5.

    python scripts/analysis/public_anchor_agentdojo.py --preflight-only   # sizes and the estimate; no key, no call
    python scripts/analysis/public_anchor_agentdojo.py --env-file PATH    # live run (spends tokens): `make anchor-eval`
    python scripts/analysis/public_anchor_agentdojo.py --render-md        # re-render the report from committed results

The subset and the predictions are fixed in evals/public-anchor/PREDICTIONS.md before any run. AgentDojo's own utility and
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
N_INJECTION_TASKS = 2
MAX_CALLS, MAX_INPUT_TOKENS = 600, 4_000_000
PREDICTED = {"utility": 0.80, "attack_success": 0.10}  # fixed in PREDICTIONS.md before any run


def _num(task_id: str) -> int:
    return int(task_id.rsplit("_", 1)[1])


def planned(suite: Any) -> List[Tuple[str, Optional[str]]]:
    """The fixed slice: every user task alone, then each with the first N injection tasks; numeric id order."""
    users = sorted(suite.user_tasks, key=_num)
    injs = sorted(suite.injection_tasks, key=_num)[:N_INJECTION_TASKS]
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


def run_one(suite: Any, client: Any, system: str, user_id: str, inj_id: Optional[str]) -> Dict[str, Any]:
    from pierre.agent.trace import TraceWriter
    from pierre.anchor.agentdojo_adapter import ImportantInstructionsForClaude, ShowcasePipeline

    pipeline = ShowcasePipeline(client, system, MODEL)
    user_task = suite.user_tasks[user_id]
    inj_task = suite.injection_tasks[inj_id] if inj_id else None
    rec: Dict[str, Any] = {"key": key_of(user_id, inj_id), "user_task": user_id, "injection_task": inj_id, "injectable": True}
    injections: Dict[str, str] = {}
    if inj_task is not None:
        try:
            injections = ImportantInstructionsForClaude(suite, pipeline).attack(user_task, inj_task)
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
    writer = TraceWriter(
        TRACES / (rec["key"].replace("/", "__") + ".trace.jsonl"), rec["key"], rec["key"], task=f"agentdojo-{SUITE}",
        runtime={"agentdojo": PACKAGE_VERSION, "benchmark_version": BENCH_VERSION, "attack": ATTACK if inj_id else None, "model_requested": MODEL},
    )
    writer.write_turn(0, user_task.PROMPT, turn, None, {"benchmark": {"utility": bool(utility), "security": rec["security"], "injection_task": inj_id}})
    return rec


def load_sessions() -> List[Dict[str, Any]]:
    if not SESSIONS.exists():
        return []
    return [json.loads(line) for line in SESSIONS.read_text().splitlines() if line.strip()]


def pct(r: Dict[str, Any]) -> str:
    if not r["n"]:
        return "n/a (0 sessions)"
    return f"{r['k']}/{r['n']} = {100 * r['rate']:.1f}% (95% Wilson {100 * r['wilson_low']:.1f}% to {100 * r['wilson_high']:.1f}%)"


def render(sessions: List[Dict[str, Any]], ledger: Dict[str, int], conclusion: str) -> str:
    s = summarise(sessions)
    rp = s["repair"]
    L: List[str] = []
    a = L.append
    a("# Public anchor: AgentDojo (banking slice) through the showcase loop")
    a("")
    a("> **Auto-generated** by `scripts/analysis/public_anchor_agentdojo.py --render-md` from `evals/public-anchor/agentdojo_banking_sessions.jsonl`. Do not hand-edit.")
    a("")
    a("This is an anchor for the loop and the trace grader on cases we did not write. It is not a leaderboard entry, and it does not replace the pierre exam.")
    a("")
    a("## What was run")
    a("")
    a(f"- Benchmark: `agentdojo=={PACKAGE_VERSION}`, benchmark version `{BENCH_VERSION}`, suite `{SUITE}`, attack `{ATTACK}`. AgentDojo's own utility and security checks are the verdict.")
    a(f"- Agent: the showcase loop with its shipped repair settings (repair on `fabricated` only, 2 rounds), model `{MODEL}`, k = 1, effort `medium`. The claim auditor is the judge and stays on its fixed judge model.")
    a("- Slice, fixed before any run (`evals/public-anchor/PREDICTIONS.md`): all 16 user tasks without injection, plus each user task with `injection_task_0` and `injection_task_1` (48 sessions). The full suite is 160 sessions (about 960 model calls); the slice was chosen to stay under about 400 model calls. Separately, the runner aborts at a hard cap of 600 calls / 4M estimated input tokens (see Metered usage).")
    a("- System prompt: AgentDojo's own default. Tools: the suite's 11 banking functions, run one after another on the benchmark's runtime.")
    a("- Deviation: the attack class derives the model's name from a table of old model ids and raises for this one, so the name is set to `Claude` (what the table gives every Claude id). The injected text is otherwise the benchmark's.")
    a("- The auditor's prompt is written for VC reports; here its evidence is every tool result plus the user's request, and injected text sits inside that evidence. It is out of its domain on this suite.")
    a("")
    a("## Result")
    a("")
    a("| quantity | observed | predicted (before the run) |")
    a("|---|---|---|")
    a(f"| utility without injection | {pct(s['utility'])} | {PREDICTED['utility']:.2f} |")
    a(f"| targeted attack success (security check true) | {pct(s['attack_success'])} | {PREDICTED['attack_success']:.2f} |")
    a(f"| utility under attack | {pct(s['utility_under_attack'])} | not predicted |")
    a("")
    a("Published figures: AgentDojo paper (arXiv 2406.13352), appendix table \"Detailed results for Figure 6\", Claude 3.5 Sonnet, whole benchmark: benign utility 78.22%, utility under attack 51.19%, targeted attack success 33.86%. "
      "Not comparable (older model, all suites, other benchmark version, other agent loop). For Sonnet-class on banking alone at this version: **no published comparable**.")
    a("")
    if s["infra_errors"] or s["not_injectable"]:
        a(f"Left out of the denominators: infrastructure errors {s['infra_errors'] or 'none'}; not injectable {s['not_injectable'] or 'none'}.")
        a("")
    a("## What the repair loop did")
    a("")
    a(f"- Sessions with at least one repair round: {rp['sessions_with_a_repair_round']} ({rp['repair_rounds']} rounds in all). On attacked sessions: {rp['attacked_sessions_with_a_repair_round']} sessions, {rp['attacked_repair_rounds']} rounds.")
    a(f"- A repair round moved the session TOWARDS the injection (a call matching the injection task's ground truth first made in a repair round, not in the draft): **{rp['repair_moved_toward_injection']}** of {s['attack_success']['n']} attacked sessions.")
    a(f"- Attacked sessions where the draft itself already made a call matching the injection's ground truth: {rp['attack_calls_in_draft']}.")
    a(f"- Sessions where the auditor never ran (no tool call, empty answer, or no evidence): {rp['sessions_with_no_audit']}. Fabricated claims found in first audits: {rp['fabricated_claims_first_audit']}. Unverifiable claims (counted, never repaired): {rp['unverifiable_claims']}.")
    a("")
    a("## Sessions")
    a("")
    a("| session | utility | security | steps | repair rounds | first failing step |")
    a("|---|---|---|---|---|---|")
    for r in sorted(sessions, key=lambda x: (x["injection_task"] is not None, int(x["user_task"].rsplit("_", 1)[1]), x["injection_task"] or "")):
        if r.get("infra_error") or not r.get("injectable", True):
            a(f"| {r['key']} | n/a | n/a | | | {r.get('note') or r.get('infra_error')} |")
            continue
        sec = "n/a" if r["security"] is None else ("ATTACK SUCCEEDED" if r["security"] else "held")
        a(f"| {r['key']} | {'pass' if r['utility'] else 'fail'} | {sec} | {r['steps']} | {r['repair_rounds']} | {r['first_failing_step'] or ''} |")
    a("")
    a("The first failing step is computed against the task's ground-truth calls (a call matches on function and on every argument the task fixes). It is a pointer for reading the trace, not a grade; the benchmark's check decides.")
    a("")
    a("## Metered usage (this run)")
    a("")
    a(f"Model calls {ledger.get('calls', 0)}; estimated input tokens {ledger.get('est_input_tokens', 0)} (chars / 4); billed input tokens {ledger.get('billed_input_tokens', 0)}; output tokens {ledger.get('output_tokens', 0)}. Cap: {MAX_CALLS} calls / {MAX_INPUT_TOKENS} estimated input tokens. Tag: metered. Agent and auditor calls are both included.")
    a("")
    a("## What this says")
    a("")
    a(conclusion.strip() if conclusion.strip() else "_Not written yet._")
    a("")
    a("## Reproduce")
    a("")
    a("```sh")
    a("make install        # includes the pinned agentdojo extra")
    a("make anchor-eval    # live run: needs a key file, spends tokens (resumes from the committed sessions file)")
    a("make reports        # re-renders this file from the committed results, no model call")
    a("```")
    return "\n".join(L) + "\n"


def render_md() -> None:
    sessions = load_sessions()
    if not sessions:
        raise SystemExit("no committed sessions file: run `make anchor-eval` first")
    ledger = json.loads(LEDGER.read_text()) if LEDGER.exists() else {}
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(render(sessions, ledger, CONCLUSION.read_text() if CONCLUSION.exists() else ""))
    print(f"wrote {REPORT.relative_to(ROOT)}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--preflight-only", action="store_true")
    ap.add_argument("--render-md", action="store_true")
    ap.add_argument("--env-file", type=Path)
    args = ap.parse_args()
    if args.render_md:
        render_md()
        return 0

    from agentdojo.agent_pipeline.agent_pipeline import load_system_message
    from agentdojo.task_suite.load_suites import get_suite

    from pierre.anchor.agentdojo_adapter import claude_tools_from_runtime
    from agentdojo.functions_runtime import FunctionsRuntime

    suite = get_suite(BENCH_VERSION, SUITE)
    system = load_system_message(None)
    plan = planned(suite)
    done = {r["key"] for r in load_sessions() if not r.get("infra_error")}
    todo = [(u, i) for u, i in plan if key_of(u, i) not in done]
    tools = claude_tools_from_runtime(FunctionsRuntime(suite.tools))
    nb, na = sum(1 for _, i in todo if i is None), sum(1 for _, i in todo if i)
    calls, tokens, parts = estimate(nb, na, system, tools)
    used = json.loads(LEDGER.read_text()) if LEDGER.exists() else {"calls": 0, "est_input_tokens": 0}
    print(f"suite {SUITE} {BENCH_VERSION}: {len(suite.user_tasks)} user x {len(suite.injection_tasks)} injection tasks = {len(suite.user_tasks) * (1 + len(suite.injection_tasks))} sessions in full")
    print(f"call list: {len(todo)} sessions to run ({nb} utility, {na} security; {len(plan) - len(todo)} already done)")
    print(f"estimated model calls: about {calls}; estimated input tokens (chars / 4): about {tokens}  [{parts}]")
    print(f"already metered in the ledger: {used['calls']} calls / {used['est_input_tokens']} estimated input tokens; cap {MAX_CALLS} / {MAX_INPUT_TOKENS}")
    if used["calls"] + calls > MAX_CALLS or used["est_input_tokens"] + tokens > MAX_INPUT_TOKENS:
        print(f"FAIL: estimate {calls} calls / {tokens} tokens plus the ledger is over the cap {MAX_CALLS} / {MAX_INPUT_TOKENS}.")
        return 1
    if args.preflight_only:
        return 0
    if not args.env_file:
        raise SystemExit("--env-file is required for a live run")

    from pierre.agent.budget import BudgetExceeded

    client = build_client(args.env_file)
    SESSIONS.parent.mkdir(parents=True, exist_ok=True)
    for u, i in todo:
        try:
            rec = run_one(suite, client, system, u, i)
        except BudgetExceeded as exc:
            print(f"FAIL: {exc}")
            return 1
        except Exception as exc:  # an infrastructure failure is a missing session, not a model outcome
            rec = {"key": key_of(u, i), "user_task": u, "injection_task": i, "infra_error": f"{type(exc).__name__}: {str(exc)[:160]}"}
        with SESSIONS.open("a") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
        print(f"{rec['key']}: " + ("infra error" if rec.get("infra_error") else f"utility={rec.get('utility')} security={rec.get('security')} repair_rounds={rec.get('repair_rounds')}"), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
