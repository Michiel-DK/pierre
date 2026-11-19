# Langfuse self-hosted: the trace dashboard

pierre has no users. The traffic this dashboard shows is a small scheduled canary of held-out cases: the telemetry is
real, the traffic is scheduled.

The JSONL file exporter ([`spans-1.md`](spans-1.md)) stays the default. `make test` and CI never need Langfuse or Docker. The dashboard is a
second exporter behind the same seam (`obs.exporter_from_config`), and it receives the same spans.

## Start, check, stop

```sh
make install          # includes the [langfuse] extra (opentelemetry-exporter-otlp-proto-http, pinned)
make langfuse-up      # Docker: generates local credentials, starts 6 containers, waits for /api/public/health
make langfuse-check   # replays one committed turn into it (no model call) and compares the readback with the JSONL spans
make langfuse-down    # stops the stack and removes its containers, volumes and env file
```

**`make langfuse-up`**
- On first start it writes random credentials to `observability/.env.langfuse`. The file is git-ignored and mode 600,
  and its values are never printed.
- If the file already exists, it is reused. New keys would not match the project already stored in the volumes.
- The organization, the project `pierre-firm-agent`, its API keys and a login user are created at first start through
  Langfuse's headless-initialization variables. Nothing needs a click.
- The UI is at <http://localhost:3000>. Log in with `LANGFUSE_INIT_USER_EMAIL` / `LANGFUSE_INIT_USER_PASSWORD` from
  the env file.

**Sending the agent's spans there.** Set these in the environment of any run, for example the canary:
- `PIERRE_OTEL_EXPORTER=langfuse`, plus `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` from the env file.
- `LANGFUSE_HOST` defaults to `http://localhost:3000`.
- `PIERRE_OTEL_EXPORTER=jsonl,langfuse` (with `PIERRE_OTEL_FILE`) writes the file and sends to Langfuse, with
  identical span ids.

## The stack

The file is `observability/docker-compose.langfuse.yml`. It is derived from the upstream compose file (langfuse/langfuse
`main` at `ab7d58a3e670`, fetched 16 Nov 2026).

| service | image (pinned) | published |
|---|---|---|
| `langfuse-web` | `langfuse/langfuse:4.80.0` | `127.0.0.1:3000` only |
| `langfuse-worker` | `langfuse/langfuse-worker:4.80.0` | no |
| `postgres` | `postgres:17.12` | no |
| `clickhouse` | `clickhouse/clickhouse-server:26.3.62.3` | no |
| `redis` | `redis:7.4.12` | no |
| `minio` | `cgr.dev/chainguard/minio@sha256:1ec745a0…` (only `latest` exists, so it is pinned by index digest) | no |

Changes from upstream:
- Upstream publishes the UI and minio on all interfaces, and postgres, redis and clickhouse on `127.0.0.1`. Here only
  the UI is published, and only on `127.0.0.1`.
- `langfuse-ai-gateway`, a beta LLM proxy, is left out.
- Product telemetry is off (`TELEMETRY_ENABLED=false`).
- No secret has a default. Without the generated env file, compose refuses to start.

Langfuse's docs recommend at least 4 cores and 16 GiB for a VM. This machine's Docker has 8 CPUs and about 11.7 GiB,
which was enough for the round-trip check.

## What the turn view shows

One turn is one trace. The root observation's session id is the spans' `gen_ai.conversation.id`, so a multi-turn
conversation groups into one session. Langfuse's docs say the trace takes the root span's name; that was not
read back, because the v4 trace endpoint answers 404.

The table comes from the API readback (`/api/public/v2/observations`) on Langfuse 4.80.0, for the replayed turn
`hq-and-ticket-quillmoor`. Nobody looked at the UI pages for this check.

| span | Langfuse observation | what it shows |
|---|---|---|
| `invoke_agent pierre-firm-agent` (root) | `AGENT` | the turn, its start and end. No usage of its own, so the turn's summed tokens are not counted a second time |
| `chat claude-opus-5-5` (×2) | `GENERATION` | model, `max_tokens` as a model parameter, `usageDetails` `input` / `output` / `input_cache_creation` / `input_cached_tokens` / `total` as numbers, and a cost (see below) |
| `execute_tool lookup_firm` | `TOOL`, **named `lookup_firm`** | Langfuse names it after `gen_ai.tool.name`, not after the span name |
| `audit` | `GENERATION` | the auditor's model and tokens (`input` / `output` / `total`) |

Per Langfuse's docs, the other span attributes (`pierre.*`, the verdict counts) are stored under the observation's
`metadata.attributes`. The readback did not request metadata, so that was not checked.

**Cost.** Langfuse shows a USD cost on each generation. That cost comes from **Langfuse's own model price table**;
pierre computes no cost in currency here. pierre's own cost in currency comes from its dated price table (`pierre/agent/prices.json`; see [`results-2.md`](results-2.md)).

**Deprecated endpoint.** Langfuse 4.80.0 answers `GET /api/public/traces/{id}` with 404, as its docs say for the v4
`events_only` write mode. The check reads observations with `GET /api/public/v2/observations?traceId=…`.

