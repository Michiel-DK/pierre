"""Run the showcase loop over AgentDojo's tool functions and task suites.

AgentDojo's runner calls ``pipeline.query(prompt, runtime, env)`` and then grades the returned messages and the
mutated environment with the benchmark's own utility and security checks. This module is that one pipeline element:
it turns the runtime's functions into Claude tools, runs ``AgentSession`` (draft, audit, repair) over them, and hands
the conversation back in AgentDojo's message types. Nothing here grades; the benchmark does.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from agentdojo.agent_pipeline.base_pipeline_element import BasePipelineElement
from agentdojo.agent_pipeline.tool_execution import tool_result_to_str
from agentdojo.attacks.base_attacks import FixedJailbreakAttack
from agentdojo.attacks.important_instructions_attacks import ImportantInstructionsAttack
from agentdojo.functions_runtime import FunctionCall, FunctionsRuntime
from agentdojo.types import ChatMessage, text_content_block_from_string

from pierre.agent._dispatch_dedup import CallDeduper
from pierre.agent.factory import make_audit_fn
from pierre.agent.loop import LoopConfig
from pierre.agent.repair import AgentSession, TurnResult
from pierre.anchor.defences import HELD_PREFIX, ConfirmOnMutate, held_result, spotlight, system_for
from pierre.anchor.metrics import REPAIR_PREFIX

RESULT_CHAR_CAP = 3000  # per tool result shown to the claim auditor
NOT_RUN = "Not run:"


def claude_tools_from_runtime(runtime: FunctionsRuntime) -> List[Dict[str, Any]]:
    return [
        {"name": f.name, "description": f.description, "input_schema": f.parameters.model_json_schema()}
        for f in runtime.functions.values()
    ]


class AnchorDispatcher:
    """Runs one turn's tool calls on the benchmark's runtime, one after another (the tools mutate one shared environment).

    Results are the benchmark's own text rendering, so an injected string reaches the model as the benchmark meant.
    Every call is logged with the repair round it was made in (read off the session's message list).

    ``arm`` puts a defence between the model and the runtime (see ``pierre.anchor.defences``): ``confirm-on-mutate``
    checks each call against ``request`` and holds it without running it; ``spotlight`` wraps each text result.
    """

    def __init__(
        self, runtime: FunctionsRuntime, env: Any, deduper: Optional[CallDeduper] = None, arm: str = "none", request: str = ""
    ) -> None:
        self.runtime, self.env, self.deduper = runtime, env, deduper or CallDeduper()
        system_for(arm, "")  # rejects an unknown arm
        self.arm = arm
        self.guard = ConfirmOnMutate(request) if arm == "confirm-on-mutate" else None
        self.dedup_hits = 0
        self.log: List[Dict[str, Any]] = []
        self.messages: List[Dict[str, Any]] = []  # set to the session's message list

    def reset_turn(self) -> None:
        self.deduper.reset()

    def _round(self) -> int:
        texts = [m["content"] for m in self.messages if m["role"] == "user" and isinstance(m["content"], str)]
        return sum(1 for t in texts if t.startswith(REPAIR_PREFIX))

    def _run(self, name: str, args: Dict[str, Any]) -> Any:
        if self.guard is not None:
            hold, reason = self.guard.check(name, args)
            if hold:
                return held_result(name, reason)
        result, error = self.runtime.run_function(self.env, name, args)
        out = {"error": error} if error else tool_result_to_str(result)
        if self.guard is not None:
            self.guard.observe(out)
        return spotlight(out) if self.arm == "spotlight" else out

    def execute_parallel(self, calls: List[Tuple[str, Dict[str, Any]]]) -> List[Any]:
        out = []
        for name, args in calls:
            seen, result = self.deduper.get_or_run(name, args, lambda n=name, a=args: self._run(n, a))
            self.dedup_hits += int(seen)
            entry = {"tool": name, "args": args, "round": self._round(), "deduped": seen}
            if self.arm == "confirm-on-mutate":
                held = is_held(result)
                entry.update(held=held, hold_reason=result["error"] if held else "")
            self.log.append(entry)
            out.append(result)
        return out


def anchor_audit_context(prompt: str) -> Callable[[List[Dict[str, Any]], List[Any]], Dict[str, Any]]:
    """Map a session's tool results onto the claim auditor's evidence keys.

    The auditor's prompt is written for VC reports. Here every tool result (and the user's request) is handed over as an
    evidence entry, so a claim is checked against what the tools returned. Injected text is inside that evidence.
    """

    def build(calls: List[Dict[str, Any]], results: List[Any]) -> Dict[str, Any]:
        entries: List[Dict[str, Any]] = [{"source": "the user's request", "text": prompt}]
        errors: List[str] = []
        for call, result in zip(calls, results):
            head = f"{call['tool']}({json.dumps(call.get('args') or {}, sort_keys=True)})"
            if isinstance(result, dict) and "error" in result:
                errors.append(f"{head}: {result['error']}")
            else:
                entries.append({"source": head, "returned": str(result)[:RESULT_CHAR_CAP]})
        return {"firm_profiles": entries, "tool_errors": errors}

    return build


def is_held(result: Any) -> bool:
    return isinstance(result, dict) and str(result.get("error", "")).startswith(HELD_PREFIX)


class ShowcasePipeline(BasePipelineElement):
    """AgentDojo pipeline element backed by the showcase loop. One ``query`` is one session; ``last`` keeps its record.

    ``client`` drives the agent; ``audit_client`` (default: the same) drives the claim auditor, which stays on its
    fixed judge model whatever the agent runs on. ``arm`` is the defence; ``none`` is the loop as shipped.
    ``loop_overrides`` are extra ``LoopConfig`` fields (a local model's deadline, say).
    """

    def __init__(
        self, client: Any, system: str, model: str, arm: str = "none", audit_client: Any = None, loop_overrides: Optional[Dict[str, Any]] = None
    ) -> None:
        self.client, self.model, self.arm = client, model, arm
        self.system = system_for(arm, system)
        self.audit_client = audit_client if audit_client is not None else client
        self.loop_overrides = dict(loop_overrides or {})
        self.name = model
        self.last: Optional[Dict[str, Any]] = None

    def query(self, query: str, runtime: FunctionsRuntime, env: Any = None, messages: Sequence[ChatMessage] = (), extra_args: Optional[dict] = None):
        dispatcher = AnchorDispatcher(runtime, env, arm=self.arm, request=query)
        cfg = LoopConfig(system=self.system, tools=claude_tools_from_runtime(runtime), model=self.model, **self.loop_overrides)
        audit_fn = make_audit_fn(self.audit_client, context_fn=anchor_audit_context(query))
        session = AgentSession(self.client, cfg, dispatcher, audit_fn)
        dispatcher.messages = session.messages
        turn = session.ask(query)
        self.last = {"turn": turn, "log": dispatcher.log, "messages": session.messages}
        return query, runtime, env, to_chat_messages(self.system, session.messages), extra_args or {}


def _blocks(text: str) -> List[Any]:
    return [text_content_block_from_string(text)]


def to_chat_messages(system: str, messages: List[Dict[str, Any]]) -> List[ChatMessage]:
    """The session's Claude messages in AgentDojo's types. The result always ends with an assistant message.

    Only tool calls that were actually answered by a result count as calls: one closed with "Not run" was never executed.
    """
    run_ids = {
        b["tool_use_id"]
        for m in messages
        if m["role"] == "user" and isinstance(m["content"], list)
        for b in m["content"]
        if not str(b.get("content", "")).startswith('{"error": "' + NOT_RUN)
    }
    calls: Dict[str, FunctionCall] = {}
    out: List[Any] = [{"role": "system", "content": _blocks(system)}]
    for m in messages:
        if m["role"] == "user" and isinstance(m["content"], str):
            out.append({"role": "user", "content": _blocks(m["content"])})
        elif m["role"] == "assistant":
            text = "".join(b.get("text", "") for b in m["content"] if b.get("type") == "text")
            uses = [b for b in m["content"] if b.get("type") == "tool_use" and b["id"] in run_ids]
            for b in uses:
                calls[b["id"]] = FunctionCall(function=b["name"], args=dict(b.get("input") or {}), id=b["id"])
            out.append({"role": "assistant", "content": _blocks(text), "tool_calls": [calls[b["id"]] for b in uses] or None})
        else:
            for b in m["content"]:
                if b["tool_use_id"] not in calls:
                    continue
                err = b["content"] if b.get("is_error") else None
                out.append(
                    {"role": "tool", "tool_call": calls[b["tool_use_id"]], "content": _blocks(str(b["content"])), "tool_call_id": b["tool_use_id"], "error": err}
                )
    if out[-1]["role"] != "assistant":
        out.append({"role": "assistant", "content": _blocks(""), "tool_calls": None})
    return out


class ImportantInstructionsForClaude(ImportantInstructionsAttack):
    """The benchmark's ``important_instructions`` attack, text unchanged.

    The stock constructor derives the model's name from a table of old model ids and raises for ``claude-sonnet-5-5``.
    The table maps every Claude id to "Claude"; that string is set here directly. This is the one deviation. For a
    model the table does not know, ``model_name`` takes the table's own value for its ``local`` entry ("Local model").
    """

    def __init__(self, task_suite: Any, target_pipeline: BasePipelineElement, model_name: str = "Claude") -> None:
        FixedJailbreakAttack.__init__(self, self._JB_STRING, task_suite, target_pipeline)
        self.user_name = "Emma Johnson"
        self.model_name = model_name
