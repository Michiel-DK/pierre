"""Timeouts for model calls and tool calls. Stdlib only.

The SDK's own default (anthropic 2.8.0, ``_constants.py`` ``DEFAULT_TIMEOUT``) is 600 s with a 5 s connect timeout, and
httpx applies it to each phase (connect, write, read, pool), not to the call as a whole. So ``budget.client_from_env``
sets an explicit client timeout, and the agent loop puts a wall-clock deadline on each model call
(``call_with_deadline``): a server that keeps a connection busy cannot hold a turn open. A client built elsewhere (the
AgentDojo anchor builds its own) keeps the SDK default timeout and gets only the loop's deadline; the claim auditor's
call has no deadline from here.

A call that passes its deadline is abandoned, not killed: its thread is a daemon and finishes (or fails) on its own.
Anything it was going to do (a model call it billed, a tool that writes) may still happen; the turn ends regardless.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Optional

MODEL_TIMEOUT_S = 120.0  # one model call (non-streaming, max_tokens 16000); the client and the deadline use the same figure
CONNECT_TIMEOUT_S = 5.0  # the SDK's own connect default, kept explicit
TOOL_TIMEOUT_S = 30.0  # one tool call; the demo tools are in-memory and take milliseconds


class DeadlineExceeded(Exception):
    """A call ran past its deadline."""


class ToolTimeout(DeadlineExceeded):
    def __init__(self, tool: str, seconds: float) -> None:
        super().__init__(f"tool {tool} timed out after {seconds:g}s")
        self.tool, self.seconds = tool, seconds


def is_timeout(exc: BaseException) -> bool:
    """A deadline of ours, a builtin timeout, or the SDK's ``APITimeoutError`` (matched by name: this module stays SDK-free)."""
    return isinstance(exc, (DeadlineExceeded, TimeoutError)) or any(c.__name__ == "APITimeoutError" for c in type(exc).__mro__)


def call_with_deadline(fn: Callable[..., Any], seconds: Optional[float], *args: Any, **kwargs: Any) -> Any:
    """``fn(*args, **kwargs)``, or ``DeadlineExceeded`` after ``seconds``. ``None`` runs it inline with no deadline."""
    if seconds is None:
        return fn(*args, **kwargs)
    box: dict = {}

    def run() -> None:
        try:
            box["value"] = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - handed back to the caller's thread below
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True, name="pierre-deadline")
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        raise DeadlineExceeded(f"no result after {seconds:g}s")
    if "error" in box:
        raise box["error"]
    return box["value"]
