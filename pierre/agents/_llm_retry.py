"""CI-strip-safe LLM call-with-retry helper for the report / audit path.

In the source system the core report/agent LLM path had NO retry/backoff —
transient API failures degraded output silently and malformed JSON raised on
the first failure. ``tenacity`` would be the obvious fix, but the unit tests
must run in a stripped-down dependency set (no database drivers, no
tenacity).

So this helper deliberately uses a **stdlib** ``for attempt in range()``
loop with ``time.sleep`` rather than ``tenacity``.
The backoff ``sleep`` is injectable so tests run instantly.

The helper is the single chokepoint every report/audit LLM call routes
through (fix the chokepoint, not each caller):

    call_with_retry(call_fn, parse_fn, ...)

      1. run ``call_fn`` (the Gemini ``generate_content`` invocation)
         — retry with exponential backoff on a transient API exception.
      2. run ``parse_fn`` on the response — retry (bounded) when the
         model returns malformed JSON.
      3. on exhaustion, **re-raise the original exception**.

Each call site keeps its OWN terminal ``except`` (graceful placeholder /
fallback-dict / pass-through) so the distinct degradation behaviour at
each site is preserved.  Crucially the bounded JSON retry sits *between*
first-failure and the raise: it retries N times then **still raises**
(it does NOT swallow malformed JSON into a fallback) — preserving the
loud-failure behaviour on bad JSON.

Pure module: imports only the stdlib so it is importable under CI-strip.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional, Tuple, Type

logger = logging.getLogger(__name__)

# Defaults — kept here so callers don't have to invent their own numbers.
DEFAULT_API_ATTEMPTS = 3          # transient-API retries (the call itself)
DEFAULT_PARSE_ATTEMPTS = 2        # bounded JSON-decode retries (re-call the model)
DEFAULT_BASE_DELAY = 2.0          # seconds — first backoff wait
DEFAULT_MAX_DELAY = 10.0          # seconds — cap on exponential backoff


def _backoff_delay(attempt: int, base_delay: float, max_delay: float) -> float:
    """Exponential backoff (base * 2**attempt), capped at ``max_delay``.

    ``attempt`` is 0-indexed for the FAILED attempt we just had, so the
    first retry waits ``base_delay`` seconds.
    """
    return min(base_delay * (2 ** attempt), max_delay)


def call_with_retry(
    call_fn: Callable[[], Any],
    parse_fn: Callable[[Any], Any],
    *,
    api_attempts: int = DEFAULT_API_ATTEMPTS,
    parse_attempts: int = DEFAULT_PARSE_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    parse_exceptions: Tuple[Type[BaseException], ...] = (TypeError, ValueError),
    sleep: Optional[Callable[[float], None]] = None,
    label: str = "llm",
) -> Any:
    """Run an LLM call + parse with exponential backoff and a bounded JSON retry.

    Args:
        call_fn: zero-arg callable that performs the LLM API call and returns
            the raw response object.
        parse_fn: callable taking the raw response and returning the parsed
            value (e.g. ``lambda r: json.loads(r.text)``). Raising one of
            ``parse_exceptions`` triggers a bounded re-call.
        api_attempts: total attempts for the API call on transient errors
            (>=1). Each retry waits ``_backoff_delay`` seconds.
        parse_attempts: total attempts for the call+parse cycle on a malformed
            response (>=1). When exhausted the LAST parse exception is re-raised
            (loud failure — never swallowed).
        base_delay / max_delay: exponential-backoff bounds (seconds).
        parse_exceptions: exception types treated as "malformed output" → retry.
        sleep: injectable sleep (tests pass a no-op so they run instantly).
        label: short string used in log messages to identify the call site.

    Returns:
        The parsed value from ``parse_fn`` on the first success.

    Raises:
        The transient-API exception if every API attempt failed; or the parse
        exception if every parse attempt produced malformed output. Both are
        re-raised so the caller's terminal ``except`` decides the graceful
        fallback — this helper NEVER substitutes a placeholder itself.
    """
    if api_attempts < 1:
        api_attempts = 1
    if parse_attempts < 1:
        parse_attempts = 1
    # Resolve ``time.sleep`` at call time (not as a default arg) so a test that
    # patches ``pierre.agents._llm_retry.time.sleep`` actually takes effect.
    if sleep is None:
        sleep = time.sleep

    last_parse_exc: Optional[BaseException] = None

    for parse_round in range(parse_attempts):
        response = _call_with_api_backoff(
            call_fn,
            api_attempts=api_attempts,
            base_delay=base_delay,
            max_delay=max_delay,
            sleep=sleep,
            label=label,
        )
        try:
            return parse_fn(response)
        except parse_exceptions as exc:
            last_parse_exc = exc
            if parse_round < parse_attempts - 1:
                logger.warning(
                    "%s: malformed LLM response (parse attempt %d/%d) — retrying: %s",
                    label, parse_round + 1, parse_attempts, exc,
                )
                # Small fixed nudge before re-calling so a momentary model
                # hiccup has a beat to clear; reuse the backoff schedule.
                sleep(_backoff_delay(parse_round, base_delay, max_delay))
                continue
            logger.error(
                "%s: malformed LLM response after %d parse attempt(s) — re-raising: %s",
                label, parse_attempts, exc,
            )
            raise

    # Unreachable (loop either returns or raises) — defensive re-raise.
    if last_parse_exc is not None:
        raise last_parse_exc
    raise RuntimeError(f"{label}: call_with_retry exhausted with no result")


def _call_with_api_backoff(
    call_fn: Callable[[], Any],
    *,
    api_attempts: int,
    base_delay: float,
    max_delay: float,
    sleep: Callable[[float], None],
    label: str,
) -> Any:
    """Run ``call_fn`` with exponential backoff on transient API exceptions.

    Re-raises the last exception when all attempts are exhausted so the
    caller's terminal ``except`` handles the graceful fallback.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(api_attempts):
        try:
            return call_fn()
        except Exception as exc:  # transient API / transport error
            last_exc = exc
            if attempt < api_attempts - 1:
                delay = _backoff_delay(attempt, base_delay, max_delay)
                logger.warning(
                    "%s: LLM API call failed (attempt %d/%d) — backing off %.1fs: %s",
                    label, attempt + 1, api_attempts, delay, exc,
                )
                sleep(delay)
                continue
            logger.error(
                "%s: LLM API call failed after %d attempt(s): %s",
                label, api_attempts, exc,
            )
            raise
    # Unreachable — loop returns or raises.
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"{label}: _call_with_api_backoff exhausted with no result")
