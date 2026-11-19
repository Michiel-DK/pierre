"""The second span exporter: the same spans over OTLP/HTTP to a local Langfuse (the `[langfuse]` extra).

pierre has no users. The dashboard is meant for a small scheduled canary of held-out cases. These tests replay a
committed turn (no model call) against a fake OTLP endpoint on 127.0.0.1, so they need no Langfuse and no Docker. The
live round trip against a real local Langfuse is `make langfuse-check`, not a unit test.
"""
from __future__ import annotations

import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pierre.agent import obs
from pierre.agent.factory import RECORDINGS_DIR, replay_case

pytest.importorskip("opentelemetry.sdk.trace")
pytest.importorskip("opentelemetry.exporter.otlp.proto.http.trace_exporter")
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest  # noqa: E402

CASE = "hq-and-ticket-quillmoor"  # one turn: 1 invoke_agent, 2 chat, 1 execute_tool, 1 audit
PK, SK = "pk-lf-test-not-a-key", "sk-lf-test-not-a-key"
ENV_KEYS = ("PIERRE_OTEL_EXPORTER", "PIERRE_OTEL_FILE", "LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")


@pytest.fixture(autouse=True)
def _clean_obs(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    obs.reset()
    yield
    obs.reset()


class _FakeOtlp:
    """A local OTLP/HTTP endpoint that records each request and answers with ``status``."""

    def __init__(self, status: int = 200, delay: float = 0.0) -> None:
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                fake.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                time.sleep(delay)
                self.send_response(status)
                self.send_header("Content-Type", "application/x-protobuf")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                return None

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def spans(self):
        out = []
        for req in self.requests:
            msg = ExportTraceServiceRequest()
            msg.ParseFromString(req["body"])
            for rs in msg.resource_spans:
                resource = {a.key: a.value for a in rs.resource.attributes}
                for ss in rs.scope_spans:
                    for s in ss.spans:
                        out.append({"span": s, "resource": resource, "attrs": {a.key: a.value for a in s.attributes}})
        return out


def _langfuse_env(monkeypatch, url, exporter="langfuse"):
    monkeypatch.setenv("PIERRE_OTEL_EXPORTER", exporter)
    monkeypatch.setenv("LANGFUSE_HOST", url)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", PK)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", SK)


def _trace_usage(path):
    """The (input, output) tokens of every model call and auditor call in the committed trace of one turn."""
    (rec,) = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    meta = rec["meta"]
    return [(u["input_tokens"], u["output_tokens"]) for u in meta["usage"]], [
        (u["input_tokens"], u["output_tokens"]) for u in meta["auditor_usage"]
    ]


# ---- configuration ------------------------------------------------------------------------------------------------


def test_langfuse_is_a_known_exporter_and_needs_both_keys():
    assert "langfuse" in obs.EXPORTERS
    with pytest.raises(ValueError, match="LANGFUSE_PUBLIC_KEY"):
        obs.exporter_from_config({"PIERRE_OTEL_EXPORTER": "langfuse"})
    with pytest.raises(ValueError, match="LANGFUSE_SECRET_KEY"):
        obs.exporter_from_config({"PIERRE_OTEL_EXPORTER": "langfuse", "LANGFUSE_PUBLIC_KEY": PK})


def test_langfuse_exporter_posts_to_the_otel_traces_path_of_the_configured_host():
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    exporter = obs.exporter_from_config(
        {"PIERRE_OTEL_EXPORTER": "langfuse", "LANGFUSE_HOST": "http://127.0.0.1:3999/", "LANGFUSE_PUBLIC_KEY": PK, "LANGFUSE_SECRET_KEY": SK}
    )
    assert isinstance(exporter, OTLPSpanExporter)
    assert exporter._endpoint == "http://127.0.0.1:3999/api/public/otel/v1/traces"
    default = obs.exporter_from_config({"PIERRE_OTEL_EXPORTER": "langfuse", "LANGFUSE_PUBLIC_KEY": PK, "LANGFUSE_SECRET_KEY": SK})
    assert default._endpoint == "http://localhost:3000/api/public/otel/v1/traces"  # the local stack, never a cloud region


# ---- the same spans, sent to a fake OTLP endpoint -----------------------------------------------------------------


