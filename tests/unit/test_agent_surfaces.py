"""Parity between the agent's tool list and the MCP server's, the audit-context mapping, and the trace record."""
from __future__ import annotations

import asyncio
import json

from pierre.agent._audit_context import audit_context_from_turn
from pierre.agent.registry import ToolContext, bind_tools, claude_tools
from pierre.agent.backend import InMemoryFirmStore
from pierre.agent.repair import TurnResult
from pierre.agent.tools import ToolDispatcher
from pierre.agent.trace import FixedClock, TraceWriter, record_id
from pierre.mcp_server import build_server


def test_the_agent_tool_list_equals_the_mcp_tool_list():
    mcp_tools = asyncio.run(build_server().list_tools())
    agent = claude_tools()
    assert [t.name for t in mcp_tools] == [t["name"] for t in agent]
    assert {t.name: t.description for t in mcp_tools} == {t["name"]: t["description"] for t in agent}
    for t in mcp_tools:
        mcp_props = set(t.inputSchema.get("properties", {}))
        assert mcp_props == set(next(a for a in agent if a["name"] == t.name)["input_schema"]["properties"])


def test_every_agent_tool_is_strict_with_closed_schema():
    for t in claude_tools():
        assert t["strict"] is True and t["input_schema"]["additionalProperties"] is False


def _run(calls):
    d = ToolDispatcher(bind_tools(ToolContext(InMemoryFirmStore())))
    return calls, d.execute_parallel([(c["tool"], c["args"]) for c in calls])


def test_audit_context_maps_each_tool_result_to_an_auditor_key():
    calls, results = _run(
        [
            {"tool": "lookup_firm", "args": {"firm_name": "Quillmoor"}},
            {"tool": "search_portfolio", "args": {"query": "robotics", "top_k": 2}},
            {"tool": "find_recent_investments", "args": {"sector": "climate"}},
            {"tool": "add_to_shortlist", "args": {"firm_slug": "Brask"}},
            {"tool": "get_portfolio_stats", "args": {"firm_ids": ["ferrin-lane-capital"]}},
        ]
    )
    ctx = audit_context_from_turn(calls, results)
    profile = ctx["firm_profiles"][0]
    assert profile["firm_name"] == "Quillmoor Capital" and profile["hq"] == "London, UK"
    assert profile["cheque_range"] == "EUR 500000-3000000" and profile["portfolio_count"] == 6
    assert ctx["portfolio_companies"][0]["description"].startswith("[Brask Ventures, 2024] ")
    assert ctx["recent_deals"][0]["company_mentioned"] == "Pellvarin Carbon" and "2026-01-20" in ctx["recent_deals"][0]["title"]
    assert ctx["actions_taken"] == ["Added brask-ventures to shortlist (canonicalised from 'Brask')."]
    assert ctx["tool_errors"] and ctx["tool_errors"][0].startswith("get_portfolio_stats(")


def test_audit_context_is_empty_when_nothing_was_called():
    assert audit_context_from_turn([], []) == {
        "firm_profiles": [], "portfolio_companies": [], "recent_deals": [], "actions_taken": [], "tool_errors": [],
    }


def test_trace_record_has_the_agreed_fields(tmp_path):
    result = TurnResult(
        answer="a", content=[{"type": "text", "text": "a"}], steps=2, tool_calls=[{"tool": "t", "args": {}}],
        tool_results=[{"error": "x"}], model="claude-opus-5-5", rounds=[{"round": 0, "answer": "a", "audit": None, "steps": 2}],
    )
    w = TraceWriter(tmp_path / "t.jsonl", "c__m__r0", "c", split="train", runtime={"effort": "medium"})
    rec = w.write_turn(0, "question", result, {"shortlist": []})
    line = json.loads((tmp_path / "t.jsonl").read_text())
    assert line == json.loads(json.dumps(rec))
    assert line["record_id"] == record_id("external:pierre", "c__m__r0", 0, "question")
    assert line["source"] == "external:pierre" and line["task"] == "pierre-firm-agent" and line["provider"] == "anthropic"
    assert line["input"] == "question" and line["output_parsed"] == {"answer": "a"} and line["steps"] == 2
    assert line["termination"] is None and line["final_state"] == {"shortlist": []} and line["repeat"] == 0
    assert line["verdict"] is None and line["failed_checks"] == [] and line["labels"] == []
    assert line["ts"].endswith("+00:00")


def test_each_trace_writer_has_its_own_clock(tmp_path):
    r = TurnResult()
    a = TraceWriter(tmp_path / "a.jsonl", "s", "c").write_turn(0, "q", r)
    b = TraceWriter(tmp_path / "b.jsonl", "s", "c").write_turn(0, "q", r)
    assert a["ts"] == b["ts"]


def test_list_shortlist_descriptions_reach_the_auditor():
    calls, results = _run(
        [{"tool": "add_to_shortlist", "args": {"firm_slug": "Quillmoor"}}, {"tool": "list_shortlist", "args": {}}]
    )
    ctx = audit_context_from_turn(calls, results)
    profile = ctx["firm_profiles"][0]
    assert profile["firm_name"] == "Quillmoor Capital" and "founded in 2014" in profile["description"]
    assert ctx["actions_taken"][-1] == "Shortlist now contains (newest first): quillmoor-capital (no notes)"


def test_a_turn_with_only_recent_deals_is_still_audited():
    from pierre.agents._claim_auditor import _is_context_empty, _summarize_context_for_prompt

    calls, results = _run([{"tool": "find_recent_investments", "args": {}}])
    assert not _is_context_empty(_summarize_context_for_prompt(audit_context_from_turn(calls, results)))
