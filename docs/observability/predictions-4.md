# The four alert rules: predictions, written before any build commit

pierre has no users. The only traffic is a small scheduled canary of held-out cases (5 fixed cases, weekdays). The
telemetry and the alerts built on it are real; the traffic they measure is scheduled, not organic. This work
writes the alert thresholds and `make canary-check`. It makes **no live model call**, and the thresholds are committed
before any canary row exists.

Written 17 Nov 2026, after the spans, the Langfuse dashboard, the timeouts and cost table and the canary were merged,
before the tests or the code of the alert rules exist. The order of the commits is in a private source repository and can be shown on request.

_Edited for publication: framing words only (titles, internal process terms, private commit ids and file names); no predicted value, range, pass line or kill line changed._

## Facts checked before writing

- `canary/metrics.jsonl` was absent when this was written (no real row existed), so the thresholds precede the data.
- Baseline `make test` before this work: 442 passed (the canary's predictions said 445; the number moved with its own
  tests).
- The canary's metrics row records `termination_rate` but not the termination cause, and the `timeout` / `max_tokens`
  rule needs the cause. This work adds one field to the row, `termination_causes` (cause -> count), written by
  `build_row`; the one existing test that compares a whole row changes with it.
- The row has no mean cost per turn, only `cost_per_turn_usd_p50` and `_p95`. "Cost per turn" in rule 2 is the p50.

## Design decisions (fixed here)

1. Pass-drop rule: a row is "bad" when `pass_count <= median(previous 7 rows) - 2`. The rule fires when the newest row
   and the row before it are both bad, each against its own previous 7 rows.
2. Cost rule: fires when the newest row's p50 cost per turn is `> 1.5 x` the median of the previous 7 rows' p50 (strictly
   above; exactly 1.5x does not fire).
3. Only rows with the same `case_ids` as the newest row form a baseline. A `FULL=1` row (21 cases) is not comparable
   with a 5-case row, so it neither triggers nor pollutes rules 1 and 2.
4. Fewer than 7 comparable previous rows: rules 1 and 2 are not evaluated (a median of two or three runs at n=5 is
   noise), and the output says so. Rules 3 and 4 do not need a baseline and always run (rule 4 needs one previous row).
5. A null cost p50 (a turn's model had no price row) in the newest row: rule 2 is not evaluated and says so.
6. Exit codes: 0 nothing fired, 1 a failure rule fired, 3 only the model/SDK change rule fired (run `make canary FULL=1`),
   an absent or empty metrics file is exit 0 with "no rows". 1 takes precedence over 3.

## Predictions

| # | check | predicted |
|---|---|---|
| P1 | `make test` after the change | 470 passed (range 460 to 485), 0 failed |
| P2 | the red run: the new test files against code that does not exist yet | every new test fails (import or missing-file errors); of the 442 old tests exactly 1 fails (the whole-row comparison, once its expected row gets `termination_causes`) and 441 pass |
| P3 | boundary: a drop of exactly 2 against the median, twice in a row | fires; a drop of 1, or a drop of 2 once, does not |
| P4 | boundary: cost exactly 1.5 x the median | does not fire; 1.5 x plus one micro-dollar does |
| P5 | the first run on which rule 1 can fire | row 9 (rows 1 to 7 are the baseline of row 8, and row 9 needs row 8 to be bad against its own 7) |
| P6 | the first run on which rule 2 can fire | row 8 |
| P7 | false-alarm rate of rule 1 alone on a simulated healthy series, pass count ~ Binomial(5, 0.8) i.i.d., 20,000 runs, seed 0 | about 2% per run (range 0.5% to 5%) |
| P8 | `make reports` after the change | no diff in `docs/calibration/`; every committed trace byte-equal (`git diff --exit-code` on `data/`) |
| P9 | `make canary-check` on the data as committed then (no `metrics.jsonl`) | exit 0, prints "no rows" |