def test_a_replayed_turn_reaches_the_endpoint_with_langfuse_auth_and_the_same_span_tree(monkeypatch):
    with _FakeOtlp() as fake:
        _langfuse_env(monkeypatch, fake.url)
        _, trace_path = replay_case(CASE)
        obs.reset()

    assert fake.requests, "no OTLP request reached the endpoint"
    for req in fake.requests:
        assert req["path"] == "/api/public/otel/v1/traces"
        assert req["headers"]["authorization"] == "Basic " + base64.b64encode(f"{PK}:{SK}".encode()).decode()
        assert req["headers"]["x-langfuse-ingestion-version"] == "4"
        assert req["headers"]["content-type"] == "application/x-protobuf"

    spans = fake.spans()
    kinds = sorted(s["span"].name.split(" ")[0] for s in spans)
    assert kinds == ["audit", "chat", "chat", "execute_tool", "invoke_agent"]
    (root,) = [s["span"] for s in spans if s["span"].name == "invoke_agent pierre-firm-agent"]
    assert root.parent_span_id == b""
    assert all(s["span"].parent_span_id == root.span_id for s in spans if s["span"] is not root)
    assert {s["span"].trace_id for s in spans} == {root.trace_id}
    assert all(s["resource"]["service.name"].string_value == "pierre-firm-agent" for s in spans)

    model_usage, auditor_usage = _trace_usage(trace_path)
    chats = [
        (s["attrs"]["gen_ai.usage.input_tokens"], s["attrs"]["gen_ai.usage.output_tokens"])
        for s in spans
        if s["span"].name.startswith("chat ")
    ]
    assert all(v.WhichOneof("value") == "int_value" for pair in chats for v in pair)  # numbers, not strings
    assert sorted((i.int_value, o.int_value) for i, o in chats) == sorted(model_usage)
    (aud,) = [s["attrs"] for s in spans if s["span"].name == "audit"]
    assert [(aud["gen_ai.usage.input_tokens"].int_value, aud["gen_ai.usage.output_tokens"].int_value)] == auditor_usage

    assert trace_path.read_bytes() == (RECORDINGS_DIR / f"{CASE}.trace.jsonl").read_bytes()


def test_jsonl_and_langfuse_together_get_the_identical_spans(monkeypatch, tmp_path):
    spans_file = tmp_path / "spans.jsonl"
    with _FakeOtlp() as fake:
        _langfuse_env(monkeypatch, fake.url, exporter="jsonl,langfuse")
        monkeypatch.setenv("PIERRE_OTEL_FILE", str(spans_file))
        replay_case(CASE)
        obs.reset()

    jsonl = {s["span_id"]: s for s in map(json.loads, spans_file.read_text(encoding="utf-8").splitlines())}
    otlp = {s["span"].span_id.hex(): s for s in fake.spans()}
    assert len(jsonl) == 5 and set(jsonl) == set(otlp)
    for span_id, j in jsonl.items():
        o = otlp[span_id]["span"]
        assert o.name == j["name"] and o.trace_id.hex() == j["trace_id"]
        assert (o.parent_span_id.hex() or None) == j["parent_span_id"]
        assert o.start_time_unix_nano == j["start_time_unix_nano"] and o.end_time_unix_nano == j["end_time_unix_nano"]


def test_a_rejecting_dashboard_does_not_change_what_the_agent_does(monkeypatch):
    with _FakeOtlp(status=401) as fake:
        _langfuse_env(monkeypatch, fake.url)
        results, trace_path = replay_case(CASE)
        obs.reset()
    assert fake.requests  # it tried
    assert results and trace_path.read_bytes() == (RECORDINGS_DIR / f"{CASE}.trace.jsonl").read_bytes()


def test_a_slow_dashboard_adds_no_latency_to_the_turn(monkeypatch):
    # 5 spans; exported one by one and synchronously, a 0.5 s endpoint would hold the turn for at least 2.5 s.
    with _FakeOtlp(delay=0.5) as fake:
        _langfuse_env(monkeypatch, fake.url)
        started = time.perf_counter()
        replay_case(CASE)
        turn_seconds = time.perf_counter() - started
        obs.reset()  # the batch is sent here, after the turn
    assert turn_seconds < 2.0, turn_seconds  # the synchronous path cannot finish under 2.5 s
    assert len(fake.spans()) == 5