## Round trip, observed 16 Nov 2026

`make langfuse-check` replayed `hq-and-ticket-quillmoor` with `PIERRE_OTEL_EXPORTER=jsonl,langfuse`, then read the
trace back. All 5 observations were readable after 2.1 s. The committed trace was still byte-equal after the replay.

| span (JSONL) | Langfuse name | id equal | parent equal | type | JSONL tokens in / out | Langfuse `usageDetails` |
|---|---|---|---|---|---|---|
| `invoke_agent pierre-firm-agent` | same | yes | yes (root) | AGENT | 6083 / 155 | none |
| `chat claude-opus-5-5` | same | yes | yes | GENERATION | 2915 / 61 | `input 2915, output 61, input_cache_creation 0, input_cached_tokens 0, total 2976` |
| `execute_tool lookup_firm` | `lookup_firm` | yes | yes | TOOL | n/a | none |
| `chat claude-opus-5-5` | same | yes | yes | GENERATION | 3168 / 94 | `input 3168, output 94, input_cache_creation 0, input_cached_tokens 0, total 3262` |
| `audit` | same | yes | yes | GENERATION | 4539 / 523 | `input 4539, output 523, total 5062` |

- The `totalUsage` summed over the trace's observations is 11,300, which is 2,976 + 3,262 + 5,062.
- The root's 6,238 tokens are not counted again.

## When the dashboard is down or slow

- The JSONL file exporter stays synchronous.
- The Langfuse exporter runs on a background `BatchSpanProcessor`. An unreachable endpoint makes the OTLP client retry
  with backoff, and that must not add to a turn's measured latency.
- Measured on a replay of the same turn, with the endpoint unreachable:
  - Every exporter synchronous: the turn took **36.7 s**.
  - With the batch processor: the turn took **0.86 s**, and the retries cost **8.4 s once**, at shutdown.
- With a rejecting endpoint (HTTP 401), the agent's answers and the trace file do not change. A unit test checks
  this. The test does not check what the SDK logs.

## Predictions against what was observed

The predictions are in [`predictions-1b.md`](predictions-1b.md), committed before the tests and the code; that commit order is in a
private source repository and can be shown on request.

| # | predicted | observed |
|---|---|---|
| Q1 | 6 services | **confirmed**: 6 containers, only `langfuse-web` published, on `127.0.0.1:3000` |
| Q2 | 1 trace, 5 observations, ids = span ids, parents = the turn | **confirmed** |
| Q2 | types: `chat` and `audit` GENERATION, `execute_tool` TOOL, `invoke_agent` AGENT (about 50%) | **confirmed**. The root has no usage, so there is no double count |
| Q2 | the session view stays empty | **refuted**: the root's `sessionId` is the `gen_ai.conversation.id` |
| Q2 | the trace is named after the root span | **not checked**: no trace endpoint in v4 `events_only` mode |
| not predicted | the tool observation's name | renamed to `lookup_firm` (`gen_ai.tool.name`) |
| Q3 | tokens as numbers, `input` / `output` | **confirmed** |
| Q3 | cache keys such as `input_cache_read` | **refuted**: `input_cache_creation` and `input_cached_tokens` |
| Q3 | a Langfuse cost on the `chat` observations | **confirmed**, and also on `audit` |
| Q4 | the new tests fail before the exporter exists | **confirmed**: `ValueError: … unknown`, 5 of 5 failed |
| Q4 | `make test` green with the stack down | **confirmed**: 397 passed |

Two expectations in the test file changed after the red run:
- The header names are compared case-insensitively, because the exporter sends them in lower case.
- The `sorted()` over protobuf values raised `TypeError`, a bug in the test, which was fixed.

The latency test was written together with the batch-processor change, so it has no committed red state. Its red run
is a simulation, with the batch processor replaced by the synchronous one: the turn took 3.03 s, against a limit of
1.0 s at the time. The limit is now 2.0 s, which still separates the two paths, because the synchronous one cannot
finish in under 2.5 s.

## Sources (fetched 16 Nov 2026; none of the pages shows a last-updated date)

- Docker Compose deployment: <https://langfuse.com/self-hosting/deployment/docker-compose>, and the compose file it
  points to: <https://raw.githubusercontent.com/langfuse/langfuse/main/docker-compose.yml> (`main` at `ab7d58a3e670`).
- Headless initialization (`LANGFUSE_INIT_*`): <https://langfuse.com/self-hosting/administration/headless-initialization>
- OpenTelemetry ingestion: <https://langfuse.com/integrations/native/opentelemetry>. It covers:
  - the endpoint `/api/public/otel/v1/traces`, over HTTP/protobuf or HTTP/JSON (no gRPC);
  - basic auth;
  - `x-langfuse-ingestion-version: 4`;
  - the `gen_ai.*` mapping.
- Public API and v2 observations: <https://langfuse.com/docs/api-and-data-platform/features/public-api>,
  <https://langfuse.com/docs/api-and-data-platform/features/observations-api>
