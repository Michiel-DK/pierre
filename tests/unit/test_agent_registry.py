import asyncio
import json
from pathlib import Path

import pytest

from pierre.agent.backend import InMemoryFirmStore
from pierre.agent.registry import BY_NAME, REGISTRY, WRITE_TOOLS, ToolContext, bind_tools, claude_tools
from pierre.mcp_server import build_server

CASES = json.loads((Path(__file__).resolve().parents[2] / "evals" / "pierre-firm-agent" / "cases.json").read_text(encoding="utf-8"))


def _expected_blocks():
    for case in CASES["cases"]:
        yield case["id"], case.get("expected", {})
        for turn in case.get("turns_expected", []):
            yield case["id"], turn["expected"]


def test_there_are_eleven_tools_with_unique_names():
    assert len(REGISTRY) == 11
    assert len(BY_NAME) == 11


def test_claude_tool_list_and_mcp_server_expose_the_same_names():
    server = build_server()
    mcp_names = [t.name for t in asyncio.run(server.list_tools())]
    assert mcp_names == [t["name"] for t in claude_tools()]


def test_every_claude_tool_is_strict_with_closed_object_schemas():
    for tool in claude_tools():
        schema = tool["input_schema"]
        assert tool["strict"] is True
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert set(schema["required"]) <= set(schema["properties"])
        assert tool["description"]


def test_write_tools_match_the_case_file():
    assert list(WRITE_TOOLS) == CASES["write_tools"]


def test_case_expectations_only_name_real_tools_and_real_arguments():
    for case_id, expected in _expected_blocks():
        named = set(expected.get("tools_called", []) + expected.get("tools_not_called", []))
        named |= {r["tool"] for r in expected.get("args_match", [])}
        if "error_recovery" in expected:
            named.add(expected["error_recovery"]["tool"])
        assert named <= set(BY_NAME), (case_id, named - set(BY_NAME))
        for rule in expected.get("args_match", []):
            assert set(rule["args"]) <= set(BY_NAME[rule["tool"]].input_schema["properties"]), (case_id, rule)
    for case in CASES["cases"]:
        for check in ("writes_expected",):
            for item in case.get("session", {}).get(check, []):
                assert item["tool"] in WRITE_TOOLS


def test_mcp_server_runs_a_tool_and_keeps_state_between_calls():
    server = build_server(InMemoryFirmStore())

    async def scenario():
        await server.call_tool("add_to_shortlist", {"firm_slug": "kestrabel-ventures", "notes": "security seed lead"})
        return await server.call_tool("list_shortlist", {})

    content = asyncio.run(scenario())
    payload = json.loads(content[0].text)
    assert payload["total"] == 1 and payload["shortlist"][0]["notes"] == "security seed lead"


def test_mcp_server_returns_error_dicts_not_tracebacks():
    server = build_server()
    content = asyncio.run(server.call_tool("get_portfolio_stats", {"firm_ids": ["ferrin-lane-capital"]}))
    payload = json.loads(content[0].text)
    assert set(payload) == {"error"}


def test_bound_tools_cover_the_registry():
    bound = bind_tools(ToolContext(InMemoryFirmStore()))
    assert list(bound) == [s.name for s in REGISTRY]
