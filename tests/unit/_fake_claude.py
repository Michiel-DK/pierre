"""A scripted stand-in for the Anthropic client, for tests. It is a test double, not a recording."""
from __future__ import annotations

import copy
from typing import Any, Dict, List


def text(t: str) -> Dict[str, Any]:
    return {"type": "text", "text": t}


def tool_use(id_: str, name: str, **args: Any) -> Dict[str, Any]:
    return {"type": "tool_use", "id": id_, "name": name, "input": args}


def message(content: List[Dict[str, Any]], stop_reason: str = "end_turn") -> Dict[str, Any]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-test",
        "content": content,
        "stop_reason": stop_reason,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


class FakeClient:
    """Serves scripted responses in order; with ``repeat_last`` it serves the last one forever."""

    def __init__(self, responses: List[Dict[str, Any]], repeat_last: bool = False) -> None:
        self.responses, self.repeat_last = list(responses), repeat_last
        self.requests: List[Dict[str, Any]] = []
        self.messages = self

    def create(self, **kwargs: Any) -> Dict[str, Any]:
        self.requests.append(copy.deepcopy(kwargs))
        i = len(self.requests) - 1
        if i >= len(self.responses):
            if not self.repeat_last:
                raise AssertionError(f"unscripted call {i + 1}")
            i = len(self.responses) - 1
        return copy.deepcopy(self.responses[i])


class CountingDispatcher:
    """A dispatcher over plain functions, for loop tests that must not depend on the firm data."""

    def __init__(self, functions: Dict[str, Any]) -> None:
        from pierre.agent._dispatch_dedup import CallDeduper
        from pierre.agent.tools import ToolDispatcher

        self.calls: List[tuple] = []

        def wrap(name: str, fn: Any) -> Any:
            def run(**kw: Any) -> Any:
                self.calls.append((name, kw))
                return fn(**kw)

            return run

        self.inner = ToolDispatcher({n: wrap(n, f) for n, f in functions.items()}, CallDeduper())

    def reset_turn(self) -> None:
        self.inner.reset_turn()

    def execute_parallel(self, calls: Any) -> Any:
        return self.inner.execute_parallel(calls)

    @property
    def dedup_hits(self) -> int:
        return self.inner.dedup_hits
