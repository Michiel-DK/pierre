"""The tool-use loop and the draft -> audit -> repair session, against a scripted client (a test double)."""
from __future__ import annotations

import json

from pierre.agent.loop import MAX_TOOL_ITERATIONS, LoopConfig, run_loop
from pierre.agent.repair import MAX_REPAIR_ROUNDS, AgentSession

from tests.unit._fake_claude import CountingDispatcher, FakeClient, message, text, tool_use

TOOLS = [{"name": "echo", "description": "d", "input_schema": {"type": "object", "properties": {}}}]
CFG = LoopConfig(system="sys", tools=TOOLS)


def _disp(**extra):
    fns = {"echo": lambda **kw: {"echoed": kw}, "boom": lambda **kw: (_ for _ in ()).throw(RuntimeError("x"))}
    fns.update(extra)
    return CountingDispatcher(fns)


def _tool_results(messages):
    return [m for m in messages if m["role"] == "user" and isinstance(m["content"], list)]


def test_request_uses_auto_tool_choice_and_pinned_effort():
    client = FakeClient([message([text("hi")])])
    run_loop(client, CFG, [{"role": "user", "content": "q"}], _disp())
    req = client.requests[0]
    assert req["tool_choice"] == {"type": "auto"}
    assert req["output_config"] == {"effort": "medium"}
    assert req["model"] == "claude-opus-5-5" and "thinking" not in req and "temperature" not in req


def test_parallel_calls_come_back_in_one_user_message_in_call_order():
    client = FakeClient(
        [
            message([tool_use("a", "echo", n=1), tool_use("b", "echo", n=2), tool_use("c", "echo", n=3)], "tool_use"),
            message([text("done")]),
        ]
    )
    msgs = [{"role": "user", "content": "q"}]
    out = run_loop(client, CFG, msgs, _disp())
    results = _tool_results(msgs)
    assert len(results) == 1
    assert [b["tool_use_id"] for b in results[0]["content"]] == ["a", "b", "c"]
    assert [json.loads(b["content"])["echoed"]["n"] for b in results[0]["content"]] == [1, 2, 3]
    assert out.text == "done" and out.steps == 2 and out.termination is None
    assert [c["args"] for c in out.tool_calls] == [{"n": 1}, {"n": 2}, {"n": 3}]


def test_failed_and_unknown_tools_return_error_dicts():
    client = FakeClient([message([tool_use("a", "boom"), tool_use("b", "nope")], "tool_use"), message([text("ok")])])
    msgs = [{"role": "user", "content": "q"}]
    out = run_loop(client, CFG, msgs, _disp())
    blocks = _tool_results(msgs)[0]["content"]
    assert all(b["is_error"] for b in blocks)
    assert all("error" in r for r in out.tool_results)
    assert "Unknown tool: nope" in out.tool_results[1]["error"]
    assert "Traceback" not in blocks[0]["content"]


def test_iteration_cap_ends_the_turn_after_eight_model_calls():
    client = FakeClient([message([tool_use("a", "echo", n=1)], "tool_use")], repeat_last=True)
    out = run_loop(client, CFG, [{"role": "user", "content": "q"}], _disp())
    assert MAX_TOOL_ITERATIONS == 8
    assert out.steps == 8 == len(client.requests)
    assert out.termination == "iteration_cap"


def test_identical_calls_in_one_response_run_once_and_are_counted():
    d = _disp()
    client = FakeClient(
        [message([tool_use("a", "echo", n=1), tool_use("b", "echo", n=1), tool_use("c", "echo", n=2)], "tool_use"), message([text("x")])]
    )
    out = run_loop(client, CFG, [{"role": "user", "content": "q"}], d)
    assert len(d.calls) == 2 and d.dedup_hits == 1
    assert out.tool_results[0] == out.tool_results[1]
    assert len(out.tool_calls) == 3  # the trace keeps what the model emitted


def test_identical_call_in_a_later_response_of_the_same_turn_is_served_from_cache():
    d = _disp()
    client = FakeClient(
        [message([tool_use("a", "echo", n=1)], "tool_use"), message([tool_use("b", "echo", n=1)], "tool_use"), message([text("x")])]
    )
    run_loop(client, CFG, [{"role": "user", "content": "q"}], d)
    assert len(d.calls) == 1 and d.dedup_hits == 1


