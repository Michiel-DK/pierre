# Timeouts and the cost table: predictions, written before any build commit

pierre has no users. Once the agent runs, the only traffic is a small scheduled canary of held-out cases. The telemetry
and alerts built on it are real; the traffic they measure is scheduled, not organic. This work makes no model
call: the cost figure is derived from the committed 14 Nov live-eval traces, and the timeout tests use fake slow
clients and fake slow tools.

Written 16 Nov 2026, after the spans were merged and before the tests or the code of the timeouts and the cost table
exist. The order of the commits is in a private source repository and can be shown on request.

_Edited for publication: framing words only (titles, internal process terms, private commit ids and file names); no predicted value, range, pass line or kill line changed._

## What is added

An explicit timeout on the Anthropic client and a per-tool timeout, each ending the turn with termination cause
`timeout`. A dated price table `pierre/agent/prices.json` (read from the provider's pricing page on 16 Nov 2026) and a
`meta.cost` per turn, also set on the turn span. A model with no row gives `cost: null` and a warning.

## Facts to check, as I expect them (checked before this file was written)

- Installed SDK 2.8.0 default client timeout: 600 s total with a 5 s connect timeout (`anthropic/_constants.py`, `DEFAULT_TIMEOUT`).
- `anthropic.APITimeoutError` subclasses `APIConnectionError`, and `BudgetedClient` retries `APIConnectionError`, so a
  timed-out model call is today retried twice (three attempts) instead of ending the turn.

## Predictions

| # | check | predicted |
|---|---|---|
| P1 | `make test` after the change | 405 passed (range 400 to 411), 0 skipped, 0 failed. The baseline today is 391 passed (the spans' predictions said 389: that was 2 off) |
| P2 | the red run: the new test files against today's code | every new test fails; the 391 old tests still pass |
| P3 | fake slow client, turn run with a client timeout shorter than its delay | `termination.cause == "timeout"` on the trace record, 1 model call made, no retry |
| P4 | fake slow tool, same shape | turn ends with cause `timeout` after the tool timeout; the history stays valid (every `tool_use` has a `tool_result`) |
| P5 | a model missing from the price table | `cost: null` plus exactly one warning per model per process |
| P6 | cost of the 14 Nov run, all 108 turns, derived from the traces | about $8 (range $4 to $15). By bucket: Sonnet agent calls $1.5 to $3, Opus agent calls $1 to $3, auditor calls (always Opus) $3 to $8: the auditor is the largest bucket |
| P7 | count of model calls in the committed traces | between 500 and 650. The metered ledger in `run_summary.json` says 352 calls, so I expect the ledger NOT to match the traces (it is not a whole-run total) |
| P8 | cache fields in the 14 Nov usage | all zero: no cache cost in that run |
| P9 | the wilco trace format and committed traces | `make reports` re-renders with 0 lines of diff; replays of the 8 recordings stay byte-equal to the committed traces (`meta.cost` only appears on live runs, as `meta.latency_ms` does) |
| P10 | term gate (a private list of forbidden terms) on a fresh copy of the repository | exit 0, "clean" |
| P11 | `gitleaks git --staged` before each commit and `gitleaks git .` after | no leaks found |

If a prediction is refuted, the report says so and explains why. That counts as a result; it is not a reason to
adjust the prediction after the fact.
