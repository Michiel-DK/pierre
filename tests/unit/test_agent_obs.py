"""OpenTelemetry spans for the agent, the `[obs]` extra (offline: replays and scripted test doubles, no model call).

pierre has no users. The spans exist to watch a small scheduled canary of held-out cases; these tests check the span
tree on recorded sessions, check that a session runs unchanged without the extra, and check that the trace format
is unchanged.
"""
from __future__ import annotations

import json
import sys
from collections import Counter

import pytest

from pierre.agent import obs
from pierre.agent.factory import RECORDINGS_DIR, load_cases, replay_case

from tests.unit._fake_claude import CountingDispatcher, FakeClient, message, text, tool_use

RECORDINGS = sorted(p.stem for p in RECORDINGS_DIR.glob("*.jsonl") if not p.name.endswith((".trace.jsonl", "record_log.jsonl")))
TURN = "invoke_agent pierre-firm-agent"


@pytest.fixture(autouse=True)
def _clean_obs(monkeypatch):
    for key in ("PIERRE_OTEL_EXPORTER", "PIERRE_OTEL_FILE"):
        monkeypatch.delenv(key, raising=False)
    obs.reset()
    yield
    obs.reset()


@pytest.fixture
def spans_file(tmp_path, monkeypatch):
    pytest.importorskip("opentelemetry.sdk.trace")
    path = tmp_path / "spans.jsonl"
    monkeypatch.setenv("PIERRE_OTEL_FILE", str(path))
    assert obs.configure() is True
    return path


