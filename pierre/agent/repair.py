"""Draft, audit, repair: a session whose answers are fact-checked by code between model calls.

The model never decides whether to be audited. After each draft, code runs ``audit_fn``; ``fabricated`` claims
are sent back as a new user message (the history is append-only) and the answer is audited again, until it is
clean or the round cap is reached. ``unverifiable`` claims are counted and reported, never repaired.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from pierre.agent import cost as cost_mod
from pierre.agent import obs, timeouts
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
    auditor_model: Optional[str] = None  # the judge's model; the auditor is fixed, whatever model the agent runs on
    cost: Optional[Dict[str, Any]] = None  # tokens x prices.json; None = a model without a price row
    dedup_hits: int = 0
    stop_reasons: List[str] = field(default_factory=list)
    usage: List[Dict[str, Any]] = field(default_factory=list)
    model: str = ""
    # Wall clock, measured on every run; the trace writes it to meta only in live mode (replays stay byte-equal).
    latency_ms: Optional[float] = None
    model_latency_ms: List[float] = field(default_factory=list)
    auditor_latency_ms: List[float] = field(default_factory=list)


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
        self.conversation_id: Optional[str] = None  # the trace's session id, when a caller sets it; a span attribute
        self._turn_index = 0

    def _audit(self, answer: str, turn: TurnResult) -> Optional[Dict[str, Any]]:
        """None when there is nothing to audit against (no tool call in the session yet): never audit an empty corpus."""
        if self.audit_fn is None or not answer or not self._calls:
            return None
        turn.auditor_calls += 1
        with obs.span("audit", {"gen_ai.operation.name": "chat", "gen_ai.provider.name": obs.PROVIDER}) as sp:
            start = obs.monotonic()
            try:
                audit = self.audit_fn(answer, list(self._calls), list(self._results))
            except Exception as exc:  # noqa: BLE001 - only a timeout ends the turn; anything else propagates unchanged
                if not timeouts.is_timeout(exc):
                    raise
                sp.error("timeout")
                turn.termination = "timeout"
                return None
            finally:
                turn.auditor_latency_ms.append(obs.elapsed_ms(start))
            sp.update({"gen_ai.request.model": audit.get("model"), **obs.usage_attributes(audit.get("usage") or {})})
            sp.update(obs.audit_attributes(audit))
            if audit.get("error"):
                sp.error("audit_error")
        if audit.get("usage"):
            turn.auditor_usage.append(audit["usage"])
            turn.auditor_model = audit.get("model") or turn.auditor_model
        return audit

    def _absorb(self, turn: TurnResult, run: LoopResult) -> None:
        turn.steps += run.steps
        turn.tool_calls += run.tool_calls
        turn.tool_results += run.tool_results
        turn.stop_reasons += run.stop_reasons
        turn.usage += run.usage
        turn.model = run.model or turn.model
        turn.model_latency_ms += run.latency_ms
        self._calls += run.tool_calls
        self._results += run.tool_results

    def ask(self, text: str) -> TurnResult:
        index, self._turn_index = self._turn_index, self._turn_index + 1
        attrs = {
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.provider.name": obs.PROVIDER,
            "gen_ai.agent.name": obs.AGENT_NAME,
            "gen_ai.request.model": self.cfg.model,
            "gen_ai.conversation.id": self.conversation_id,
            "pierre.turn.index": index,
        }
        with obs.span(f"invoke_agent {obs.AGENT_NAME}", attrs) as sp:
            start = obs.monotonic()
            turn = self._ask(text)
            turn.latency_ms = obs.elapsed_ms(start)
            turn.cost = cost_mod.turn_cost(turn)
            last_audit = next((r["audit"] for r in reversed(turn.rounds) if r["audit"] is not None), None)
            sp.update({
                "pierre.turn.steps": turn.steps,
                "pierre.turn.rounds": len(turn.rounds),
                "pierre.turn.auditor_calls": turn.auditor_calls,
                "pierre.turn.dedup_hits": turn.dedup_hits,
                "pierre.turn.termination": turn.termination,
                "pierre.turn.cost.usd": turn.cost["total_usd"] if turn.cost else None,
                "gen_ai.response.model": turn.model or None,
                "gen_ai.usage.input_tokens": sum(int(u.get("input_tokens") or 0) for u in turn.usage),
                "gen_ai.usage.output_tokens": sum(int(u.get("output_tokens") or 0) for u in turn.usage),
                **obs.audit_attributes(last_audit),
            })
            if turn.termination and turn.termination != "repair_cap":
                sp.error(turn.termination)
        return turn

    def _ask(self, text: str) -> TurnResult:
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
            with obs.span("repair_round", {"pierre.repair.round": n, "pierre.repair.claims": len(bad)}) as sp:
                self.messages.append({"role": "user", "content": repair_message(bad)})
                run = run_loop(self.client, self.cfg, self.messages, self.dispatcher, REPAIR_ITERATIONS)
                self._absorb(turn, run)
                if run.termination:
                    turn.termination = run.termination
                    turn.rounds.append({"round": n, "answer": run.text, "audit": None, "steps": run.steps})
                    sp.error(run.termination)
                    break
                turn.answer, turn.content = run.text, run.content
                audit = self._audit(run.text, turn)
                turn.rounds.append({"round": n, "answer": run.text, "audit": _public(audit), "steps": run.steps})
                sp.update(obs.audit_attributes(audit))
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
