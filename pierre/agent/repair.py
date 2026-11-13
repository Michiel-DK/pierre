"""Draft, audit, repair: a session whose answers are fact-checked by code between model calls.

The model never decides whether to be audited. After each draft, code runs ``audit_fn``; ``fabricated`` claims
are sent back as a new user message (the history is append-only) and the answer is audited again, until it is
clean or the round cap is reached. ``unverifiable`` claims are counted and reported, never repaired.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from pierre.agent.loop import Dispatcher, LoopConfig, LoopResult, run_loop
from pierre.agent.prompts import repair_message

REPAIR_ON: Tuple[str, ...] = ("fabricated",)
MAX_REPAIR_ROUNDS = 2
REPAIR_ITERATIONS = 4

# audit_fn(answer, tool_calls, tool_results) -> {"verified": n, "fabricated": n, "unverifiable": n,
#   "claims": [{"claim_text", "claim_type", "verdict", "evidence"}], "error": str | None, "usage": {...}}
AuditFn = Callable[[str, List[Dict[str, Any]], List[Any]], Dict[str, Any]]


@dataclass
class TurnResult:
    answer: str = ""
    content: List[Dict[str, Any]] = field(default_factory=list)
    steps: int = 0
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    tool_results: List[Any] = field(default_factory=list)
    termination: Optional[str] = None
    rounds: List[Dict[str, Any]] = field(default_factory=list)
    auditor_calls: int = 0
    auditor_usage: List[Dict[str, Any]] = field(default_factory=list)
    dedup_hits: int = 0
    stop_reasons: List[str] = field(default_factory=list)
    usage: List[Dict[str, Any]] = field(default_factory=list)
    model: str = ""


class AgentSession:
    """One conversation. ``ask`` runs one user turn through draft, audit and repair."""

    def __init__(
        self,
        client: Any,
        cfg: LoopConfig,
        dispatcher: Dispatcher,
        audit_fn: Optional[AuditFn],
        repair_on: Sequence[str] = REPAIR_ON,
        max_rounds: int = MAX_REPAIR_ROUNDS,
    ) -> None:
        self.client, self.cfg, self.dispatcher, self.audit_fn = client, cfg, dispatcher, audit_fn
        self.repair_on, self.max_rounds = tuple(repair_on), max_rounds
        self.messages: List[Dict[str, Any]] = []
        self._calls: List[Dict[str, Any]] = []  # every tool call of the session so far: the audit corpus
        self._results: List[Any] = []

    def _audit(self, answer: str, turn: TurnResult) -> Optional[Dict[str, Any]]:
        """None when there is nothing to audit against (no tool call in the session yet): never audit an empty corpus."""
        if self.audit_fn is None or not answer or not self._calls:
            return None
        turn.auditor_calls += 1
        audit = self.audit_fn(answer, list(self._calls), list(self._results))
        if audit.get("usage"):
            turn.auditor_usage.append(audit["usage"])
        return audit

    def _absorb(self, turn: TurnResult, run: LoopResult) -> None:
        turn.steps += run.steps
        turn.tool_calls += run.tool_calls
        turn.tool_results += run.tool_results
        turn.stop_reasons += run.stop_reasons
        turn.usage += run.usage
        turn.model = run.model or turn.model
        self._calls += run.tool_calls
        self._results += run.tool_results

    def ask(self, text: str) -> TurnResult:
        turn = TurnResult()
        self.messages.append({"role": "user", "content": text})
        self.dispatcher.reset_turn()

        run = run_loop(self.client, self.cfg, self.messages, self.dispatcher)
        self._absorb(turn, run)
        turn.answer, turn.content, turn.termination = run.text, run.content, run.termination
        audit = self._audit(run.text, turn) if run.termination is None else None
        turn.rounds.append({"round": 0, "answer": run.text, "audit": _public(audit), "steps": run.steps})

        for n in range(1, self.max_rounds + 1):
            bad = _repair_claims(audit, self.repair_on)
            if turn.termination or not bad:
                break
            self.messages.append({"role": "user", "content": repair_message(bad)})
            run = run_loop(self.client, self.cfg, self.messages, self.dispatcher, REPAIR_ITERATIONS)
            self._absorb(turn, run)
            if run.termination:
                turn.termination = run.termination
                turn.rounds.append({"round": n, "answer": run.text, "audit": None, "steps": run.steps})
                break
            turn.answer, turn.content = run.text, run.content
            audit = self._audit(run.text, turn)
            turn.rounds.append({"round": n, "answer": run.text, "audit": _public(audit), "steps": run.steps})
        else:
            if _repair_claims(audit, self.repair_on):
                turn.termination = "repair_cap"
        turn.dedup_hits = self.dispatcher.dedup_hits
        return turn


def _repair_claims(audit: Optional[Dict[str, Any]], repair_on: Sequence[str]) -> List[Dict[str, Any]]:
    if not audit or audit.get("error"):
        return []
    return [c for c in audit["claims"] if c["verdict"] in repair_on]


def _public(audit: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if audit is None:
        return None
    keep = {k: audit[k] for k in ("verified", "fabricated", "unverifiable", "claims") if k in audit}
    if audit.get("error"):
        keep["error"] = audit["error"]
    return keep