def _spans(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _kind(span):
    return span["name"].split(" ")[0]


# ---- configuration: the exporter seam ---------------------------------------------------------------------------


def test_no_configuration_means_no_tracing():
    assert obs.exporter_from_config({}) is None
    assert obs.configure() is False and obs.enabled() is False


def test_a_file_path_alone_selects_the_jsonl_exporter(tmp_path):
    pytest.importorskip("opentelemetry.sdk.trace")
    exporter = obs.exporter_from_config({"PIERRE_OTEL_FILE": str(tmp_path / "s.jsonl")})
    assert type(exporter).__name__ == "JsonlSpanExporter"
    with pytest.raises(ValueError, match="unknown"):
        obs.exporter_from_config({"PIERRE_OTEL_EXPORTER": "nope"})
    with pytest.raises(ValueError, match="PIERRE_OTEL_FILE"):
        obs.exporter_from_config({"PIERRE_OTEL_EXPORTER": "jsonl"})


# ---- the span tree of a replayed turn ---------------------------------------------------------------------------


def test_replayed_turn_with_failing_tools_yields_the_expected_span_tree(spans_file):
    results, _ = replay_case("tool-failure-timeout")
    spans = _spans(spans_file)
    assert Counter(_kind(s) for s in spans) == {"invoke_agent": 1, "chat": 5, "execute_tool": 12, "audit": 1}
    (turn,) = [s for s in spans if s["name"] == TURN]
    assert turn["parent_span_id"] is None
    assert all(s["parent_span_id"] == turn["span_id"] for s in spans if s is not turn)
    assert len({s["trace_id"] for s in spans}) == 1

    a = turn["attributes"]
    r = results[0]
    assert a["gen_ai.operation.name"] == "invoke_agent" and a["gen_ai.provider.name"] == "anthropic"
    assert a["gen_ai.conversation.id"] == "tool-failure-timeout__claude-opus-5-5__r0"
    assert a["pierre.turn.steps"] == r.steps == 5 and a["pierre.turn.auditor_calls"] == 1
    assert a["gen_ai.usage.input_tokens"] == sum(u["input_tokens"] for u in r.usage)
    assert a["gen_ai.usage.output_tokens"] == sum(u["output_tokens"] for u in r.usage)
    audit = r.rounds[-1]["audit"]
    assert (a["pierre.audit.verified"], a["pierre.audit.fabricated"], a["pierre.audit.unverifiable"]) == (
        audit["verified"], audit["fabricated"], audit["unverifiable"])

    chats = sorted((s for s in spans if _kind(s) == "chat"), key=lambda s: s["start_time_unix_nano"])
    for span, usage, stop in zip(chats, r.usage, r.stop_reasons):
        c = span["attributes"]
        assert span["name"] == "chat claude-opus-5-5" and c["gen_ai.operation.name"] == "chat"
        assert c["gen_ai.request.model"] == "claude-opus-5-5" and c["gen_ai.response.model"].startswith("claude-opus-5-5")
        assert c["gen_ai.response.finish_reasons"] == [stop]
        assert c["gen_ai.usage.input_tokens"] == usage["input_tokens"]
        assert c["gen_ai.usage.output_tokens"] == usage["output_tokens"]
        assert c["gen_ai.usage.cache_read.input_tokens"] == usage["cache_read_input_tokens"]
        assert c["gen_ai.usage.cache_creation.input_tokens"] == usage["cache_creation_input_tokens"]
        assert span["duration_ms"] >= 0

    tools = [s for s in spans if _kind(s) == "execute_tool"]
    assert Counter(s["attributes"]["gen_ai.tool.name"] for s in tools) == Counter(c["tool"] for c in r.tool_calls)
    assert all(s["attributes"]["gen_ai.tool.type"] == "function" for s in tools)
    failed = [s for s in tools if "error.type" in s["attributes"]]
    assert len(failed) == 2 == sum(1 for x in r.tool_results if isinstance(x, dict) and "error" in x)
    assert {s["attributes"]["error.type"] for s in failed} == {"TimeoutError"}  # raised by the fault slug, caught by the dispatcher
    assert all(s["status"] == "ERROR" for s in failed)

    (aud,) = [s for s in spans if _kind(s) == "audit"]
    assert aud["attributes"]["pierre.audit.fabricated"] == audit["fabricated"]
    assert aud["attributes"]["gen_ai.usage.input_tokens"] == r.auditor_usage[0]["input_tokens"]
    assert aud["attributes"]["gen_ai.request.model"]


def test_replayed_one_tool_turn_yields_five_spans(spans_file):
    replay_case("hq-and-ticket-quillmoor")
    assert Counter(_kind(s) for s in _spans(spans_file)) == {"invoke_agent": 1, "chat": 2, "execute_tool": 1, "audit": 1}


def test_every_turn_of_a_multi_turn_session_is_its_own_root_span(spans_file):
    replay_case("shortlist-swap-three-turns")
    roots = [s for s in _spans(spans_file) if s["parent_span_id"] is None]
    assert [s["name"] for s in roots] == [TURN] * 3
    assert sorted(s["attributes"]["pierre.turn.index"] for s in roots) == [0, 1, 2]


# ---- the repair round (no committed recording has one, so a scripted session) -----------------------------------


def _audit(*verdicts):
    queue = list(verdicts)

    def fn(answer, calls, results):
        claims = [{"claim_text": f"c{i}", "claim_type": "other", "verdict": v, "evidence": None} for i, v in enumerate(queue.pop(0))]
        n = Counter(c["verdict"] for c in claims)
        return {"verified": n["verified"], "fabricated": n["fabricated"], "unverifiable": n["unverifiable"],
                "claims": claims, "error": None, "usage": {"input_tokens": 7, "output_tokens": 3}, "model": "judge-test"}

    return fn


def test_a_repair_round_is_a_span_holding_its_model_and_auditor_calls(spans_file):
    from pierre.agent.loop import LoopConfig
    from pierre.agent.repair import AgentSession

    client = FakeClient([message([tool_use("t", "echo", n=1)], "tool_use"), message([text("draft")]), message([text("fixed")])])
    cfg = LoopConfig(system="s", tools=[{"name": "echo"}])
    session = AgentSession(client, cfg, CountingDispatcher({"echo": lambda **kw: kw}), _audit(["fabricated"], ["verified"]))
    session.ask("q")
    spans = _spans(spans_file)
    by_id = {s["span_id"]: s for s in spans}
    (turn,) = [s for s in spans if _kind(s) == "invoke_agent"]
    (rnd,) = [s for s in spans if _kind(s) == "repair_round"]
    assert rnd["parent_span_id"] == turn["span_id"] and rnd["attributes"]["pierre.repair.round"] == 1
    assert rnd["attributes"]["pierre.repair.claims"] == 1
    inside = sorted(_kind(s) for s in spans if s["parent_span_id"] == rnd["span_id"])
    assert inside == ["audit", "chat"]
    direct = Counter(_kind(s) for s in spans if s["parent_span_id"] == turn["span_id"])
    assert direct == {"chat": 2, "execute_tool": 1, "audit": 1, "repair_round": 1}
    assert all(by_id[s["parent_span_id"]] for s in spans if s["parent_span_id"])
    assert turn["attributes"]["pierre.turn.rounds"] == 2 and turn["attributes"]["pierre.audit.fabricated"] == 0


def test_a_crashing_model_call_marks_the_chat_and_turn_spans_with_its_error_type(spans_file):
    from pierre.agent.loop import LoopConfig
    from pierre.agent.repair import AgentSession

    class Boom:
        messages = None

        def create(self, **kw):
            raise RuntimeError("boom")  # not a timeout: a timeout now ends the turn instead (test_agent_timeouts.py)

    boom = Boom()
    boom.messages = boom
    session = AgentSession(boom, LoopConfig(system="s", tools=[]), CountingDispatcher({}), None)
    with pytest.raises(RuntimeError):
        session.ask("q")
    spans = {_kind(s): s for s in _spans(spans_file)}
    assert spans["chat"]["attributes"]["error.type"] == "RuntimeError"
    assert spans["invoke_agent"]["attributes"]["error.type"] == "RuntimeError"
    assert spans["chat"]["status"] == "ERROR"


# ---- the trace format does not change ----------------------------------------------------------------------------


@pytest.mark.parametrize("case_id", RECORDINGS)
def test_replay_with_tracing_on_is_byte_equal_to_the_committed_trace(spans_file, case_id):
    _, path = replay_case(case_id)
    assert path.read_bytes() == (RECORDINGS_DIR / f"{case_id}.trace.jsonl").read_bytes()
    assert _spans(spans_file)


def test_without_the_extra_the_agent_runs_as_today(monkeypatch, tmp_path):
    for name in [m for m in sys.modules if m == "opentelemetry.sdk" or m.startswith("opentelemetry.sdk.")] + ["opentelemetry.sdk"]:
        monkeypatch.setitem(sys.modules, name, None)  # an import of the SDK now raises ImportError
    obs.reset()
    assert obs.sdk_available() is False
    assert obs.configure() is False and obs.enabled() is False
    _, path = replay_case("hq-and-ticket-quillmoor")
    assert path.read_bytes() == (RECORDINGS_DIR / "hq-and-ticket-quillmoor.trace.jsonl").read_bytes()
    assert list(tmp_path.iterdir()) == []

    # Asking for spans without the extra is a loud error, never a silent run without telemetry.
    monkeypatch.setenv("PIERRE_OTEL_FILE", str(tmp_path / "spans.jsonl"))
    obs.reset()
    with pytest.raises(RuntimeError, match=r"\[obs\]"):
        obs.configure()


# ---- wall-clock latency in meta, live mode only -------------------------------------------------------------------


def test_latency_goes_into_meta_in_live_mode_only(tmp_path, monkeypatch):
    from pierre.agent.eval_run import Session, run_session

    cases = load_cases()
    cid = "hq-and-ticket-quillmoor"
    ticks = iter(range(0, 10_000, 250))
    monkeypatch.setattr(obs, "monotonic", lambda: next(ticks) / 1000)
    script = [message([tool_use("t", "lookup_firm", firm_name="Quillmoor")], "tool_use"), message([text("Quillmoor is in Ghent.")])]
    audit_reply = message([text(json.dumps({"claims": []}))])

    s_live = Session(cid, "claude-sonnet-5-5", 0)
    run_session(FakeClient(script + [audit_reply]), cases[cid], s_live, tmp_path / "live", live=True)
    (live,) = [json.loads(x) for x in s_live.path(tmp_path / "live").read_text().splitlines()]
    lat = live["meta"]["latency_ms"]
    assert set(lat) == {"turn", "model_calls", "auditor_calls"}
    assert len(lat["model_calls"]) == 2 and all(x > 0 for x in lat["model_calls"]) and lat["turn"] > sum(lat["model_calls"])
    assert len(lat["auditor_calls"]) == live["meta"]["auditor_calls"]

    s_replay = Session(cid, "claude-sonnet-5-5", 0)
    run_session(FakeClient(script + [audit_reply]), cases[cid], s_replay, tmp_path / "replay")
    (replay,) = [json.loads(x) for x in s_replay.path(tmp_path / "replay").read_text().splitlines()]
    assert "latency_ms" not in replay["meta"]
    assert live["ts"] == replay["ts"]  # FixedClock stays the clock of `ts` in both modes
    live["meta"].pop("latency_ms")
    assert live["meta"].pop("cost") is None  # live mode writes meta.cost; the fake model has no price row, so it is null
    assert "cost" not in replay["meta"]
    assert live == replay
