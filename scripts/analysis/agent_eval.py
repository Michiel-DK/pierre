"""The live agentic eval of the pierre firm agent, graded by wilco.

    python scripts/analysis/agent_eval.py run         # live: every case x k x model, one trace per session (needs ANTHROPIC_API_KEY, spends tokens)
    python scripts/analysis/agent_eval.py grade       # wilco grade over the committed traces (needs `make install-eval`; no model, no key)
    python scripts/analysis/agent_eval.py render-md   # re-render docs/calibration/agent_eval_report.md (stdlib only; no model, no key, no wilco)

`make agent-eval` runs the three in that order; `make reports` runs only the last. Before the first call, `run` prints the call
count and the estimated input tokens and refuses to start if the estimate is over the cap (the estimate x 1.5). The cap is
enforced at runtime by the budget ledger. Tokens and call counts only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pierre.agent import eval_report  # noqa: E402
from pierre.agent.eval_run import MODELS, estimate, is_complete, plan, run_all  # noqa: E402

DATA = ROOT / "data" / "agent_eval"
TRACES = DATA / "traces"
GRADES = DATA / "grades"
SUMMARY = DATA / "run_summary.json"
CASES = ROOT / "evals" / "pierre-firm-agent" / "cases.json"
EXAM_DIR = CASES.parent
REPORT = ROOT / "docs" / "calibration" / "agent_eval_report.md"
TASK = "pierre-firm-agent"
DEFAULT_LEDGER = Path.home() / ".cache" / "pierre-agent-eval" / "ledger.json"


def pinned_wilco_sha() -> str:
    m = re.search(r"wilco-exams @ git\+https://github\.com/Michiel-DK/wilco@([0-9a-f]{40})", (ROOT / "pyproject.toml").read_text())
    if not m:
        raise SystemExit("pyproject.toml has no 40-character wilco pin in the [eval] extra")
    return m.group(1)


def cmd_run(args: argparse.Namespace) -> int:
    from pierre.agent.factory import load_cases

    cases = load_cases()
    review = json.loads(CASES.read_text())["review"]["status"]
    if review != "REVIEWED":
        print(f"FAIL: the case file's review status is {review!r}, not 'REVIEWED'.")
        return 1
    sessions = plan(cases, MODELS)
    est = estimate(cases, sessions)
    print(f"call list: {est['sessions']} sessions, {est['turns']} turns, about {est['calls']} model calls")
    print(f"estimated input tokens (chars / 4, resent history counted on every call): about {est['est_input_tokens']:,}")
    print(f"cap = estimate x 1.5: {est['cap_calls']} calls / {est['cap_input_tokens']:,} estimated input tokens")
    ledger = Path(os.environ.setdefault("PIERRE_BUDGET_LEDGER", str(DEFAULT_LEDGER)))
    os.environ["PIERRE_BUDGET_MAX_CALLS"] = str(est["cap_calls"])
    os.environ["PIERRE_BUDGET_MAX_INPUT_TOKENS"] = str(est["cap_input_tokens"])
    used = json.loads(ledger.read_text()) if ledger.exists() else {"calls": 0, "est_input_tokens": 0}
    print(f"ledger {ledger.name}: {used['calls']} calls / {used['est_input_tokens']:,} estimated input tokens already metered")
    from pierre.agent.factory import case_inputs

    todo = [x for x in sessions if not is_complete(x.path(TRACES), len(case_inputs(cases[x.case_id])))]
    left = estimate(cases, todo)
    print(f"still to run (sessions without a complete trace): {left['sessions']} sessions, about {left['calls']} calls, about {left['est_input_tokens']:,} estimated input tokens")
    if used["calls"] + left["calls"] > est["cap_calls"] or used["est_input_tokens"] + left["est_input_tokens"] > est["cap_input_tokens"]:
        print(f"FAIL: the estimate for what is left ({left['calls']} calls / {left['est_input_tokens']:,} tokens) plus what the ledger holds is over the cap.")
        return 1
    if args.preflight_only:
        return 0

    from pierre.agent.budget import BudgetExceeded, client_from_env

    client = client_from_env()
    TRACES.mkdir(parents=True, exist_ok=True)
    done = {"n": 0}

    def on_done(s, results):
        done["n"] += 1
        terms = [r.termination for r in results]
        print(f"[{done['n']}] {s.model} {s.case_id} r{s.repeat}: {len(results)} turns, termination {terms}", flush=True)

    try:
        counts = run_all(client, cases, sessions, TRACES, workers=args.workers, on_done=on_done)
        status = "complete"
    except BudgetExceeded as exc:
        counts, status = {"ran": done["n"], "skipped_complete": 0}, f"ABORTED at the cap: {exc}"
        print(status)
    metered = json.loads(ledger.read_text()) if ledger.exists() else {}
    summary = json.loads(SUMMARY.read_text()) if SUMMARY.exists() else {"runs": []}
    summary.update({"estimate": est, "metered_ledger": metered, "wilco_commit": pinned_wilco_sha(), "workers": args.workers})
    summary["runs"].append({"at": datetime.now(timezone.utc).isoformat(), "status": status, **counts})
    SUMMARY.write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")
    print(f"metered: {metered.get('calls')} calls, {metered.get('est_input_tokens')} estimated input tokens, "
          f"{metered.get('billed_input_tokens')} billed input tokens, {metered.get('output_tokens')} output tokens")
    return 0 if status == "complete" else 1


def _wilco_run(root: Path, trace_file: Path, label: str) -> dict:
    code = "import sys; from wilco_cli import main; sys.exit(main())"
    cmd = [sys.executable, "-c", code, "grade", TASK, "--traces", str(trace_file), "--exam-dir", str(EXAM_DIR), "--label", label, "--strict"]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        raise SystemExit(f"wilco grade refused or failed (exit {proc.returncode}): {proc.stderr.strip()}")
    return json.loads((root / "results" / "external" / f"{TASK}__trace__{label}.json").read_text())


def cmd_grade(_: argparse.Namespace) -> int:
    try:
        import wilco_cli
    except ImportError:
        raise SystemExit("wilco is not installed: run `make install-eval` (it installs the pinned commit as an editable checkout).")
    root = Path(wilco_cli.__file__).resolve().parent
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    if head != pinned_wilco_sha():
        raise SystemExit(f"the wilco checkout is at {head or 'unknown'}, the pin is {pinned_wilco_sha()}: run `make install-eval`.")
    GRADES.mkdir(parents=True, exist_ok=True)
    traces = eval_report.load_traces(TRACES)
    for model in sorted(traces):
        wrong = {r["model"] for recs in traces[model].values() for r in recs if r["model"] != model}
        if wrong:
            raise SystemExit(f"{model}: records carry another model string {sorted(wrong)}; the grade would mix models")
        with tempfile.TemporaryDirectory() as tmp:
            for label, text in ((model, eval_report.concat_traces(TRACES, model)), (f"{model}-draft", eval_report.draft_trace(TRACES, model))):
                f = Path(tmp) / f"{label}.jsonl"
                f.write_text(text, encoding="utf-8")
                raw = _wilco_run(root, f, label)
                out = eval_report.slim_grade(raw, text, label)
                (GRADES / (f"{model}.draft.json" if label.endswith("-draft") else f"{model}.json")).write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    return 0


def render_text() -> str:
    return eval_report.render(DATA, CASES, pinned_wilco_sha())


def cmd_render(_: argparse.Namespace) -> int:
    REPORT.write_text(render_text(), encoding="utf-8")
    print(f"wrote {REPORT}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--preflight-only", action="store_true")
    r.add_argument("--workers", type=int, default=3)
    r.set_defaults(fn=cmd_run)
    sub.add_parser("grade").set_defaults(fn=cmd_grade)
    sub.add_parser("render-md").set_defaults(fn=cmd_render)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
