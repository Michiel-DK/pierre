# Cost of the 13 Nov live eval, re-derived from the committed traces

*derived from committed traces, prices as of 2026-11-16.* No model call was made to produce this page:
`python scripts/analysis/cost_from_traces.py --render-md` reads `meta.usage` and `meta.auditor_usage` from
`data/agent_eval/traces/` and prices the tokens with `pierre/agent/prices.json` (list price, standard tier).
The claim auditor is priced as `claude-opus-5-5`, the fixed judge model; the traces do not record the judge per call.
pierre has no users: this run was the fixed eval set (21 cases), not organic traffic.

| agent model | sessions | turns | agent calls | auditor calls | agent USD | auditor USD | total USD |
|---|---|---|---|---|---|---|---|
| claude-opus-5-5 | 21 | 27 | 61 | 27 | 1.02 | 1.12 | 2.14 |
| claude-sonnet-5-5 | 63 | 81 | 182 | 85 | 1.43 | 3.18 | 4.61 |
| **all** | | 108 | | | | | **6.75** |

Model calls counted in the traces: 355. The metered ledger in `run_summary.json` counts 352; the +3 difference is not explained by the files in this repo.
