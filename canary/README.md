# canary

pierre has no users, so there is no organic traffic to watch. The canary is scheduled traffic: the same five held-out
cases on a timer, so the input is the same in every 5-case row. A change between those rows comes from the model, the
SDK, the tools or the prompt, or from sampling alone: each row is one run (k = 1) of a sampled model. The
telemetry and the alerts built on this file are real; the traffic is scheduled, not organic.

- `cases.txt`: the five cases. Rule: the first 5 held-out ids of `evals/pierre-firm-agent/cases.json`, sorted as plain
  strings (fixed in `docs/observability/predictions-3.md` before any result was looked at; that file says the commit order
  is in a private source repository and can be shown on request). A test checks the file against the rule.
- `metrics.jsonl`: one JSON row per run, appended by `make canary`. It was created by the first live run; no row
  has been written from the committed 13 Nov traces. The root `README.md` states how many rows exist (a test checks it).
- `runs/<date>/`: the traces and the budget ledger of that run.

```sh
make canary           # 5 cases, k=1, claude-sonnet-5-5, live, capped (estimate x 1.5), graded by the pinned wilco
make canary FULL=1    # all 21 cases, k=1: use it when the model id or the SDK version changed
python scripts/analysis/canary.py run --preflight-only   # the call list and the cap, no key, no call
python scripts/analysis/canary.py estimate [--full]      # calls, billed input tokens and list-price cost, from the committed traces
```

One row per run date: `make canary` and `make canary FULL=1` refuse a date that already has a row, before they delete or
run anything. `make canary-check` judges a row that is already written, so the `make canary FULL=1` that its exit 3 asks
for runs only on a date without a row, at the earliest the day after that row.

## The row

| field | meaning |
|---|---|
| `date` | run date, UTC; one row per date |
| `pass_count`, `cases_graded` | sessions that passed all checks (wilco, `--strict`), of the sessions graded |
| `turns` | turns in the run |
| `cost_per_turn_usd_p50`, `_p95` | list-price USD per turn (agent calls plus auditor calls), nearest-rank percentiles; `null` if any turn's model has no price row |
| `latency_p95_ms` | wall clock of a whole turn (all model calls, tools and audits), nearest-rank p95; `null` if any turn lacks `meta.latency_ms` |
| `repair_loop_rate` | share of turns with at least one repair round |
| `termination_rate` | share of turns ending on a termination cause (loop stop, timeout, crash). A repair cap is not one: it is `repair_cap_rate` |
| `fabricated_flag_rate` | share of audited turns whose FIRST draft the claim auditor flagged with at least one fabricated claim |
| `model` | the model id the API returned (several ids are joined with a comma) |
| `sdk` | the `anthropic` SDK version recorded in the trace |
| `case_ids` | the cases run |
| `termination_causes` | terminated turns by cause, e.g. `{"timeout": 1}`; `{}` when none |
| `alerts` | `[]` at write time; `make canary-check` fills it |

**Note, 19 Nov 2026: the rows up to and including 2026-11-19 carry a known false positive.** In each of them every
`fabricated` flag and every repair round is the same one (counted by hand from `meta.rounds` of the committed
canary traces, on the day of this note): in `disambiguate-tessaly` the claim auditor called a true claim (for example "There is also a firm
called Tessaly Growth.") `fabricated`, because its evidence left out the `other_matches` that
`lookup_firm` returns. So those rows' `fabricated_flag_rate` and `repair_loop_rate` (0.1429, 1 of 7 turns) measure that
defect, not invented facts. The rows stay as measured. The audit context passes `other_matches` from 19 Nov 2026, so rows
after these are measured with the corrected context and are not comparable with them on those two fields. No alert rule
reads those two fields.

Nearest rank: the smallest value with at least p percent of the values at or below it, so with n values the p-th percentile
is the value at rank ceil(p * n / 100) of the sorted list.
