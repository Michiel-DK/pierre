"""Record/replay mechanics and the runtime budget, over a scripted client (a test double, not a recording)."""
from __future__ import annotations

import json

import pytest

from pierre.agent.budget import BudgetedClient, BudgetExceeded
from pierre.agent.factory import run_case
from pierre.agent.replay import RecordingClient, ReplayClient, ReplayMismatch
from pierre.agent.trace import TraceWriter

from tests.unit._fake_claude import FakeClient, message, text, tool_use

CASE = {"id": "c1", "input": "Where is Quillmoor Capital based?"}
SCRIPT = [
    message([tool_use("t1", "lookup_firm", firm_name="Quillmoor Capital")], "tool_use"),
    message([text("London.")]),
    message([text('{"audits": [{"claim_text": "London", "claim_type": "other", "verdict": "verified", "evidence": null}]}')]),
]


def _run(client, path):
    run_case(client, CASE, TraceWriter(path, "c1__m__r0", "c1", split="train"))
    return path.read_bytes()


def test_replay_reproduces_the_recorded_run_byte_for_byte(tmp_path):
    rec = RecordingClient(FakeClient(SCRIPT), tmp_path / "rec.jsonl", {"case_id": "c1"})
    live = _run(rec, tmp_path / "live.jsonl")
    first = _run(ReplayClient(tmp_path / "rec.jsonl"), tmp_path / "r1.jsonl")
    second = _run(ReplayClient(tmp_path / "rec.jsonl"), tmp_path / "r2.jsonl")
    assert live == first == second
    lines = [json.loads(x) for x in (tmp_path / "rec.jsonl").read_text().splitlines()]
    assert lines[0]["recorded"] is True and lines[0]["case_id"] == "c1"
    assert [x["role"] for x in lines[1:]] == ["agent", "agent", "auditor"]
    assert "headers" not in json.dumps(lines)


def test_replay_refuses_a_changed_request(tmp_path):
    RecordingClient(FakeClient(SCRIPT), tmp_path / "rec.jsonl", {})  # header only
    _run(RecordingClient(FakeClient(SCRIPT), tmp_path / "rec.jsonl", {}), tmp_path / "a.jsonl")
    changed = dict(CASE, input="Where is Brask Ventures based?")
    with pytest.raises(ReplayMismatch, match="record the session again"):
        run_case(ReplayClient(tmp_path / "rec.jsonl"), changed, TraceWriter(tmp_path / "b.jsonl", "s", "c1"))


def test_replay_refuses_a_call_that_was_never_recorded(tmp_path):
    _run(RecordingClient(FakeClient(SCRIPT[:2]), tmp_path / "rec.jsonl", {}), tmp_path / "a.jsonl")
    with pytest.raises(ReplayMismatch):
        # an audit call is now made but was not recorded
        _run(ReplayClient(tmp_path / "rec.jsonl"), tmp_path / "b.jsonl")


class _Spy:
    def __init__(self):
        self.n = 0
        self.messages = self

    def create(self, **kw):
        self.n += 1
        return message([text("x")])


def test_budget_aborts_before_the_call_that_would_pass_the_cap(tmp_path):
    spy = _Spy()
    c = BudgetedClient(spy, tmp_path / "l.json", max_calls=2, max_input_tokens=10**9)
    c.create(model="m", messages=[])
    c.create(model="m", messages=[])
    with pytest.raises(BudgetExceeded, match="cap is 2 calls"):
        c.create(model="m", messages=[])
    assert spy.n == 2


def test_budget_is_shared_across_processes_through_the_ledger_and_checks_tokens(tmp_path):
    spy = _Spy()
    BudgetedClient(spy, tmp_path / "l.json", 10, 10**9).create(model="m", messages=[{"role": "user", "content": "x" * 400}])
    other = BudgetedClient(spy, tmp_path / "l.json", 10, 150)
    with pytest.raises(BudgetExceeded):
        other.create(model="m", messages=[{"role": "user", "content": "x" * 400}])
    ledger = json.loads((tmp_path / "l.json").read_text())
    assert ledger["calls"] == 1 and ledger["est_input_tokens"] >= 100 and ledger["billed_input_tokens"] == 10


def test_budget_counts_retries(tmp_path):
    class Flaky(_Spy):
        def create(self, **kw):
            self.n += 1
            if self.n == 1:
                raise TimeoutError("t")
            return message([text("x")])

    spy = Flaky()
    c = BudgetedClient(spy, tmp_path / "l.json", 5, 10**9, sleep=lambda s: None, retryable=(TimeoutError,))
    c.create(model="m", messages=[])
    assert json.loads((tmp_path / "l.json").read_text())["calls"] == 2
