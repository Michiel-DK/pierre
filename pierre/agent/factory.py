"""Wire the generic loop to the demo firm tools and the claim auditor."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pierre.agent._audit_context import audit_context_from_turn
from pierre.agent.backend import InMemoryFirmStore
from pierre.agent.loop import DEFAULT_EFFORT, DEFAULT_MAX_TOKENS, DEFAULT_MODEL, LoopConfig
from pierre.agent.prompts import SYSTEM_PROMPT
from pierre.agent.registry import ToolContext, bind_tools, claude_tools
from pierre.agent.replay import ReplayClient
from pierre.agent.repair import AgentSession, AuditFn, TurnResult
from pierre.agent.tools import ToolDispatcher
from pierre.agent._dispatch_dedup import CallDeduper
from pierre.agent.trace import TraceWriter
from pierre.agents._claim_auditor import DEFAULT_AUDIT_MODEL, audit_report_claims


def make_audit_fn(client: Any, model: str = DEFAULT_AUDIT_MODEL, context_fn: Any = audit_context_from_turn) -> AuditFn:
    """An audit function over the session's tool results. Its calls are tagged ``auditor`` when the client can tag.

    The judge is fixed (``DEFAULT_AUDIT_MODEL``) whatever model the agent runs on, so scores across agent models
    are judged by the same auditor."""
    audit_client = client.with_role("auditor") if hasattr(client, "with_role") else client

    def audit(answer: str, calls: List[Dict[str, Any]], results: List[Any]) -> Dict[str, Any]:
        result = audit_report_claims(answer, context_fn(calls, results), audit_client, model=model)
        return {
            "verified": result.verified_count,
            "fabricated": result.fabricated_count,
            "unverifiable": result.unverifiable_count,
            "claims": [
                {"claim_text": a.claim_text, "claim_type": a.claim_type, "verdict": a.verdict, "evidence": a.evidence}
                for a in result.audits
            ],
            "error": result.error,
            "usage": {"input_tokens": result.input_tokens, "output_tokens": result.output_tokens},
        }

    return audit


def new_session(client: Any, store: Optional[InMemoryFirmStore] = None, model: str = DEFAULT_MODEL) -> AgentSession:
    store = store or InMemoryFirmStore()
    dispatcher = ToolDispatcher(bind_tools(ToolContext(store)), CallDeduper())
    cfg = LoopConfig(system=SYSTEM_PROMPT, tools=claude_tools(), model=model)
    session = AgentSession(client, cfg, dispatcher, make_audit_fn(client))
    session.store = store  # type: ignore[attr-defined]
    return session


def case_inputs(case: Dict[str, Any]) -> List[str]:
    return list(case["turns"]) if "turns" in case else [case["input"]]


def run_case(client: Any, case: Dict[str, Any], writer: TraceWriter, model: str = DEFAULT_MODEL) -> List[TurnResult]:
    """Run every turn of a case in one session and write its trace. The last turn also records the final state."""
    session = new_session(client, model=model)
    inputs = case_inputs(case)
    results = []
    for i, text in enumerate(inputs):
        result = session.ask(text)
        last = i == len(inputs) - 1
        writer.write_turn(i, text, result, session.store.final_state() if last else None)  # type: ignore[attr-defined]
        results.append(result)
    return results


CASES_PATH = Path(__file__).resolve().parents[2] / "evals" / "pierre-firm-agent" / "cases.json"
RECORDINGS_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "agent_recordings"


def load_cases() -> Dict[str, Dict[str, Any]]:
    return {c["id"]: c for c in json.loads(CASES_PATH.read_text())["cases"]}


def session_id(case_id: str, model: str, repeat: int = 0) -> str:
    return f"{case_id}__{model}__r{repeat}"


def replay_case(case_id: str, model: str = DEFAULT_MODEL) -> Tuple[List[TurnResult], Path]:
    """Replay a recorded session into a trace file next to a temp dir; returns the results and the trace path."""
    import tempfile

    client = ReplayClient(RECORDINGS_DIR / f"{case_id}.jsonl")
    path = Path(tempfile.mkdtemp()) / f"{case_id}.trace.jsonl"
    case = load_cases()[case_id]
    results = run_case(client, case, TraceWriter(path, session_id(case_id, model), case_id, split=case.get("split"), runtime=runtime_stamp(model)), model)
    client.assert_exhausted()
    return results, path


def runtime_stamp(model: str) -> Dict[str, Any]:
    import anthropic

    return {"sdk": anthropic.__version__, "effort": DEFAULT_EFFORT, "max_tokens": DEFAULT_MAX_TOKENS, "model_requested": model}
