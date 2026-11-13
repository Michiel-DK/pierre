"""Write one trace per session: one JSON line per turn, in the agent-trace record format graded by wilco."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from pierre.agent.backend import AS_OF
from pierre.agent.repair import TurnResult

SOURCE = "external:pierre"
TASK = "pierre-firm-agent"


class FixedClock:
    """A deterministic clock: each call returns the next second after ``start``, with an offset."""

    def __init__(self, start: str = f"{AS_OF}T09:00:00+00:00") -> None:
        self._t = datetime.fromisoformat(start)

    def __call__(self) -> str:
        value = self._t.isoformat()
        self._t += timedelta(seconds=1)
        return value


def record_id(source: str, session_id: str, turn: int, text: str) -> str:
    return hashlib.sha256(json.dumps([source, session_id, turn, text]).encode("utf-8")).hexdigest()


class TraceWriter:
    def __init__(
        self,
        path: Path,
        session_id: str,
        case_id: str,
        *,
        split: Optional[str] = None,
        repeat: int = 0,
        runtime: Optional[Dict[str, Any]] = None,
        clock: Optional[Callable[[], str]] = None,
        task: str = TASK,
    ) -> None:
        self.task = task
        self.path = Path(path)
        self.session_id, self.case_id, self.split, self.repeat = session_id, case_id, split, repeat
        self.runtime = runtime or {}
        self.clock = clock or FixedClock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("")

    def write_turn(self, turn: int, text: str, result: TurnResult, final_state: Optional[Dict[str, Any]] = None, meta_extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        record: Dict[str, Any] = {
            "record_id": record_id(SOURCE, self.session_id, turn, text),
            "session_id": self.session_id,
            "turn": turn,
            "ts": self.clock(),
            "source": SOURCE,
            "task": self.task,
            "case_id": self.case_id,
            "split": self.split,
            "input": text,
            "output_raw": result.content,
            "output_parsed": {"answer": result.answer},
            "tool_calls": result.tool_calls,
            "tool_results": result.tool_results,
            "model": result.model or "unknown",
            "provider": "anthropic",
            "runtime": self.runtime,
            "steps": max(result.steps, 1),
            # wilco force-fails a turn that carries `termination`. `repair_cap` is the auditor (a model) still
            # objecting after the last round, and the turn did answer, so it goes to meta and the deterministic
            # checks decide the verdict. Loop stops (iteration cap, max_tokens, refusal, crash) stay terminations.
            "termination": {"cause": result.termination} if result.termination and result.termination != "repair_cap" else None,
            "repeat": self.repeat,
            "verdict": None,
            "grader_hash": None,
            "failed_checks": [],
            "labels": [],
            "meta": {
                "rounds": result.rounds,
                "auditor_calls": result.auditor_calls,
                "dedup_hits": result.dedup_hits,
                "stop_reasons": result.stop_reasons,
                "usage": result.usage,
                "auditor_usage": result.auditor_usage,
            },
        }
        if meta_extra:
            record["meta"].update(meta_extra)
        if result.termination == "repair_cap":
            record["meta"]["repair_cap"] = True
        if final_state is not None:
            record["final_state"] = final_state
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
        return record
