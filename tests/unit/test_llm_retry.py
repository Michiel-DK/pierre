"""Unit tests for ``pierre.agents._llm_retry``.

The report / agent LLM path previously had NO retry/backoff: transient API
failures degraded output silently and malformed JSON raised on the first
failure. ``call_with_retry`` is the shared chokepoint that adds:

- exponential backoff on transient API exceptions, and
- a BOUNDED JSON-decode retry (retry N times, then re-raise loudly — never
  swallow malformed output into a placeholder).

These tests prove:
  (a) a transient API error triggers a bounded retry then succeeds,
  (b) malformed JSON is retried (not raised on first failure), then succeeds,
  (c) exhausted retries re-raise loudly (the loud-failure contract — bad
      JSON must NOT silently degrade),
  (d) backoff delays follow the exponential schedule (capped),
  (e) the injected ``sleep`` is used (tests never actually sleep).

Pure module: ``_llm_retry`` imports only the stdlib, so this test runs in the
stripped-down environment without database drivers or tenacity. No real LLM calls — ``call_fn`` / ``parse_fn`` are
plain Python callables.
"""
from __future__ import annotations

import json

import pytest

from pierre.agents._llm_retry import (
    DEFAULT_API_ATTEMPTS,
    DEFAULT_PARSE_ATTEMPTS,
    _backoff_delay,
    call_with_retry,
)


# ---------------------------------------------------------------------------
# Test doubles.
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Stand-in for a Gemini response object — only ``.text`` is read."""

    def __init__(self, text: str):
        self.text = text


def _no_sleep_recorder():
    """Return a (sleep_fn, calls_list) pair that records sleep durations."""
    calls: list = []

    def _sleep(seconds: float) -> None:
        calls.append(seconds)

    return _sleep, calls


# ---------------------------------------------------------------------------
# (a) transient API error → bounded retry → success
# ---------------------------------------------------------------------------


def test_transient_api_error_retries_then_succeeds():
    sleep, slept = _no_sleep_recorder()
    attempts = {"n": 0}

    def call_fn():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("transient transport blip")
        return _FakeResponse('{"ok": true}')

    result = call_with_retry(
        call_fn,
        lambda r: json.loads(r.text),
        api_attempts=3,
        sleep=sleep,
        label="test",
    )

    assert result == {"ok": True}
    assert attempts["n"] == 3            # failed twice, succeeded on 3rd
    assert len(slept) == 2               # one backoff per failed attempt


def test_api_error_exhausted_reraises():
    sleep, _ = _no_sleep_recorder()

    def call_fn():
        raise TimeoutError("API down")

    with pytest.raises(TimeoutError, match="API down"):
        call_with_retry(
            call_fn,
            lambda r: json.loads(r.text),
            api_attempts=3,
            sleep=sleep,
            label="test",
        )


# ---------------------------------------------------------------------------
# (b) malformed JSON is RETRIED (not raised on first failure)
# ---------------------------------------------------------------------------


def test_malformed_json_retried_then_succeeds():
    sleep, slept = _no_sleep_recorder()
    calls = {"n": 0}

    def call_fn():
        calls["n"] += 1
        # First call returns garbage; second returns valid JSON.
        if calls["n"] == 1:
            return _FakeResponse("not json at all {{{")
        return _FakeResponse('{"recovered": 1}')

    result = call_with_retry(
        call_fn,
        lambda r: json.loads(r.text),
        parse_attempts=2,
        sleep=sleep,
        label="test",
    )

    assert result == {"recovered": 1}
    assert calls["n"] == 2               # the model was re-called once on bad JSON
    assert len(slept) == 1               # one parse-retry backoff


# ---------------------------------------------------------------------------
# (c) exhausted parse retries RE-RAISE loudly (loud-failure contract)
# ---------------------------------------------------------------------------


def test_malformed_json_exhausted_raises_loudly():
    sleep, _ = _no_sleep_recorder()
    calls = {"n": 0}

    def call_fn():
        calls["n"] += 1
        return _FakeResponse("still not json")

    with pytest.raises(json.JSONDecodeError):
        call_with_retry(
            call_fn,
            lambda r: json.loads(r.text),
            parse_attempts=2,
            sleep=sleep,
            label="test",
        )

    # Re-called exactly parse_attempts times, then raised — never swallowed.
    assert calls["n"] == 2


def test_typeerror_on_none_text_is_retried_and_raised():
    """A None ``.text`` makes ``json.loads`` raise TypeError — the report
    call sites pass ``(TypeError, ValueError)`` as the malformed-output set,
    so this must be retried then re-raised, not treated as a transient API
    error (which would hit the graceful placeholder)."""
    sleep, _ = _no_sleep_recorder()

    def call_fn():
        return _FakeResponse(None)  # type: ignore[arg-type]

    with pytest.raises(TypeError):
        call_with_retry(
            call_fn,
            lambda r: json.loads(r.text),
            parse_attempts=2,
            sleep=sleep,
            label="test",
        )


# ---------------------------------------------------------------------------
# (d) backoff schedule
# ---------------------------------------------------------------------------


def test_backoff_delay_is_exponential_and_capped():
    assert _backoff_delay(0, base_delay=2.0, max_delay=10.0) == 2.0
    assert _backoff_delay(1, base_delay=2.0, max_delay=10.0) == 4.0
    assert _backoff_delay(2, base_delay=2.0, max_delay=10.0) == 8.0
    # capped at max_delay
    assert _backoff_delay(3, base_delay=2.0, max_delay=10.0) == 10.0
    assert _backoff_delay(10, base_delay=2.0, max_delay=10.0) == 10.0


def test_first_success_no_sleep():
    sleep, slept = _no_sleep_recorder()

    result = call_with_retry(
        lambda: _FakeResponse('{"x": 1}'),
        lambda r: json.loads(r.text),
        sleep=sleep,
        label="test",
    )
    assert result == {"x": 1}
    assert slept == []                   # clean first attempt never sleeps


def test_defaults_are_sane():
    assert DEFAULT_API_ATTEMPTS >= 2
    assert DEFAULT_PARSE_ATTEMPTS >= 2


def test_identity_parse_fn_returns_response():
    """The claim-auditor wires an identity parse_fn (parse_attempts=1) so the
    backoff applies to the API call only and the raw response flows through."""
    resp = _FakeResponse('{"audits": []}')
    out = call_with_retry(
        lambda: resp,
        lambda r: r,
        parse_attempts=1,
        sleep=lambda _s: None,
        label="claim_audit",
    )
    assert out is resp
