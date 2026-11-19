# OpenTelemetry spans for the agent

pierre has no users. Apart from the committed eval runs, the agent's only traffic is a small scheduled canary of held-out
cases (`canary/`). The spans below are real telemetry, but the traffic they measure is scheduled, not organic. This work made no model call.
Every span here comes from offline replays of committed recordings or from scripted test doubles.

## What exists

- **The `[obs]` extra** (`pyproject.toml`) pins `opentelemetry-sdk==1.45.2`. The OpenTelemetry API package is already
  installed, because `anthropic` depends on it; the SDK is what the extra adds. Without the SDK, or without any
  configuration, every helper in `pierre/agent/obs.py` is a no-op, and the agent sends the same requests and writes
  the same traces as before.
- **Configuration** comes from the environment:
  - `PIERRE_OTEL_FILE=<path>` turns tracing on and writes spans to that JSONL file.
  - `PIERRE_OTEL_EXPORTER` picks the exporter. Its default is `jsonl`; the other is `langfuse`, and `jsonl,langfuse` writes both (see the exporter seam below).
  - Asking for spans without the SDK installed raises an error. It never silently runs without telemetry.
- **The exporter seam** is `obs.exporter_from_config(config)`, the one function that maps configuration to a span
  exporter. The Langfuse dashboard exporter ([`langfuse.md`](langfuse.md)) is one more branch in that function; the spans
  themselves stay as they are.
- **A private tracer provider**, never the global one, so spans from other libraries (the Anthropic SDK is
  OpenTelemetry-aware) never join the tree. The JSONL file exporter goes through a `SimpleSpanProcessor`, so a span is on disk as
  soon as it ends; a network exporter (Langfuse) goes through a `BatchSpanProcessor` (`obs.configure`).

## The span tree (GenAI semantic conventions)

Attribute names were checked against `opentelemetry-semantic-conventions` 0.66b2. Attributes starting with `pierre.`
are this project's own.

| span | parent | attributes |
|---|---|---|
| `invoke_agent pierre-firm-agent`, one per turn | none (root) | `gen_ai.operation.name=invoke_agent`, `gen_ai.provider.name=anthropic`, `gen_ai.agent.name`, `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.conversation.id` (the trace's session id), `gen_ai.usage.input_tokens` / `output_tokens` (summed over the turn's model calls), `pierre.turn.index` / `steps` / `rounds` / `auditor_calls` / `dedup_hits` / `termination`, `pierre.audit.verified` / `fabricated` / `unverifiable` (the last audit), `error.type` (a termination other than `repair_cap`, or the class of an exception that escaped) |
| `chat {model}`, one per model call | the turn, or a repair round | `gen_ai.operation.name=chat`, `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.request.max_tokens`, `gen_ai.response.model`, `gen_ai.response.id`, `gen_ai.response.finish_reasons` (the `stop_reason`), `gen_ai.usage.input_tokens` / `output_tokens` / `cache_read.input_tokens` / `cache_creation.input_tokens`, `error.type` on an exception |
| `execute_tool {tool}`, one per tool call that is run | the turn, or a repair round. Tools run on worker threads, so the context is passed to them explicitly | `gen_ai.operation.name=execute_tool`, `gen_ai.tool.name`, `gen_ai.tool.type=function`, `pierre.tool.dedup_hit`, `error.type`: the exception class when a tool raised (the dispatcher catches it), `tool_error` when the tool returned an error, `unknown_tool` |
| `audit`, one per auditor call | the turn, or a repair round | `gen_ai.operation.name=chat`, `gen_ai.request.model` (the fixed judge), `gen_ai.usage.input_tokens` / `output_tokens`, the three verdict counts, `error.type=audit_error` |
| `repair_round` | the turn | `pierre.repair.round`, `pierre.repair.claims` (the fabricated claims sent back), verdict counts of the round's audit, `error.type` when a loop stop ended the round |

The draft round (round 0) has no span of its own: its model, tool and auditor calls sit directly under the turn.
Latency is the span's own start and end, which the JSONL exporter writes as `duration_ms`. A call repeated in one
response is resolved before dispatch and gets no span, because it never runs.

## Latency in the trace (live runs only)

`TraceWriter(live=True)` adds a single new key, `meta.latency_ms = {"turn", "model_calls", "auditor_calls"}`, measured
in wall-clock milliseconds (`time.perf_counter`). Two callers set `live`, both through `run_all(..., live=True)`: `scripts/analysis/agent_eval.py run` and the canary
(`pierre/agent/canary.py`, `make canary`). Everything
else stays as it was:
- `ts` keeps `FixedClock`.
- No existing key changes.
- Replays and `scripts/demo/agent_record.py` stay non-live. The trace that `agent_record.py` writes is the reference
  that replay must reproduce byte for byte, so it cannot carry latency.

## Predictions against what was observed

The predictions are in [`predictions-1.md`](predictions-1.md), committed before the tests and the code; that commit order is in a
private source repository and can be shown on request.

| # | predicted | observed |
|---|---|---|
| P1 | 389 passed (range 385 to 392), 0 skipped | **391 passed, 0 skipped** (`make test`). Within the range; the new file has 17 tests, not 15 |
| P2 | every new test fails against today's code | **partly refuted in form.** The first red run was one collection error (`ImportError: cannot import name 'obs'`), with the 374 old tests passing. A second red run, with `obs.py` present and not yet wired in, gave 14 failed and 3 passed. The 3 that passed are the two configuration tests and the guard for a missing extra, which has nothing to hide until the spans exist |
| P3 | `tool-failure-timeout`: 19 spans, 2 with `error.type` | **confirmed**: 1 / 5 / 12 / 1 / 0, two tool spans with `error.type`, every non-root span parented to the turn. The prediction did not name the error type. The test first expected `tool_error`; it is `TimeoutError`, the class the fault slug raises, and the test was changed to that |
| P4 | `hq-and-ticket-quillmoor`: 5 spans | **confirmed** |
| P5 | 1 `repair_round` holding its `chat` and `audit` | **confirmed** (scripted session) |
| P6 | 8 of 8 replays byte-equal with tracing on | **confirmed** |
| P7 | without the SDK: runs, byte-equal, no span file | **confirmed**: the SDK modules were hidden from import inside the test. In a fresh env with only `.[test]` (what CI installs), the new file gives 3 passed and 14 skipped |
| P8 | `make reports`: 0 lines of diff | **confirmed** |
| P9 / P10 | term gate exit 0, gitleaks clean | not recorded in this file. The term gate's list is private, so that check cannot be re-run from this repository; `gitleaks git .` can |

## What CI checks

CI installs `.[test]` only, without the `[obs]` extra, so it runs the guard for a missing extra and skips the 14 span
tests. `make install` includes `[obs]`, so `make test` runs them all. This work leaves the CI workflow unchanged.
