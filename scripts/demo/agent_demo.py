"""Replay two recorded agent sessions: `make demo-agent`. No key, no network.

The sessions were recorded from real model calls (see scripts/demo/agent_record.py); the data is the fictional
firm set in data/agent/firms.json. The replay refuses to run if a prompt, tool description or loop has changed.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pierre.agent.factory import case_inputs, load_cases, replay_case  # noqa: E402

SESSIONS = ["hq-and-ticket-quillmoor", "shortlist-swap-three-turns"]


def show(case_id: str) -> None:
    case = load_cases()[case_id]
    results, _ = replay_case(case_id)
    print(f"=== {case_id} ({len(results)} turn{'s' * (len(results) > 1)}) ===")
    for text, r in zip(case_inputs(case), results):
        print(f"\nUser: {text}")
        for call in r.tool_calls:
            print(f"  tool: {call['tool']} {call['args']}")
        for rd in r.rounds:
            audit = rd["audit"]
            if audit:
                print(f"  round {rd['round']}: audit {audit['verified']} verified, {audit['fabricated']} fabricated, {audit['unverifiable']} unverifiable")
        print(f"  steps {r.steps}, auditor calls {r.auditor_calls}, ended: {r.termination or 'end_turn'}")
        print(f"Agent: {r.answer}")


if __name__ == "__main__":
    for sid in SESSIONS:
        show(sid)
        print()
