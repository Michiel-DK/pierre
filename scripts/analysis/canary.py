"""The scheduled canary: five fixed held-out cases, live, one metrics row per run.

    python scripts/analysis/canary.py run                    # live: the cases in canary/cases.txt, k=1, claude-sonnet-5-5 (needs ANTHROPIC_API_KEY, spends tokens, capped)
    python scripts/analysis/canary.py run --full             # all 21 cases, k=1 (for when the model id or the SDK version changed)
    python scripts/analysis/canary.py run --preflight-only   # print the call list and the cap; no key, no call
    python scripts/analysis/canary.py estimate [--full]      # calls, input tokens and list-price cost of one run, counted from the committed 13 Nov traces

`make canary` and `make canary FULL=1` run the first two. A run appends one row to canary/metrics.jsonl (one per run date).
pierre has no users: this is scheduled traffic, not organic traffic.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pierre.agent import canary  # noqa: E402
from pierre.agent.factory import load_cases  # noqa: E402

CANARY = ROOT / "canary"
CASE_LIST = CANARY / "cases.txt"
METRICS = CANARY / "metrics.jsonl"
RUNS = CANARY / "runs"
COMMITTED = ROOT / "data" / "agent_eval" / "traces" / canary.MODEL


def case_ids(cases: dict, full: bool) -> list:
    if full:
        return list(cases)
    ids = canary.read_case_list(CASE_LIST)
    canary.validate_case_list(ids, cases)
    return ids


def wilco_grader(date: str):
    """Grade a trace text with the pinned wilco, as ``agent_eval.py grade`` does (same pin check, same strict mode)."""
    from scripts.analysis import agent_eval
    from pierre.agent import eval_report
    import subprocess

    try:
        import wilco_cli
    except ImportError:
        raise SystemExit("wilco is not installed: run `make install-eval` (it installs the pinned commit as an editable checkout).")
    root = Path(wilco_cli.__file__).resolve().parent
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    if head != agent_eval.pinned_wilco_sha():
        raise SystemExit(f"the wilco checkout is at {head or 'unknown'}, the pin is {agent_eval.pinned_wilco_sha()}: run `make install-eval`.")

    def grade(text: str) -> dict:
        label = f"canary-{date}"
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / f"{label}.jsonl"
            f.write_text(text, encoding="utf-8")
            raw = agent_eval._wilco_run(root, f, label)
        return eval_report.slim_grade(raw, text, label)

    return grade


def cmd_run(args: argparse.Namespace) -> int:
    cases = load_cases()
    ids = case_ids(cases, args.full)
    b = canary.budget(cases, ids)
    print(f"call list: {b['sessions']} sessions, {b['turns']} turns, about {b['calls']} model calls ({'full run' if args.full else 'canary'}, k=1, {canary.MODEL})")
    print(f"estimated input tokens (chars / 4, resent history counted on every call): about {b['est_input_tokens']:,}")
    print(f"cap = estimate x 1.5: {b['cap_calls']} calls / {b['cap_input_tokens']:,} estimated input tokens")
    if args.preflight_only:
        return 0
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("FAIL: ANTHROPIC_API_KEY is not set: a live run needs a key in this process's environment.")
        return 1
    date = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if date in canary._dates(METRICS):  # before the ledger below is deleted: that day's ledger is a committed file
        print(f"FAIL: {METRICS.name} already has a row for {date} (one row per run date); nothing was run or deleted. Run it on a later date.")
        return 1
    grader = wilco_grader(date)  # refuse before spending when the grader is not ready
    ledger = RUNS / date / "ledger.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.unlink(missing_ok=True)  # the cap is per run
    os.environ["PIERRE_BUDGET_LEDGER"] = str(ledger)
    os.environ["PIERRE_BUDGET_MAX_CALLS"] = str(b["cap_calls"])
    os.environ["PIERRE_BUDGET_MAX_INPUT_TOKENS"] = str(b["cap_input_tokens"])

    from pierre.agent.budget import BudgetExceeded, client_from_env

    try:
        row = canary.run_canary(client_from_env(), cases, ids, RUNS, date, grader, METRICS, workers=args.workers)
    except BudgetExceeded as exc:
        print(f"ABORTED at the cap, no row written: {exc}")
        return 1
    print(json.dumps(row, sort_keys=True))
    return 0


def cmd_estimate(args: argparse.Namespace) -> int:
    cases = load_cases()
    ids = case_ids(cases, args.full)
    est = canary.estimate_from_traces(canary.load_records(COMMITTED, None if args.full else ids, repeat=0))
    b = canary.budget(cases, ids)
    print(f"one {'full' if args.full else 'canary'} run, counted from the committed 13 Nov {canary.MODEL} traces (repeat 0):")
    print(json.dumps(est, sort_keys=True))
    print(f"budget formula (the cap's basis): {b['calls']} calls, {b['est_input_tokens']:,} estimated input tokens (chars / 4)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--full", action="store_true")
    r.add_argument("--preflight-only", action="store_true")
    r.add_argument("--workers", type=int, default=3)
    r.add_argument("--date", help="run date, YYYY-MM-DD (default: today, UTC)")
    r.set_defaults(fn=cmd_run)
    e = sub.add_parser("estimate")
    e.add_argument("--full", action="store_true")
    e.set_defaults(fn=cmd_estimate)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
