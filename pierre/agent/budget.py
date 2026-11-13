"""A call and token budget that is enforced at runtime and persists across processes in a ledger file."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional


class BudgetExceeded(RuntimeError):
    abort_run = True  # the claim auditor re-raises these instead of reporting an audit error


def estimate_input_tokens(kwargs: Dict[str, Any]) -> int:
    """chars / 4 of the whole request, history included: the estimate the budget is defined on."""
    return len(json.dumps(kwargs, ensure_ascii=False)) // 4


class BudgetedClient:
    """Wraps a real client. Every attempt, retries included, is counted before it is sent."""

    def __init__(
        self,
        inner: Any,
        ledger_path: Path,
        max_calls: int,
        max_input_tokens: int,
        retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
        retryable: tuple = (),
    ) -> None:
        self.inner, self.path = inner, Path(ledger_path)
        self.max_calls, self.max_input_tokens, self.retries = max_calls, max_input_tokens, retries
        self._sleep, self._retryable = sleep, retryable
        self.messages = self
        self._lock = threading.Lock()  # sessions may run on threads; the ledger is read-modify-write

    def _read(self) -> Dict[str, int]:
        if self.path.exists():
            return json.loads(self.path.read_text())
        return {"calls": 0, "est_input_tokens": 0, "billed_input_tokens": 0, "output_tokens": 0}

    def _write(self, ledger: Dict[str, int]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(ledger, sort_keys=True))

    def create(self, **kwargs: Any) -> Any:
        for attempt in range(self.retries + 1):
            est = estimate_input_tokens(kwargs)
            with self._lock:
                ledger = self._read()
                if ledger["calls"] + 1 > self.max_calls or ledger["est_input_tokens"] + est > self.max_input_tokens:
                    raise BudgetExceeded(
                        f"budget: next call would reach {ledger['calls'] + 1} calls / "
                        f"{ledger['est_input_tokens'] + est} estimated input tokens; "
                        f"cap is {self.max_calls} calls / {self.max_input_tokens} tokens"
                    )
                ledger["calls"] += 1
                ledger["est_input_tokens"] += est
                self._write(ledger)
            try:
                response = self.inner.messages.create(**kwargs)
            except self._retryable:
                if attempt == self.retries:
                    raise
                self._sleep(2.0 * (attempt + 1))
                continue
            usage = (response.get("usage") if isinstance(response, dict) else response.usage) or {}
            get = usage.get if isinstance(usage, dict) else (lambda k, d=0: getattr(usage, k, d))
            with self._lock:
                ledger = self._read()
                ledger["billed_input_tokens"] += sum(
                    int(get(k, 0) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
                )
                ledger["output_tokens"] += int(get("output_tokens", 0) or 0)
                self._write(ledger)
            return response
        raise AssertionError("unreachable")


def client_from_env() -> Any:
    """A live client from ``ANTHROPIC_API_KEY``, capped when ``PIERRE_BUDGET_LEDGER`` is set.

    The SDK's own retries are off (``max_retries=0``) so that BudgetedClient counts every attempt.
    The cap comes from ``PIERRE_BUDGET_MAX_CALLS`` and ``PIERRE_BUDGET_MAX_INPUT_TOKENS``.
    """
    import os

    import anthropic

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY is not set: a live run needs a key in this process's environment.")
    client = anthropic.Anthropic(max_retries=0)
    ledger = os.environ.get("PIERRE_BUDGET_LEDGER")
    if not ledger:
        return client
    return BudgetedClient(
        client,
        Path(ledger),
        int(os.environ["PIERRE_BUDGET_MAX_CALLS"]),
        int(os.environ["PIERRE_BUDGET_MAX_INPUT_TOKENS"]),
        retryable=(anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError),
    )
