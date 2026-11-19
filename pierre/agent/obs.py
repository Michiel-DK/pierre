"""OpenTelemetry spans for the agent, behind the optional ``[obs]`` extra (the OpenTelemetry SDK).

pierre has no users. These spans are meant for a small scheduled canary of held-out cases: the telemetry is real, but
the traffic it measures is scheduled. The spans follow the OpenTelemetry GenAI semantic conventions: one
``invoke_agent`` span per turn, with child spans ``chat`` (model call), ``execute_tool`` (tool call), ``audit``
(auditor call) and ``repair_round``.

This module imports only the standard library. Tracing is off when nothing is configured, and every helper is then a
no-op, so without the extra the agent runs exactly as before. Configuration comes from the environment:

    PIERRE_OTEL_FILE=<path>        turn tracing on; spans go to this JSONL file
    PIERRE_OTEL_EXPORTER=jsonl     the exporter (the default)
    PIERRE_OTEL_EXPORTER=langfuse  OTLP/HTTP to a local self-hosted Langfuse (the ``[langfuse]`` extra); needs
                                   LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY, and LANGFUSE_HOST (default
                                   http://localhost:3000)
    PIERRE_OTEL_EXPORTER=jsonl,langfuse  both, with the same spans

``exporter_from_config`` is the single place that maps configuration to a span exporter, so a second exporter (the
dashboard) is one more branch there and needs no change to the spans.

The spans never touch a request, a response or the trace record, so replays and committed traces stay byte-equal.
They use a private tracer provider, never the global one, so no other library's spans end up in the tree.
"""
from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from typing import Any, Callable, Dict, Iterator, Mapping, Optional

AGENT_NAME = "pierre-firm-agent"
PROVIDER = "anthropic"
EXPORTERS = ("jsonl", "langfuse")
LANGFUSE_DEFAULT_HOST = "http://localhost:3000"  # the local stack (observability/docker-compose.langfuse.yml)

monotonic: Callable[[], float] = time.perf_counter  # seconds; tests replace it to make latency deterministic

_lock = threading.Lock()
_state: Dict[str, Any] = {"configured": False, "tracer": None, "provider": None}


def sdk_available() -> bool:
    try:
        import opentelemetry.sdk.trace  # noqa: F401
        import opentelemetry.sdk.trace.export  # noqa: F401
    except ImportError:
        return False
    return True


def _require_sdk() -> None:
    if not sdk_available():
        raise RuntimeError(
            "tracing is configured (PIERRE_OTEL_FILE / PIERRE_OTEL_EXPORTER) but the OpenTelemetry SDK is not "
            'installed: pip install -e ".[obs]"'
        )


def _jsonl_exporter_class() -> type:
    from opentelemetry.sdk.trace.export import SpanExportResult, SpanExporter

    class JsonlSpanExporter(SpanExporter):
        """One JSON line per finished span. Children end before their parent, so a turn's span is written last."""

        def __init__(self, path: str) -> None:
            self.path = path
            self._lock = threading.Lock()
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)

        def export(self, spans: Any) -> Any:
            lines = [json.dumps(span_record(s), sort_keys=True, ensure_ascii=False) for s in spans]
            with self._lock, open(self.path, "a", encoding="utf-8") as fh:
                for line in lines:
                    fh.write(line + "\n")
            return SpanExportResult.SUCCESS

        def shutdown(self) -> None:
            return None

    return JsonlSpanExporter


