"""Client and tool timeouts (offline: fake slow clients and fake slow tools, no model call).

A hit ends the turn with termination cause ``timeout``; wilco force-fails any turn that carries a termination.
pierre has no users: these guard the scheduled canary, which must never hang on one slow call.
"""
from __future__ import annotations

import time

import httpx2
import anthropic

from pierre.agent import timeouts
from pierre.agent.budget import BudgetedClient, client_from_env
from pierre.agent.loop import LoopConfig, run_loop
from pierre.agent.repair import AgentSession
from pierre.agent.tools import ToolDispatcher
from pierre.agent.trace import TraceWriter

from tests.unit._fake_claude import FakeClient, message, text, tool_use

TOOLS = [{"name": "echo", "description": "d", "input_schema": {"type": "object", "properties": {}}}]


class SlowClient:
    """A model call that takes ``delay`` seconds."""

    def __init__(self, delay: float) -> None:
        self.delay, self.calls, self.messages = delay, 0, self

    def create(self, **kwargs):
        self.calls += 1
        time.sleep(self.delay)
        return message([text("late")])


def _sdk_timeout() -> Exception:
    return anthropic.APITimeoutError(request=httpx2.Request("POST", "https://example.invalid/v1/messages"))


def test_defaults_are_explicit_and_bounded():
    assert 0 < timeouts.MODEL_TIMEOUT_S < 600  # the SDK default is 600 s (anthropic/_constants.py DEFAULT_TIMEOUT)
    assert 0 < timeouts.TOOL_TIMEOUT_S < timeouts.MODEL_TIMEOUT_S


def test_a_slow_model_call_ends_the_turn_with_timeout():
    client = SlowClient(delay=0.6)
    cfg = LoopConfig(system="s", tools=TOOLS, model_timeout_s=0.05)
    msgs = [{"role": "user", "content": "q"}]
    start = time.monotonic()
    out = run_loop(client, cfg, msgs, ToolDispatcher({}))
    assert time.monotonic() - start < 0.4  # it did not wait for the slow call
    assert out.termination == "timeout" and out.text == ""
    assert client.calls == 1 and len(out.latency_ms) == 1
    assert msgs == [{"role": "user", "content": "q"}]  # no half-written assistant message


def test_an_sdk_timeout_error_ends_the_turn_with_timeout():
    class Raises:
        messages = None

        def __init__(self):
            self.messages, self.calls = self, 0

        def create(self, **kwargs):
            self.calls += 1
            raise _sdk_timeout()

    client = Raises()
    out = run_loop(client, LoopConfig(system="s", tools=TOOLS), [{"role": "user", "content": "q"}], ToolDispatcher({}))
    assert out.termination == "timeout" and client.calls == 1


def test_other_client_errors_still_propagate():
    class Boom:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            raise RuntimeError("not a timeout")

    try:
        run_loop(Boom(), LoopConfig(system="s", tools=TOOLS), [{"role": "user", "content": "q"}], ToolDispatcher({}))
    except RuntimeError as exc:
        assert "not a timeout" in str(exc)
    else:
        raise AssertionError("a non-timeout error must not be swallowed")


def test_budgeted_client_does_not_retry_a_timeout(tmp_path):
    class Raises:
        def __init__(self):
            self.messages, self.calls = self, 0

        def create(self, **kwargs):
            self.calls += 1
            raise _sdk_timeout()

    inner = Raises()
    bc = BudgetedClient(inner, tmp_path / "l.json", 10, 10_000_000, sleep=lambda s: None, retryable=(anthropic.APIConnectionError,))
    try:
        bc.create(model="m", max_tokens=1, messages=[])
    except anthropic.APITimeoutError:
        pass
    else:
        raise AssertionError("expected the timeout to propagate")
    assert inner.calls == 1  # APITimeoutError subclasses APIConnectionError, which is retried: a timeout must not be


