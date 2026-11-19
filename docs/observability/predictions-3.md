# The canary run and its metrics row: predictions, written before any build commit

pierre has no users. Once the agent runs, the only traffic is a small scheduled canary of held-out cases. The telemetry
and alerts built on it are real; the traffic they measure is scheduled, not organic. This work builds the canary
and the writer of its metrics row. It makes **no live model call**: the alert thresholds (written separately) are written
before the first real row exists. The row writer is tested on the committed 13 Nov traces and on fixture traces.

Written 17 Nov 2026, after the spans, the Langfuse dashboard and the timeouts and cost table were merged, before the
tests or the code of the canary exist. The order of the commits is in a private source repository and can be shown on request.

_Edited for publication: framing words only (titles, internal process terms, private commit ids and file names); no predicted value, range, pass line or kill line changed._

## The case-selection rule (fixed here, before any result is looked at)

The canary runs the **first 5 held-out cases of `evals/pierre-firm-agent/cases.json`, sorted by case id (plain string
order)**, k=1, on `claude-sonnet-5-5`. The rule is mechanical and does not look at any grade or trace. Applied today it
selects, and `canary/cases.txt` will list exactly:

1. `disambiguate-tessaly`
2. `followup-pronoun-this-year`
3. `hq-and-ticket-quillmoor-paraphrase`
4. `injection-in-firm-description`
5. `injection-then-legit-write`

`make canary FULL=1` runs all 21 cases at k=1 on the same model (for when the model id or the SDK version changed).
A change to the case file's held-out set does not silently change the canary: the runner refuses when `cases.txt` names
a case that is not held-out or does not exist.

## Facts to check, as I expect them (checked before this file was written)

- Baseline `make test` before this work: 420 passed. (The timeouts and cost predictions said 405 and the baseline they
  quoted was 391; the suite grew.)
- The timeouts and cost predictions call the committed live run "14 Nov". The traces and `docs/observability/cost-of-the-13-nov-run.md` say 13 Nov.
  I use 13 Nov.
- Before the timeouts and cost table, `BudgetedClient` had no explicit timeout and there was no cost in currency. Both
  changed with that work (`timeouts.py`, `cost.py`, `prices.json`). Trace `meta` now also gets `latency_ms` and `cost`, but only on live runs.
- No committed trace has `meta.latency_ms` or `meta.cost` (they predate that work). So "latency p95" cannot be derived
  from the committed traces; the writer must give `null` there and not invent a number.

## Predictions

| # | check | predicted |
|---|---|---|
| P1 | `make test` after the change | 445 passed (range 435 to 460), 0 failed |
| P2 | the red run: the new test files against code that does not exist yet | every new test fails (import or missing-file errors); the 420 old tests still pass |
| P3 | the 5 canary cases in the committed Sonnet traces | 8 turns in total (range 6 to 11), 15 trace files at k=3 (5 sessions at k=1) |
| P4 | model calls (agent + auditor) of one canary run, derived from the committed traces | 22 calls (range 14 to 35) |
| P5 | billed input tokens of one canary run, same derivation | 120,000 (range 60,000 to 250,000) |
| P6 | calls of one full run (21 cases, k=1, Sonnet) | 90 (range 70 to 120) |
| P7 | billed input tokens of one full run | 500,000 (range 300,000 to 900,000) |
| P8 | list-price cost of one canary run from the committed usage, auditor priced as the fixed judge model | about $0.60 (range $0.25 to $1.50) |
| P9 | `estimate()` (the 13 Nov formula: 6 calls and 40k tokens per turn) against P4/P5 | over-estimates the calls and the tokens of the canary, by a factor of 1.5 to 3 |
| P10 | `wilco grade --strict` on a 5-case trace file (the other 16 cases absent) | accepted, with the 16 absent cases reported as not graded; it does not refuse |
| P11 | the row built from the committed traces | `latency_p95_ms: null`; every other field filled; `alerts: []` |
| P12 | percentiles | computed by hand on one fixture in the report and equal to the code's output; the definition is nearest-rank (the smallest value with at least p percent of the values at or below it) |
| P13 | the wilco trace format and every committed trace | `make reports` re-renders with 0 lines of diff; nothing under `data/agent_eval/traces/` or `tests/fixtures/agent_recordings/` is touched |
| P14 | term gate (a private list of forbidden terms) on a fresh copy of the repository | exit 0, "clean" |
| P15 | `gitleaks git --staged` before each commit and `gitleaks git .` after | no leaks found |

If a prediction is refuted, the report says so and explains why. That counts as a result; it is not a reason to
adjust the prediction after the fact.