def _langfuse_exporter(config: Mapping[str, str]) -> Any:
    """OTLP over HTTP/protobuf to Langfuse's OpenTelemetry endpoint, with its project keys as basic auth."""
    import base64

    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    except ImportError as exc:
        raise RuntimeError('PIERRE_OTEL_EXPORTER=langfuse needs the OTLP exporter: pip install -e ".[obs,langfuse]"') from exc
    keys = {k: (config.get(k) or "").strip() for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")}
    for key, value in keys.items():
        if not value:
            raise ValueError(f"PIERRE_OTEL_EXPORTER=langfuse needs {key} (make langfuse-up writes it to the env file)")
    host = ((config.get("LANGFUSE_HOST") or "").strip() or LANGFUSE_DEFAULT_HOST).rstrip("/")
    auth = base64.b64encode(f"{keys['LANGFUSE_PUBLIC_KEY']}:{keys['LANGFUSE_SECRET_KEY']}".encode()).decode()
    return OTLPSpanExporter(
        endpoint=f"{host}/api/public/otel/v1/traces",
        # Without the ingestion-version header, Langfuse v4 can show directly ingested spans up to 10 minutes late.
        headers={"Authorization": f"Basic {auth}", "x-langfuse-ingestion-version": "4"},
    )


def span_record(span: Any) -> Dict[str, Any]:
    ctx = span.get_span_context()
    parent = span.parent
    return {
        "name": span.name,
        "trace_id": format(ctx.trace_id, "032x"),
        "span_id": format(ctx.span_id, "016x"),
        "parent_span_id": format(parent.span_id, "016x") if parent is not None else None,
        "start_time_unix_nano": span.start_time,
        "end_time_unix_nano": span.end_time,
        "duration_ms": round((span.end_time - span.start_time) / 1e6, 3),
        "status": span.status.status_code.name,
        "attributes": {k: (list(v) if isinstance(v, tuple) else v) for k, v in (span.attributes or {}).items()},
    }


def exporter_from_config(config: Optional[Mapping[str, str]] = None) -> Any:
    """The span exporter the configuration asks for (a tuple when it names several), or None when tracing is off.

    The one seam for exporters."""
    config = os.environ if config is None else config
    raw = (config.get("PIERRE_OTEL_EXPORTER") or "").strip().lower()
    path = (config.get("PIERRE_OTEL_FILE") or "").strip()
    if not raw and not path:
        return None
    names = [n.strip() for n in (raw or "jsonl").split(",") if n.strip()]
    unknown = [n for n in names if n not in EXPORTERS]
    if unknown or not names:
        raise ValueError(f"PIERRE_OTEL_EXPORTER={raw!r} is unknown; known: {', '.join(EXPORTERS)}")
    _require_sdk()
    exporters = []
    for name in dict.fromkeys(names):
        if name == "jsonl":
            if not path:
                raise ValueError("PIERRE_OTEL_EXPORTER=jsonl needs PIERRE_OTEL_FILE=<path>")
            exporters.append(_jsonl_exporter_class()(path))
        elif name == "langfuse":
            exporters.append(_langfuse_exporter(config))
        else:
            raise AssertionError("unreachable")
    return exporters[0] if len(exporters) == 1 else tuple(exporters)


def configure(exporter: Any = None) -> bool:
    """Set up tracing from ``exporter``, or from the environment when it is None. Returns whether tracing is on."""
    with _lock:
        exporter = exporter if exporter is not None else exporter_from_config()
        _state["configured"] = True
        if exporter is None:
            _state["tracer"] = _state["provider"] = None
            return False
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

        provider = TracerProvider(resource=Resource.create({"service.name": AGENT_NAME}))
        for one in exporter if isinstance(exporter, tuple) else (exporter,):
            # A local file is written synchronously, so a span is on disk when it ends. A network exporter gets a
            # background batch: a slow or unreachable dashboard (the OTLP client retries with backoff) must not add
            # to the turn's latency. Its spans are sent at the latest when reset() shuts the provider down.
            local = type(one).__name__ == "JsonlSpanExporter"
            provider.add_span_processor(SimpleSpanProcessor(one) if local else BatchSpanProcessor(one))
        _state["provider"], _state["tracer"] = provider, provider.get_tracer("pierre.agent")
        return True


def reset() -> None:
    """Shut tracing down and forget the configuration; the next span reads the environment again."""
    with _lock:
        if _state["provider"] is not None:
            _state["provider"].shutdown()
        _state.update(configured=False, tracer=None, provider=None)


def _tracer() -> Any:
    if not _state["configured"]:
        configure()
    return _state["tracer"]


def enabled() -> bool:
    return _tracer() is not None


class _Span:
    """What the instrumented code sees: attribute setters that do nothing when tracing is off."""

    __slots__ = ("_span",)

    def __init__(self, span: Any = None) -> None:
        self._span = span

    def set(self, key: str, value: Any) -> None:
        if self._span is not None and value is not None:
            self._span.set_attribute(key, value)

    def update(self, attributes: Mapping[str, Any]) -> None:
        for k, v in attributes.items():
            self.set(k, v)

    def error(self, error_type: str) -> None:
        if self._span is not None:
            from opentelemetry.trace import Status, StatusCode

            self._span.set_attribute("error.type", error_type)
            self._span.set_status(Status(StatusCode.ERROR))


_OFF = _Span()


@contextlib.contextmanager
def span(name: str, attributes: Optional[Mapping[str, Any]] = None) -> Iterator[_Span]:
    """A child of the current span. An exception that leaves it sets ``error.type`` to the exception's class name."""
    tracer = _tracer()
    if tracer is None:
        yield _OFF
        return
    with tracer.start_as_current_span(name, record_exception=True, set_status_on_exception=True) as raw:
        handle = _Span(raw)
        handle.update(attributes or {})
        try:
            yield handle
        except BaseException as exc:
            raw.set_attribute("error.type", type(exc).__name__)
            raise


def capture() -> Any:
    """The current context, to hand to a worker thread (thread pools do not carry it over)."""
    if _tracer() is None:
        return None
    from opentelemetry import context

    return context.get_current()


def run_in(ctx: Any, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    if ctx is None:
        return fn(*args, **kwargs)
    from opentelemetry import context

    token = context.attach(ctx)
    try:
        return fn(*args, **kwargs)
    finally:
        context.detach(token)


def elapsed_ms(start: float) -> float:
    return round((monotonic() - start) * 1000, 1)


# ---- attribute builders (GenAI semantic conventions; names checked against opentelemetry-semantic-conventions 0.66b2)


def chat_request_attributes(model: str, max_tokens: Optional[int]) -> Dict[str, Any]:
    return {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": PROVIDER,
        "gen_ai.request.model": model,
        "gen_ai.request.max_tokens": max_tokens,
    }


def usage_attributes(usage: Mapping[str, Any]) -> Dict[str, Any]:
    out = {
        "gen_ai.usage.input_tokens": usage.get("input_tokens"),
        "gen_ai.usage.output_tokens": usage.get("output_tokens"),
        "gen_ai.usage.cache_read.input_tokens": usage.get("cache_read_input_tokens"),
        "gen_ai.usage.cache_creation.input_tokens": usage.get("cache_creation_input_tokens"),
    }
    return {k: int(v) for k, v in out.items() if v is not None}


def chat_response_attributes(response: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "gen_ai.response.model": response.get("model"),
        "gen_ai.response.id": response.get("id"),
        "gen_ai.response.finish_reasons": [str(response.get("stop_reason"))],
        **usage_attributes(response.get("usage") or {}),
    }


def audit_attributes(audit: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if not audit:
        return {}
    return {f"pierre.audit.{k}": int(audit.get(k) or 0) for k in ("verified", "fabricated", "unverifiable")}
