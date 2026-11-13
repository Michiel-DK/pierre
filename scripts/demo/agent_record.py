"""Record the demo sessions from real model calls: `make record-agent` (needs ANTHROPIC_API_KEY, spends tokens).

Each session is one case run live on the model, with every request/response pair written to
tests/fixtures/agent_recordings/<case>.jsonl and its trace to <case>.trace.jsonl. Nothing is ever written by hand.
Before the first call it prints the size of the call list and the estimated input tokens (chars / 4).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pierre.agent.budget import client_from_env  # noqa: E402
from pierre.agent.factory import RECORDINGS_DIR, case_inputs, load_cases, run_case, runtime_stamp, session_id  # noqa: E402
from pierre.agent.loop import DEFAULT_EFFORT, DEFAULT_MODEL  # noqa: E402
from pierre.agent.prompts import SYSTEM_PROMPT  # noqa: E402
from pierre.agent.registry import claude_tools  # noqa: E402
from pierre.agent.replay import RecordingClient  # noqa: E402
from pierre.agent.trace import TraceWriter  # noqa: E402

# Train cases only, so the held-out split stays unseen.
RECORD_CASES = [
    "hq-and-ticket-quillmoor",
    "raise-fit-above-max",
    "tool-failure-timeout",
    "overclaim-pitch-kestrabel",
    "recent-climate-deals",
    "shortlist-swap-three-turns",
    "saved-list-from-results",
    "cross-turn-consistency-lumary",
]
EST_CALLS_PER_TURN = 6  # 3 draft calls, 1 audit, 0.65 repair rounds of 3 calls
EST_INPUT_TOKENS_PER_TURN = 40_000


def preflight(case_ids: list[str]) -> tuple[int, int]:
    cases = load_cases()
    turns = sum(len(case_inputs(cases[c])) for c in case_ids)
    later = sum(len(case_inputs(cases[c])) - 1 for c in case_ids)
    calls = turns * EST_CALLS_PER_TURN
    tokens = turns * EST_INPUT_TOKENS_PER_TURN + later * 6_000
    prefix = (len(SYSTEM_PROMPT) + len(json.dumps(claude_tools()))) // 4
    print(f"call list: {len(case_ids)} sessions, {turns} turns, about {calls} model calls")
    print(f"estimated input tokens (chars / 4, resent history counted on every call): about {tokens}")
    print(f"  of which the tools + system prefix is about {prefix} tokens per agent call")
    return calls, tokens


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cases", default=",".join(RECORD_CASES))
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--preflight-only", action="store_true")
    args = ap.parse_args()
    ids = [c for c in args.cases.split(",") if c]
    calls, tokens = preflight(ids)

    max_calls, max_tokens = int(os.environ.get("PIERRE_BUDGET_MAX_CALLS", 0)), int(os.environ.get("PIERRE_BUDGET_MAX_INPUT_TOKENS", 0))
    ledger = Path(os.environ["PIERRE_BUDGET_LEDGER"]) if os.environ.get("PIERRE_BUDGET_LEDGER") else None
    if ledger and max_calls:
        used = json.loads(ledger.read_text()) if ledger.exists() else {"calls": 0, "est_input_tokens": 0}
        left_calls, left_tokens = max_calls - used["calls"], max_tokens - used["est_input_tokens"]
        print(f"budget left: {left_calls} calls / {left_tokens} estimated input tokens (cap {max_calls} / {max_tokens})")
        if calls > left_calls or tokens > left_tokens:
            print(f"FAIL: estimate {calls} calls / {tokens} tokens is over what is left of the cap.")
            return 1
    if args.preflight_only:
        return 0

    live = client_from_env()
    cases = load_cases()
    log = RECORDINGS_DIR / "record_log.jsonl"
    for cid in ids:
        rec_path = RECORDINGS_DIR / f"{cid}.jsonl"
        header = {
            "model": args.model,
            "effort": DEFAULT_EFFORT,
            "sdk_version": runtime_stamp(args.model)["sdk"],
            "recorded_on": date.today().isoformat(),
            "case_id": cid,
        }
        client = RecordingClient(live, rec_path, header)
        case = cases[cid]
        writer = TraceWriter(
            RECORDINGS_DIR / f"{cid}.trace.jsonl", session_id(cid, args.model), cid, split=case.get("split"), runtime=runtime_stamp(args.model)
        )
        results = run_case(client, case, writer, args.model)
        n_calls = len(rec_path.read_text().splitlines()) - 1
        with log.open("a") as fh:
            fh.write(json.dumps({"session": cid, "recorded_on": header["recorded_on"], "calls": n_calls, "sha256": hashlib.sha256(rec_path.read_bytes()).hexdigest(), "argv": sys.argv, "recorded_at": datetime.now(timezone.utc).isoformat()}, sort_keys=True) + "\n")
        print(f"recorded {cid}: {len(results)} turns, {n_calls} calls, termination {[r.termination for r in results]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
