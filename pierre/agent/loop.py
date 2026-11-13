"""The tool-use loop: one user turn on Claude, tools dispatched in parallel, with caps and stop-reason handling.

Nothing here knows about the firm data. A caller passes the Claude tool list, a dispatcher (an object with
``reset_turn()``, ``execute_parallel(calls)`` and ``dedup_hits``) and a system prompt.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Tuple

MAX_TOOL_ITERATIONS = 8
DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "medium"
DEFAULT_MAX_TOKENS = 16000


class Dispatcher(Protocol):
    dedup_hits: int

    def reset_turn(self) -> None: ...

    def execute_parallel(self, calls: List[Tuple[str, Dict[str, Any]]]) -> List[Any]: ...


@dataclass(frozen=True)
class LoopConfig:
    system: str
    tools: List[Dict[str, Any]]
    model: str = DEFAULT_MODEL
    effort: str = DEFAULT_EFFORT
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_iterations: int = MAX_TOOL_ITERATIONS


@dataclass
class LoopResult:
    """What one run of the loop produced. ``termination`` is None when the model ended its turn."""

    text: str = ""
    content: List[Dict[str, Any]] = field(default_factory=list)
    steps: int = 0
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    tool_results: List[Any] = field(default_factory=list)
    termination: Optional[str] = None
    stop_reasons: List[str] = field(default_factory=list)
    usage: List[Dict[str, Any]] = field(default_factory=list)
    model: str = ""


def as_dict(response: Any) -> Dict[str, Any]:
    """A response as a plain dict, so history, recordings and replays are all the same kind of object."""
    return response if isinstance(response, dict) else response.to_dict()


def request_kwargs(cfg: LoopConfig, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    # Only tool_choice "auto": a forced choice is a 400 on these models. Thinking is left at its default (adaptive).
    return {
        "model": cfg.model,
        "max_tokens": cfg.max_tokens,
        "system": cfg.system,
        "tools": cfg.tools,
        "messages": messages,
        "tool_choice": {"type": "auto"},
        "output_config": {"effort": cfg.effort},
    }


def _result_block(tool_use_id: str, result: Any) -> Dict[str, Any]:
    block = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        # A tool that already returns text is passed through as is: JSON-quoting it would rewrite its newlines.
        "content": result if isinstance(result, str) else json.dumps(result, sort_keys=True, ensure_ascii=False, default=str),
    }
    if isinstance(result, dict) and "error" in result:
        block["is_error"] = True
    return block


def _close_unanswered(messages: List[Dict[str, Any]], content: List[Dict[str, Any]]) -> None:
    """An assistant message whose tool_use blocks were not run still needs a result for each, or the next request 400s."""
    blocks = [b for b in content if b.get("type") == "tool_use"]
    if blocks:
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": b["id"],
                        "content": json.dumps({"error": "Not run: the turn ended before this tool call."}),
                        "is_error": True,
                    }
                    for b in blocks
                ],
            }
        )


def run_loop(
    client: Any,
    cfg: LoopConfig,
    messages: List[Dict[str, Any]],
    dispatcher: Dispatcher,
    max_iterations: Optional[int] = None,
) -> LoopResult:
    """Call the model until it ends its turn or a cap is hit. ``messages`` is appended to, never edited."""
    cap = max_iterations or cfg.max_iterations
    out = LoopResult()
    for _ in range(cap):
        response = as_dict(client.messages.create(**request_kwargs(cfg, messages)))
        out.steps += 1
        stop = response.get("stop_reason")
        out.stop_reasons.append(str(stop))
        out.usage.append(response.get("usage") or {})
        out.model = response.get("model", out.model)
        content = response.get("content") or []
        out.content = content
        messages.append({"role": "assistant", "content": content})

        # The stop reason is read before any tool runs: a truncated tool input must never be executed.
        if stop in ("refusal", "max_tokens", "pause_turn"):
            out.termination = "unexpected_pause_turn" if stop == "pause_turn" else stop
            _close_unanswered(messages, content)
            return out
        uses = [b for b in content if b.get("type") == "tool_use"]
        if stop != "tool_use" or not uses:
            out.text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            return out

        results = dispatcher.execute_parallel([(b["name"], dict(b.get("input") or {})) for b in uses])
        out.tool_calls.extend({"tool": b["name"], "args": b.get("input") or {}} for b in uses)
        out.tool_results.extend(results)
        # All results of one response go back in ONE user message, in call order.
        messages.append({"role": "user", "content": [_result_block(b["id"], r) for b, r in zip(uses, results)]})
    out.termination = "iteration_cap"
    return out
