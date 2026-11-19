# The canary run and its metrics row: results against the predictions

No live model call was made for this work. Every number below is derived from the committed 13 Nov traces
(`data/agent_eval/traces/claude-sonnet-5-5`, repeat 0, the 5 cases of `canary/cases.txt`) or from fixtures.
pierre has no users: the canary is scheduled traffic, and the telemetry and alerts built on it are real.

| # | predicted | observed |
|---|---|---|
| P1 | 445 passed (435 to 460) | 442 passed (420 old + 22 new). Inside the range |
| P2 | red run: new tests fail, 420 old pass | collection error on `tests/unit/test_canary.py` (`pierre.agent.canary` did not exist); 420 old passed |
| P3 | 8 turns (6 to 11), 15 files at k=3 | 7 turns, 5 sessions at k=1. Inside the range |
| P4 | 22 calls (14 to 35) | 24 calls: 16 agent + 8 auditor |
| P5 | 120,000 billed input tokens (60k to 250k) | 91,517 (output 9,842) |
| P6 | 90 calls full run (70 to 120) | 91 calls (62 agent + 29 auditor), 21 sessions, 27 turns |
| P7 | 500,000 billed input tokens (300k to 900k) | 337,208. Inside the range, near the low end |
| P8 | about $0.60 (0.25 to 1.50) | $0.4363 per canary run; full run $1.5923 (list price as of 2026-11-16, auditor priced as `claude-opus-5-5`) |
| P9 | `estimate()` over-counts calls and tokens by 1.5 to 3x | calls: 42 vs 24 observed (1.75x, as predicted). **Tokens: 292,000 (chars / 4) vs 91,517 billed = 3.19x, just outside the predicted range (REFUTED).** The cap (1.5 x estimate) is therefore 438,000 estimated tokens, about 4.8x what a run bills: the token cap is a loose runaway guard, not a tight budget. Call cap 63 vs 24 observed (2.6x) |
| P10 | `wilco grade --strict` accepts a 5-case file | accepted; "16 of 21 exam cases have no session in the trace file and are not graded", exit 0 |
| P11 | row from committed traces: `latency_p95_ms: null`, rest filled | confirmed: pass_count 4/5, cost p50 $0.0566, p95 $0.1062, repair rate 0.1429, termination 0.0, fabricated-flag 0.1429, model `claude-sonnet-5-5`, sdk `2.8.0`, alerts `[]` |
| P12 | percentiles hand-checked, nearest rank | below |
| P13 | wilco format and committed traces unchanged | `make reports` exit 0, no diff; the change's `git diff --stat` touches no file under `data/` or `tests/fixtures/` |
| P14, P15 | term gate clean; gitleaks clean | clean on every commit (staged) and `gitleaks git .` after. The term gate's list is private, so that check cannot be re-run from this repository |

## Percentiles by hand

Fixture (20 turns, turn i costs i x 0.05 USD and takes i x 100 ms): the p50 is the value at rank ceil(0.50 x 20) = 10,
0.50; the p95 is at rank ceil(0.95 x 20) = 19, 0.95; latency p95 is 1,900 ms. `test_the_row_on_the_fixture_matches_the_numbers_worked_by_hand` asserts these.

Committed traces, the 7 turn costs sorted: 0.0397, 0.0489, 0.0513, 0.0566, 0.0636, 0.0699, 0.1062. p50: rank ceil(3.5) = 4,
0.0566. p95: rank ceil(6.65) = 7, 0.1062. The code gives 0.056566 and 0.10622.

## What the alert rules need to know before their thresholds are written

- The 13 Nov Sonnet baseline on these 5 cases is 4 of 5, not 5 of 5: `disambiguate-tessaly` failed (`grounded_answer`) at repeat 0. A "pass_count < 5" alert would fire on the first row.
- 7 turns per run is a small sample: a p95 of 7 values is the maximum. Rates move in steps of 1/7 (0.143).
- There is no baseline for `latency_p95_ms`: the 13 Nov traces predate wall-clock latency. The first live row is the first latency figure.
- One definition to keep in view: `termination_rate` excludes the repair cap (`repair_cap_rate` is separate), because the trace writer records a repair cap in `meta`, not as a termination.
