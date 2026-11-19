# The OpenTelemetry spans: predictions, written before any build commit

pierre has no users. Once the agent runs, the only traffic is a small scheduled canary of held-out cases. The telemetry
and alerts built on it are real; the traffic they measure is scheduled, not organic. This work makes no model
call: every number below comes from offline replays of committed recordings and from scripted test doubles.

Written 16 Nov 2026, before the tests or the code of the spans exist. The order of the commits is in a private source repository and can be shown on request.

_Edited for publication: framing words only (titles, internal process terms, private commit ids and file names); no predicted value, range, pass line or kill line changed._

## What is added

OpenTelemetry spans (GenAI semantic conventions) behind an optional `[obs]` extra. Per turn there is one span, with
child spans for each model call, tool call, auditor call and repair round. The default exporter writes a JSONL file.
`TraceWriter(live=True)` adds a new `meta.latency_ms` key, and only live runs (`agent_eval.py run`) set it.

## Predictions

| # | check | predicted |
|---|---|---|
| P1 | `make test` after the change | 389 passed (range 385 to 392), 0 skipped, 0 failed. The baseline today is 374 passed |
| P2 | the red run: the new test file against today's code | every new test fails (the module `pierre.agent.obs` does not exist yet); the 374 old tests still pass |
| P3 | spans for the one turn of the `tool-failure-timeout` replay | 19 spans: 1 `invoke_agent`, 5 `chat`, 12 `execute_tool`, 1 `audit`, 0 `repair_round`. Of the 12 tool spans, 2 carry `error.type`. Every span except the turn has the turn as its parent |
| P4 | spans for the `hq-and-ticket-quillmoor` replay | 5 spans: 1 `invoke_agent`, 2 `chat`, 1 `execute_tool`, 1 `audit` |
| P5 | the repair-round span, from a scripted session (no committed recording has a repair round: every `meta.rounds` in the 8 committed traces has length 1) | 1 `repair_round` span under the turn, holding that round's `chat` and `audit` spans |
| P6 | replay with tracing switched on, for the 8 committed recordings | 8 of 8 byte-equal to the committed `.trace.jsonl` |
| P7 | replay with the `[obs]` SDK hidden from import | runs, byte-equal to the committed trace, no span file written |
| P8 | `make reports` | re-renders with 0 lines of diff under `docs/calibration/` |
| P9 | term gate (a private list of forbidden terms) on a fresh copy of the repository | exit 0, "clean" |
| P10 | `gitleaks git --staged` before each commit and `gitleaks git .` after the last one | no leaks found |

If a prediction is refuted, the report says so and explains why. That counts as a result; it is not a reason to
adjust the prediction after the fact.
