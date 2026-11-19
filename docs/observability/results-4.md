# The four alert rules: results against the predictions

pierre has no users; the traffic is a scheduled canary of 5 held-out cases, not users. No live call was made, and when
this was written no canary row existed (`canary/metrics.jsonl` was absent), so nothing here says how the agent behaves in operation. The first row has
the date of the thresholds, 2026-11-17; the order within that day is in a private source repository and can be shown on
request.

| # | predicted | observed |
|---|---|---|
| P1 | 470 passed (460 to 485) | 478 passed, 0 failed (442 old + 36 new) |
| P2 | red: every new test fails, 1 old test fails, 441 pass | 1 failed (the whole-row comparison), 441 passed, 1 collection error: the new test file did not import `pierre.agent.alerts`, so all 36 of its tests failed as one error, not individually |
| P3 | drop of exactly 2, twice, fires; once or by 1 does not | confirmed by tests |
| P4 | exactly 1.5 x does not fire; 1.5 x plus a micro-dollar does | confirmed by tests (0.15 against 0.10 does not, 0.151 does) |
| P5 | pass rule can first fire on row 9 | confirmed on a fixture series (all-4 pass counts, rows 8 and 9 at 0) |
| P6 | cost rule can first fire on row 8 | confirmed |
| P7 | about 2% false alarms per run (0.5% to 5%) | 1.06% (20,000 runs, seed 0, i.i.d. Binomial(5, 0.8)): inside the range, below the point estimate. It assumes independent runs at p=0.8, which no real series is known to follow |
| P8 | `make reports`: no diff | exit 0, `docs/calibration/` and `data/` unchanged |
| P9 | `make canary-check` with no file: exit 0, "no rows" | confirmed |

Differences worth knowing:

- `make` turns the script's exit code 1 or 3 into its own exit 2 (checked: script rc 1, make rc 2). The script prints
  the real code and the README says to run the script to get the 3. The "exit code 3" of design decision 6 in `predictions-4.md` holds for the script, not for `make`.
- The row gained `termination_causes` (the canary's row had only a rate); the rule cannot be written without it.
- "Cost per turn" is the p50 column, the only per-turn cost figure in the row.
- A rule firing needs 7 comparable rows (same `case_ids`); until then the pass and cost rules say "not evaluated".
- A failure and a model change on the same run exit 1 and both reasons are written.