def test_budgeted_client_still_retries_a_connection_error(tmp_path):
    class Flaky:
        def __init__(self):
            self.messages, self.calls = self, 0

        def create(self, **kwargs):
            self.calls += 1
            if self.calls < 2:
                raise anthropic.APIConnectionError(request=httpx2.Request("POST", "https://example.invalid"))
            return message([text("ok")])

    inner = Flaky()
    bc = BudgetedClient(inner, tmp_path / "l.json", 10, 10_000_000, sleep=lambda s: None, retryable=(anthropic.APIConnectionError,))
    bc.create(model="m", max_tokens=1, messages=[])
    assert inner.calls == 2


def test_the_live_client_carries_an_explicit_timeout(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.delenv("PIERRE_BUDGET_LEDGER", raising=False)
    client = client_from_env()
    assert client.timeout.read == timeouts.MODEL_TIMEOUT_S and client.timeout.connect == timeouts.CONNECT_TIMEOUT_S
    assert client.timeout != anthropic._constants.DEFAULT_TIMEOUT
    assert client.max_retries == 0


def test_a_slow_tool_ends_the_turn_with_timeout_and_a_valid_history():
    release = []

    def slow(**kw):
        time.sleep(0.6)
        release.append(1)
        return {"ok": True}

    disp = ToolDispatcher({"echo": lambda **kw: {"echoed": kw}, "slow": slow}, tool_timeout_s=0.05)
    client = FakeClient([message([tool_use("a", "slow"), tool_use("b", "echo", n=1)], "tool_use"), message([text("never")])])
    msgs = [{"role": "user", "content": "q"}]
    start = time.monotonic()
    out = run_loop(client, LoopConfig(system="s", tools=TOOLS), msgs, disp)
    assert time.monotonic() - start < 0.4
    assert out.termination == "timeout"
    assert len(client.requests) == 1  # the model was not called again
    uses = [b["id"] for b in msgs[1]["content"] if b["type"] == "tool_use"]
    answered = [b["tool_use_id"] for b in msgs[2]["content"]]
    assert uses == answered == ["a", "b"]  # every tool_use has a tool_result, so the history can be sent again
    assert all(b["is_error"] for b in msgs[2]["content"])


def test_a_fast_tool_is_unaffected_by_the_tool_timeout():
    disp = ToolDispatcher({"echo": lambda **kw: {"echoed": kw}}, tool_timeout_s=5)
    assert disp.execute_parallel([("echo", {"n": 1})]) == [{"echoed": {"n": 1}}]


def test_a_tool_that_raises_is_still_an_error_result_not_a_timeout():
    disp = ToolDispatcher({"boom": lambda **kw: (_ for _ in ()).throw(RuntimeError("x"))}, tool_timeout_s=5)
    out = disp.execute_parallel([("boom", {})])
    assert "error" in out[0]


def test_the_trace_records_the_timeout_as_a_termination(tmp_path):
    cfg = LoopConfig(system="s", tools=TOOLS, model_timeout_s=0.05)
    session = AgentSession(SlowClient(0.6), cfg, ToolDispatcher({}), None)
    result = session.ask("q")
    assert result.termination == "timeout" and result.rounds[0]["audit"] is None
    writer = TraceWriter(tmp_path / "t.jsonl", "s", "c")
    rec = writer.write_turn(0, "q", result)
    assert rec["termination"] == {"cause": "timeout"}


def test_a_timed_out_audit_call_ends_the_turn_with_timeout():
    from pierre.agent.factory import new_session

    class ScriptedThenTimeout:
        def __init__(self):
            self.messages, self.calls = self, 0

        def create(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return message([tool_use("a", "lookup_firm", firm_name="Quillmoor")], "tool_use")
            if self.calls == 2:
                return message([text("Quillmoor is in Ghent.")])
            raise _sdk_timeout()  # the auditor's call

    client = ScriptedThenTimeout()
    session = new_session(client)
    result = session.ask("q")
    assert client.calls == 3  # the audit call was made once and not retried
    assert result.termination == "timeout"
    assert result.auditor_calls == 1 and result.rounds[0]["audit"] is None