def test_refusal_is_checked_before_any_tool_runs():
    d = _disp()
    client = FakeClient([message([tool_use("a", "echo", n=1)], "refusal")])
    out = run_loop(client, CFG, [{"role": "user", "content": "q"}], d)
    assert out.termination == "refusal" and d.calls == []


def test_max_tokens_does_not_run_truncated_tool_calls_and_leaves_valid_history():
    d = _disp()
    msgs = [{"role": "user", "content": "q"}]
    client = FakeClient([message([tool_use("a", "echo", n=1)], "max_tokens")])
    out = run_loop(client, CFG, msgs, d)
    assert out.termination == "max_tokens" and d.calls == []
    last = msgs[-1]
    assert last["role"] == "user" and last["content"][0]["tool_use_id"] == "a" and last["content"][0]["is_error"]


def test_pause_turn_is_an_unexpected_termination():
    out = run_loop(FakeClient([message([text("x")], "pause_turn")]), CFG, [{"role": "user", "content": "q"}], _disp())
    assert out.termination == "unexpected_pause_turn"


def test_loop_takes_any_registry_nothing_pierre_specific():
    d = CountingDispatcher({"add": lambda a, b: {"sum": a + b}})
    client = FakeClient([message([tool_use("a", "add", a=2, b=3)], "tool_use"), message([text("5")])])
    out = run_loop(client, LoopConfig(system="toy", tools=[{"name": "add"}]), [{"role": "user", "content": "q"}], d)
    assert out.tool_results == [{"sum": 5}] and out.text == "5"


# ---- the repair loop ------------------------------------------------------------------------------------------


def _audit(*verdicts):
    """An audit_fn that returns the given verdict lists in order (a test double)."""
    queue = list(verdicts)
    seen = []

    def fn(answer, calls, results):
        seen.append(answer)
        claims = [{"claim_text": f"claim {i}", "claim_type": "other", "verdict": v, "evidence": None} for i, v in enumerate(queue.pop(0))]
        return {
            "verified": sum(c["verdict"] == "verified" for c in claims),
            "fabricated": sum(c["verdict"] == "fabricated" for c in claims),
            "unverifiable": sum(c["verdict"] == "unverifiable" for c in claims),
            "claims": claims,
            "error": None,
        }

    fn.seen = seen
    return fn


def _session(client, audit_fn):
    return AgentSession(client, CFG, _disp(), audit_fn)


def _with_evidence(*answers):
    """Scripted turn: one tool call, then the first answer; further answers are repair replies."""
    first = [message([tool_use("t", "echo", n=1)], "tool_use"), message([text(answers[0])])]
    return first + [message([text(a)]) for a in answers[1:]]


def test_a_fabricated_claim_triggers_a_repair_round_and_stops_when_clean():
    client = FakeClient(_with_evidence("draft", "fixed"))
    s = _session(client, _audit(["fabricated", "verified"], ["verified"]))
    turn = s.ask("q")
    assert [r["round"] for r in turn.rounds] == [0, 1]
    assert turn.rounds[0]["audit"]["fabricated"] == 1 and turn.rounds[1]["audit"]["fabricated"] == 0
    assert turn.answer == "fixed" and turn.termination is None
    assert turn.auditor_calls == 2 and turn.steps == 3
    # append-only: the draft stays, the repair is a new user message after it
    assert [m["role"] for m in s.messages][-3:] == ["assistant", "user", "assistant"]
    repair = client.requests[2]["messages"][-1]
    assert repair["role"] == "user" and "claim 0" in repair["content"] and "claim 1" not in repair["content"]
    assert s.messages[3]["content"][0]["text"] == "draft"


def test_a_clean_draft_runs_no_repair():
    s = _session(FakeClient(_with_evidence("draft")), _audit(["verified"]))
    turn = s.ask("q")
    assert len(turn.rounds) == 1 and turn.auditor_calls == 1 and turn.steps == 2


