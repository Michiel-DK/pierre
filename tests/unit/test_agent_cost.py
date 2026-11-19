"""Cost per turn from a dated price table (offline: no model call).

pierre has no users; the figures below are for the scheduled canary and for the 13 Nov live eval, derived from tokens.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from pierre.agent import cost, obs
from pierre.agent.repair import TurnResult
from pierre.agent.trace import TraceWriter

ROOT = Path(__file__).resolve().parents[2]
MTOK = 1_000_000


@pytest.fixture(autouse=True)
def _fresh_warnings():
    cost.reset_warnings()
    yield
    cost.reset_warnings()


def test_the_price_table_is_dated_and_every_row_cites_its_source():
    table = json.loads((ROOT / "pierre" / "agent" / "prices.json").read_text())
    assert date.fromisoformat(table["as_of"]) == date(2026, 11, 16)
    assert table["currency"] == "USD" and table["unit"] == "per million tokens"
    assert {"claude-opus-5-5", "claude-sonnet-5-5"} <= set(table["models"])
    for model, row in table["models"].items():
        assert row["source_url"].startswith("https://"), model
        assert date.fromisoformat(row["read_on"]) == date(2026, 11, 16), model
        assert {"input", "output", "cache_write_5m", "cache_write_1h", "cache_read"} <= set(row), model
        assert all(isinstance(row[k], (int, float)) and row[k] > 0 for k in ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read"))


def test_one_million_tokens_cost_the_table_price():
    usage = {"input_tokens": MTOK, "output_tokens": MTOK}
    assert cost.usage_cost("claude-opus-5-5", usage) == pytest.approx(4 + 20)
    assert cost.usage_cost("claude-sonnet-5-5", usage) == pytest.approx(2 + 10)


def test_cache_tokens_are_priced_separately():
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": MTOK,
        "cache_creation_input_tokens": 2 * MTOK,
        "cache_creation": {"ephemeral_5m_input_tokens": MTOK, "ephemeral_1h_input_tokens": MTOK},
    }
    assert cost.usage_cost("claude-opus-5-5", usage) == pytest.approx(0.2 + 5 + 8)
    flat = {"cache_creation_input_tokens": MTOK}  # no split given: the 5 minute rate, the lower of the two writes
    assert cost.usage_cost("claude-opus-5-5", flat) == pytest.approx(5)


def test_a_model_without_a_row_is_null_with_one_warning_never_a_guess(caplog):
    with caplog.at_level(logging.WARNING, logger="pierre.agent.cost"):
        assert cost.usage_cost("claude-nonexistent-9", {"input_tokens": 100}) is None
        assert cost.usage_cost("claude-nonexistent-9", {"input_tokens": 100}) is None
    warnings = [r for r in caplog.records if "claude-nonexistent-9" in r.getMessage()]
    assert len(warnings) == 1


def test_turn_cost_splits_the_agent_from_the_auditor():
    turn = TurnResult(
        model="claude-sonnet-5-5",
        usage=[{"input_tokens": MTOK, "output_tokens": 0}, {"input_tokens": 0, "output_tokens": MTOK}],
        auditor_usage=[{"input_tokens": MTOK, "output_tokens": MTOK}],
        auditor_model="claude-opus-5-5",
    )
    c = cost.turn_cost(turn)
    assert c["agent_usd"] == pytest.approx(2 + 10) and c["auditor_usd"] == pytest.approx(4 + 20)
    assert c["total_usd"] == pytest.approx(36)
    assert c["prices_as_of"] == "2026-11-16" and c["currency"] == "USD"


def test_turn_cost_is_null_when_any_needed_model_has_no_row():
    turn = TurnResult(model="claude-sonnet-5-5", usage=[{"input_tokens": 5, "output_tokens": 5}],
                      auditor_usage=[{"input_tokens": 5, "output_tokens": 5}], auditor_model="claude-mystery-1")
    assert cost.turn_cost(turn) is None  # no partial total that looks complete


def test_a_turn_with_no_auditor_call_costs_only_the_agent():
    turn = TurnResult(model="claude-opus-5-5", usage=[{"input_tokens": MTOK, "output_tokens": 0}])
    c = cost.turn_cost(turn)
    assert c["total_usd"] == pytest.approx(4) and c["auditor_usd"] == 0


def _turn():
    return TurnResult(answer="a", model="claude-opus-5-5", steps=1, usage=[{"input_tokens": 1000, "output_tokens": 100}])


def test_cost_goes_into_meta_on_a_live_run_only(tmp_path):
    turn = _turn()
    turn.cost = cost.turn_cost(turn)
    live = TraceWriter(tmp_path / "a.jsonl", "s", "c", live=True).write_turn(0, "q", turn)
    replay = TraceWriter(tmp_path / "b.jsonl", "s", "c").write_turn(0, "q", turn)
    assert live["meta"]["cost"]["total_usd"] == pytest.approx(0.004 + 0.002)
    assert "cost" not in replay["meta"]  # replayed traces stay byte-equal to the committed ones


def test_a_live_turn_with_an_unpriced_model_writes_cost_null(tmp_path):
    turn = TurnResult(model="claude-mystery-1", usage=[{"input_tokens": 1, "output_tokens": 1}])
    turn.cost = cost.turn_cost(turn)
    rec = TraceWriter(tmp_path / "a.jsonl", "s", "c", live=True).write_turn(0, "q", turn)
    assert "cost" in rec["meta"] and rec["meta"]["cost"] is None


def test_the_cost_is_on_the_turn_span(tmp_path, monkeypatch):
    pytest.importorskip("opentelemetry.sdk.trace")
    from pierre.agent.loop import LoopConfig
    from pierre.agent.repair import AgentSession
    from pierre.agent.tools import ToolDispatcher
    from tests.unit._fake_claude import FakeClient, message, text

    path = tmp_path / "spans.jsonl"
    monkeypatch.setenv("PIERRE_OTEL_FILE", str(path))
    obs.reset()
    assert obs.configure() is True
    try:
        reply = message([text("hi")])
        reply["model"] = "claude-opus-5-5"
        reply["usage"] = {"input_tokens": 1000, "output_tokens": 100}
        AgentSession(FakeClient([reply]), LoopConfig(system="s", tools=[]), ToolDispatcher({}), None).ask("q")
    finally:
        obs.reset()
    spans = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    turn = next(s for s in spans if s["name"].startswith("invoke_agent"))
    assert turn["attributes"]["pierre.turn.cost.usd"] == pytest.approx(0.006)


def test_the_derivation_script_reads_the_committed_traces_and_is_repeatable():
    cmd = [sys.executable, "-I", str(ROOT / "scripts" / "analysis" / "cost_from_traces.py"), "--json"]
    a = subprocess.run(cmd, capture_output=True, text=True, check=True, cwd=ROOT).stdout
    b = subprocess.run(cmd, capture_output=True, text=True, check=True, cwd=ROOT).stdout
    assert a == b
    out = json.loads(a)
    assert out["prices_as_of"] == "2026-11-16" and out["turns"] == 108
    assert set(out["by_agent_model"]) == {"claude-opus-5-5", "claude-sonnet-5-5"}
    assert out["total_usd"] == pytest.approx(sum(v["agent_usd"] + v["auditor_usd"] for v in out["by_agent_model"].values()), abs=1e-4)
