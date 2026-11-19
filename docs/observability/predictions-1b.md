# The Langfuse dashboard: predictions, written before any build commit

This file was written and committed before any build commit for the dashboard. The order of the commits is in a private source repository and can be shown on request. Observations are recorded
in [`langfuse.md`](langfuse.md).

_Edited for publication: framing words only (titles, internal process terms, private commit ids and file names); no predicted value, range, pass line or kill line changed._

pierre has no users. The traffic this dashboard will show is a small scheduled canary of held-out cases. The telemetry
is real; the traffic is scheduled. The round-trip check replays one committed turn and makes no model
call.

## Q1. How many services does the stack have?

**Prediction: 6.**
- `langfuse-web`
- `langfuse-worker`
- `postgres`
- `clickhouse`
- `redis`
- `minio`

The upstream `docker-compose.yml` (langfuse/langfuse `main` at `ab7d58a3e670`, fetched 16 Nov 2026) has 7 services.
The seventh is `langfuse-ai-gateway`, a beta LLM proxy that starts unconfigured by design and publishes port 3031 on
all interfaces. It is not needed to ingest or show traces, so it is left out.

Only `langfuse-web` gets a host port, on `127.0.0.1`. No other service is published, because the stack talks over
its internal network. On this machine `127.0.0.1:5432` is already in use, so publishing postgres would fail anyway.

## Q2. Which spans show up for the replayed turn?

The turn is the single-turn recording `hq-and-ticket-quillmoor`. The JSONL file exporter of the spans writes 5 spans for it:
- `invoke_agent pierre-firm-agent` (root)
- 2 × `chat {model}`
- 1 × `execute_tool {tool}`
- 1 × `audit`

**Prediction:**
- Langfuse shows 1 trace with 5 observations.
- The 4 non-root observations have the turn as their parent.
- Each observation id is the span's 16-hex `span_id`, and the trace id is the 32-hex `trace_id`.
- The trace name is the root span's name.

Observation types, predicted per span:

| span | predicted type | why |
|---|---|---|
| `chat {model}` | `GENERATION` | has `gen_ai.request.model` and `gen_ai.operation.name=chat` |
| `audit` | `GENERATION` | same two attributes |
| `execute_tool {tool}` | `TOOL` | from `gen_ai.operation.name=execute_tool`. Low confidence: the docs say the operation name is used but give no table. Fallback: `SPAN` |
| `invoke_agent …` | `AGENT` | from `gen_ai.operation.name=invoke_agent`. Low confidence. |

The open risk is the root span. It also carries `gen_ai.request.model` and the turn's summed `gen_ai.usage.*`. If
Langfuse types it as `GENERATION` (the model fallback), the trace's token total counts the turn twice: once on the
root, once across its chat spans. The prediction is `AGENT`, with about 50% confidence. The results report what is
observed; the span code is not changed to steer the mapping.

The session view will probably stay empty. Langfuse maps `session.id` / `langfuse.session.id`, and the docs do not
mention `gen_ai.conversation.id`, which is what the spans carry.

## Q3. Do token counts arrive as numbers?

**Prediction: yes.** The exporter sends OTLP/protobuf `int_value`. For the `chat` and `audit` observations,
Langfuse's `usageDetails` holds integers equal to the JSONL span's `gen_ai.usage.input_tokens` / `output_tokens`.

Key names, predicted: `input` and `output`, plus a key for each cache count the span carries. The cache key names are
a guess (for example `input_cache_read`); confidence is low.

**Cost:** Langfuse may compute a USD cost from its own model price table if it knows the model name. Prediction:
a cost appears on the `chat` observations. Any such cost is Langfuse's price table, not a cost computed by pierre.
pierre's own cost in currency comes from its dated price table (`pierre/agent/prices.json`), built separately.

## Q4. Tests

- The new exporter test runs against a local fake OTLP endpoint on `127.0.0.1:<random port>`.
- It fails before the exporter exists (`ValueError: … unknown`) and passes after.
- `make test` stays green with the stack down.
- The committed traces stay byte-equal.