def test_unverifiable_claims_are_counted_but_never_repaired():
    s = _session(FakeClient(_with_evidence("draft")), _audit(["unverifiable", "unverifiable"]))
    turn = s.ask("q")
    assert len(turn.rounds) == 1 and turn.rounds[0]["audit"]["unverifiable"] == 2 and turn.termination is None


def test_repair_stops_at_the_round_cap_with_repair_cap_termination():
    n = MAX_REPAIR_ROUNDS
    client = FakeClient(_with_evidence("d0", "d1", "d2"))
    s = _session(client, _audit(*(["fabricated"] for _ in range(n + 1))))
    turn = s.ask("q")
    assert n == 2 and len(turn.rounds) == 3 and turn.auditor_calls == 3
    assert turn.termination == "repair_cap" and turn.answer == "d2"
    assert turn.steps <= 16


def test_no_audit_without_any_tool_evidence():
    audit = _audit()
    s = _session(FakeClient([message([text("hello")])]), audit)
    turn = s.ask("hi")
    assert turn.auditor_calls == 0 and turn.rounds[0]["audit"] is None and audit.seen == []


def test_evidence_from_an_earlier_turn_counts_for_a_later_turn():
    audit = _audit(["verified"], ["verified"])
    client = FakeClient(_with_evidence("first") + [message([text("second")])])
    s = _session(client, audit)
    s.ask("q1")
    turn = s.ask("q2")
    assert turn.auditor_calls == 1 and audit.seen == ["first", "second"]


def test_a_failed_audit_is_recorded_and_not_treated_as_clean_or_repaired():
    def failing(answer, calls, results):
        return {"verified": 0, "fabricated": 0, "unverifiable": 0, "claims": [], "error": "RuntimeError: boom"}

    turn = _session(FakeClient(_with_evidence("draft")), failing).ask("q")
    assert turn.rounds[0]["audit"]["error"] == "RuntimeError: boom" and len(turn.rounds) == 1


def test_a_refusal_ends_the_turn_without_an_audit():
    audit = _audit()
    turn = _session(FakeClient([message([text("no")], "refusal")]), audit).ask("q")
    assert turn.termination == "refusal" and turn.auditor_calls == 0


def test_a_failing_audit_call_is_not_clean(monkeypatch):
    monkeypatch.setattr("pierre.agents._llm_retry.time.sleep", lambda s: None)
    from pierre.agent.factory import make_audit_fn

    class Down:
        class messages:
            @staticmethod
            def create(**kw):
                raise RuntimeError("down")

    client = Down()
    fn = make_audit_fn(client)
    out = fn("They backed Acme.", [{"tool": "search_portfolio", "args": {}}],
             [{"companies": [{"company_name": "Acme", "vc_firm": "F", "sector": "s", "description": "d", "investment_date": "2024"}]}])
    assert out["error"] and "down" in out["error"] and out["fabricated"] == 0


def test_a_budget_stop_inside_the_audit_is_not_swallowed_as_an_audit_error():
    import pytest

    from pierre.agent.budget import BudgetExceeded
    from pierre.agent.factory import make_audit_fn

    class Stopped:
        class messages:
            @staticmethod
            def create(**kw):
                raise BudgetExceeded("cap")

    with pytest.raises(BudgetExceeded):
        make_audit_fn(Stopped())("x", [{"tool": "lookup_firm", "args": {}}], [{"found": True, "firm_name": "F", "firm_id": "f"}])


def test_the_judge_is_fixed_whatever_model_the_agent_runs_on():
    from pierre.agent.factory import new_session
    from pierre.agents._claim_auditor import DEFAULT_AUDIT_MODEL

    verdict = '{"audits": [{"claim_text": "x", "claim_type": "other", "verdict": "verified", "evidence": null}]}'
    client = FakeClient(
        [message([tool_use("t", "lookup_firm", firm_name="Quillmoor Capital")], "tool_use"), message([text("London.")]), message([text(verdict)])]
    )
    new_session(client, model="claude-sonnet-5-5").ask("Where is Quillmoor?")
    assert [r["model"] for r in client.requests] == ["claude-sonnet-5-5", "claude-sonnet-5-5", DEFAULT_AUDIT_MODEL]
    assert DEFAULT_AUDIT_MODEL == "claude-opus-5-5"
