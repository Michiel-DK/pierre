"""The live agentic eval: every case x repeats x models, one trace file per session.

Pure planning and session running; the CLI is scripts/analysis/agent_eval.py. A session that raises is written down as
a ``crash:<ExceptionName>`` termination (wilco refuses gaps, and a crash is a result), except ``BudgetExceeded``,
which aborts the whole run. A session whose trace file is complete is skipped, so a re-run never spends twice.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Sequence, Tuple

from pierre.agent.budget import BudgetExceeded
from pierre.agent.factory import case_inputs, new_session, runtime_stamp, session_id
from pierre.agent.repair import TurnResult
from pierre.agent.trace import TraceWriter

# (model, k). Sonnet at k=3 and Opus at k=1 (decided 13 Nov 2026, before the run).
MODELS: Tuple[Tuple[str, int], ...] = (("claude-sonnet-5-5", 3), ("claude-opus-5-5", 1))
CALLS_PER_TURN = 6.0  # pre-run per-turn estimate: 3 draft calls, 1 audit, about 0.65 repair rounds of 3 calls
INPUT_TOKENS_PER_TURN = 40_000
INPUT_TOKENS_PER_LATER_TURN = 6_000
CAP_FACTOR = 1.5


@dataclass(frozen=True)
class Session:
    case_id: str
    model: str
    repeat: int

    @property
    def sid(self) -> str:
        return session_id(self.case_id, self.model, self.repeat)

    def path(self, traces_dir: Path) -> Path:
        return Path(traces_dir) / self.model / f"{self.case_id}__r{self.repeat}.trace.jsonl"


def plan(cases: Dict[str, Dict[str, Any]], models: Sequence[Tuple[str, int]] = MODELS) -> List[Session]:
    """Repeat-major, so an abort at the cap leaves whole repeats of every model rather than a half-run model."""
    out: List[Session] = []
    for r in range(max(k for _, k in models)):
        for model, k in models:
            if r < k:
                out += [Session(cid, model, r) for cid in cases]
    return out


def estimate(cases: Dict[str, Dict[str, Any]], sessions: Sequence[Session]) -> Dict[str, int]:
    turns = sum(len(case_inputs(cases[s.case_id])) for s in sessions)
    later = sum(len(case_inputs(cases[s.case_id])) - 1 for s in sessions)
    calls = round(turns * CALLS_PER_TURN)
    tokens = turns * INPUT_TOKENS_PER_TURN + later * INPUT_TOKENS_PER_LATER_TURN
    return {
        "sessions": len(sessions), "turns": turns, "calls": calls, "est_input_tokens": tokens,
        "cap_calls": int(calls * CAP_FACTOR), "cap_input_tokens": int(tokens * CAP_FACTOR),
    }


def is_complete(path: Path, n_turns: int) -> bool:
    if not path.exists():
        return False
    lines = [x for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    return len(lines) == n_turns and json.loads(lines[-1]).get("turn") == n_turns - 1


def run_session(client: Any, case: Dict[str, Any], s: Session, traces_dir: Path, split: str | None = None) -> List[TurnResult]:
    """One session to its own trace file. Any non-budget exception becomes a crash termination on the turn it hit
    and on every later turn, so the file is never short."""
    path = s.path(traces_dir)
    writer = TraceWriter(path, s.sid, s.case_id, split=case.get("split", split), repeat=s.repeat, runtime=runtime_stamp(s.model))
    session = new_session(client, model=s.model)
    inputs = case_inputs(case)
    results: List[TurnResult] = []
    crashed: str | None = None
    for i, text in enumerate(inputs):
        last = i == len(inputs) - 1
        if crashed is None:
            try:
                result = session.ask(text)
            except BudgetExceeded:
                path.unlink(missing_ok=True)  # an incomplete file would look like a finished session
                raise
            except Exception as exc:  # noqa: BLE001 - a crash is a recorded result, not a reason to stop the run
                crashed = f"crash:{type(exc).__name__}"
                result = TurnResult(termination=crashed, model=s.model)
        else:
            result = TurnResult(termination=crashed, model=s.model)
        writer.write_turn(i, text, result, session.store.final_state() if last else None)  # type: ignore[attr-defined]
        results.append(result)
    return results


def run_all(
    client: Any,
    cases: Dict[str, Dict[str, Any]],
    sessions: Sequence[Session],
    traces_dir: Path,
    workers: int = 3,
    on_done: Callable[[Session, List[TurnResult]], None] | None = None,
) -> Dict[str, int]:
    todo = [s for s in sessions if not is_complete(s.path(traces_dir), len(case_inputs(cases[s.case_id])))]
    skipped = len(sessions) - len(todo)

    def one(s: Session) -> Tuple[Session, List[TurnResult]]:
        res = run_session(client, cases[s.case_id], s, traces_dir)
        if on_done:
            on_done(s, res)
        return s, res

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(one, s) for s in todo]
        try:
            for f in futures:
                f.result()
        except BudgetExceeded:
            for f in futures:
                f.cancel()
            raise
    return {"ran": len(todo), "skipped_complete": skipped}
