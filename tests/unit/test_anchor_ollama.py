"""The Ollama adapter against a fake server on an ephemeral port (never :11434). No Ollama, no model, no key."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pierre.agent.loop import LoopConfig, run_loop
from pierre.anchor.ollama_client import ContextOverflow, OllamaClient, OllamaError, from_ollama_response, to_ollama_messages
from tests.unit._fake_claude import tool_use


class FakeOllama:
    """Serves scripted /api/chat replies in order and keeps every request body."""

    def __init__(self, replies, status=200):
        self.replies, self.bodies, self.status = list(replies), [], status
        fake = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                assert self.path == "/api/chat"
                fake.bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                out = json.dumps(fake.replies.pop(0) if fake.status == 200 else {"error": "boom"}).encode()
                self.send_response(fake.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake():
    made = []

    def make(replies, status=200):
        f = FakeOllama(replies, status)
        made.append(f)
        return f

    yield make
    for f in made:
        f.close()


def reply(content="", calls=(), done_reason="stop", prompt=100, out=10):
    msg = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = [{"function": {"name": n, "arguments": a}} for n, a in calls]
    return {"model": "gemma4:e2b", "message": msg, "done": True, "done_reason": done_reason, "prompt_eval_count": prompt, "eval_count": out}


TOOLS = [{"name": "get_balance", "description": "Balance.", "input_schema": {"type": "object", "properties": {}}},
         {"name": "send_money", "description": "Send.", "input_schema": {"type": "object", "properties": {"recipient": {"type": "string"}}}}]


def test_request_translation_system_tools_calls_and_named_tool_results(fake):
    f = fake([reply("ok")])
    client = OllamaClient(f.url, num_ctx=8192, num_predict=512, think=False)
    history = [
        {"role": "user", "content": "What is my balance?"},
        {"role": "assistant", "content": [{"type": "text", "text": "Checking."}, tool_use("toolu_local_0_0", "get_balance")]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_local_0_0", "content": "1810.0"}]},
    ]
    out = client.messages.create(model="gemma4:e2b", system="SYS", tools=TOOLS, messages=history, max_tokens=16000,
                                 tool_choice={"type": "auto"}, output_config={"effort": "medium"})
    body = f.bodies[0]
    assert body["model"] == "gemma4:e2b" and body["stream"] is False and body["think"] is False
    assert body["options"] == {"num_ctx": 8192, "num_predict": 512}  # max_tokens is clamped to the local output budget
    assert body["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "What is my balance?"},
        {"role": "assistant", "content": "Checking.", "tool_calls": [{"function": {"name": "get_balance", "arguments": {}}}]},
        {"role": "tool", "tool_name": "get_balance", "content": "1810.0"},
    ]
    assert body["tools"][1] == {"type": "function", "function": {"name": "send_money", "description": "Send.", "parameters": TOOLS[1]["input_schema"]}}
    assert "tool_choice" not in body and "output_config" not in body
    assert out["stop_reason"] == "end_turn" and out["content"] == [{"type": "text", "text": "ok"}]
    assert out["usage"] == {"input_tokens": 100, "output_tokens": 10}


def test_response_tool_calls_get_deterministic_ids_and_string_arguments_are_parsed():
    r = from_ollama_response(reply(calls=[("send_money", {"recipient": "X"}), ("get_balance", '{"a": 1}')]), step=3)
    assert r["stop_reason"] == "tool_use"
    assert [(b["id"], b["name"], b["input"]) for b in r["content"]] == [("toolu_local_3_0", "send_money", {"recipient": "X"}), ("toolu_local_3_1", "get_balance", {"a": 1})]
    assert from_ollama_response(reply(calls=[("x", "not json")]), 0)["content"][0]["input"] == {"_unparsed_arguments": "not json"}


def test_a_length_cut_is_max_tokens_even_with_a_tool_call_so_the_loop_never_runs_it(fake):
    r = from_ollama_response(reply(calls=[("send_money", {"recipient": "X"})], done_reason="length"), 0)
    assert r["stop_reason"] == "max_tokens"
    f = fake([reply(calls=[("send_money", {"recipient": "X"})], done_reason="length")])

    class D:
        dedup_hits = 0

        def reset_turn(self):
            pass

        def execute_parallel(self, calls):
            raise AssertionError("a cut-off tool call was run")

    out = run_loop(OllamaClient(f.url), LoopConfig(system="S", tools=TOOLS, model="gemma4:e2b"), [{"role": "user", "content": "go"}], D())
    assert out.termination == "max_tokens"


def test_the_loop_drives_a_local_model_through_tool_calls(fake):
    f = fake([reply(calls=[("get_balance", {})]), reply("Your balance is 1810.")])

    class D:
        dedup_hits = 0
        calls = []

        def reset_turn(self):
            pass

        def execute_parallel(self, calls):
            self.calls += calls
            return ["1810.0" for _ in calls]

    d = D()
    msgs = [{"role": "user", "content": "balance?"}]
    out = run_loop(OllamaClient(f.url), LoopConfig(system="S", tools=TOOLS, model="gemma4:e2b"), msgs, d)
    assert out.termination is None and out.text == "Your balance is 1810." and d.calls == [("get_balance", {})]
    assert f.bodies[1]["messages"][-1] == {"role": "tool", "tool_name": "get_balance", "content": "1810.0"}
    assert out.steps == 2 and out.usage == [{"input_tokens": 100, "output_tokens": 10}] * 2


def test_a_prompt_too_long_for_num_ctx_is_refused_before_sending(fake):
    f = fake([reply("never")])
    client = OllamaClient(f.url, num_ctx=1000, num_predict=500)
    with pytest.raises(ContextOverflow):
        client.messages.create(model="m", system="S" * 4000, tools=[], messages=[{"role": "user", "content": "q"}])
    assert f.bodies == []


def test_a_prompt_ollama_cut_is_an_error_not_an_answer(fake):
    f = fake([reply("partial view", prompt=16384)])
    with pytest.raises(ContextOverflow):
        OllamaClient(f.url, num_ctx=16384).messages.create(model="m", system="S", tools=[], messages=[{"role": "user", "content": "q"}])


def test_an_http_error_raises(fake):
    f = fake([], status=500)
    with pytest.raises(OllamaError, match="HTTP 500"):
        OllamaClient(f.url).messages.create(model="m", system="S", tools=[], messages=[{"role": "user", "content": "q"}])


def test_settings_are_recorded_and_thinking_is_sent_explicitly(fake):
    f = fake([reply("ok")])
    c = OllamaClient(f.url, num_ctx=4096, num_predict=256, think=True, options={"seed": 1})
    c.messages.create(model="m", system="", tools=[], messages=[{"role": "user", "content": "q"}])
    assert f.bodies[0]["think"] is True and f.bodies[0]["options"] == {"seed": 1, "num_ctx": 4096, "num_predict": 256}
    assert c.settings() == {"provider": "ollama", "num_ctx": 4096, "num_predict": 256, "think": True, "seed": 1}
    assert to_ollama_messages("", [{"role": "user", "content": "q"}]) == [{"role": "user", "content": "q"}]


def test_a_whole_agentdojo_session_on_the_local_adapter_is_graded_by_the_benchmark(fake, tmp_path):
    pytest.importorskip("agentdojo")
    import sys
    from pathlib import Path

    from agentdojo.agent_pipeline.agent_pipeline import load_system_message
    from agentdojo.task_suite.load_suites import get_suite

    from tests.unit._fake_claude import FakeClient, message, text

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "analysis"))
    import public_anchor_agentdojo as runner

    bill = dict(recipient="UK12345678901234567890", amount=98.7, subject="Car Rental\t\t\t98.70", date="2022-01-01")
    f = fake([reply(calls=[("read_file", {"file_path": "bill-december-2023.txt"})]), reply(calls=[("send_money", bill)]), reply("Paid the bill.")])
    audit = FakeClient([message([text(json.dumps({"audits": [{"claim_text": "c", "claim_type": "other", "verdict": "verified", "evidence": "x"}]}))])])
    suite = get_suite("v1.2.2", "banking")
    rec = runner.run_one(suite, OllamaClient(f.url), load_system_message(None), "user_task_0", None, model=runner.LOCAL_MODEL, arm="none",
                         audit_client=audit, traces=tmp_path)
    assert rec["utility"] is True and rec["steps"] == 3 and rec["auditor_calls"] == 1 and len(audit.requests) == 1
    assert [c["tool"] for c in rec["calls"]] == ["read_file", "send_money"] and rec["defence_fired"] is None
