# Agent eval: predictions, written before the run

> Hand-written. The commit that adds this file was pushed before the first live call of `make agent-eval`. This repo is a
> cut from a private source repo, so that order is checkable there, not in this repo's history: the predictions commit `3e748ee`
> is dated 2026-11-13 11:59:22 UTC, and the live run completed at 12:11:46 UTC (`data/agent_eval/run_summary.json`). The source
> history can be shown on request (this sentence on where the order is checkable was added after the run). Below, only the wording listed in the erratum just
> below was edited after the run (this sentence first read "Nothing below was edited after the run"; narrowed on 19 Nov 2026). The result report is
> `agent_eval_report.md`; where it disagrees with this file, this file stays as written and the report says so.
>
> **Erratum, 13 Nov 2026 (after the run): wording only.** References to unpublished planning notes were reworded for
> the public copy (an internal job id; a planning note, now "pre-run estimate"). No prediction, number or threshold changed.

## What will be run

- 21 cases (11 train, 10 held-out), 27 turns, `evals/pierre-firm-agent/cases.json` (status `REVIEWED`, 13 Nov 2026).
- `claude-sonnet-5-5` at k=3 (63 sessions) and `claude-opus-5-5` at k=1 (21 sessions): 84 sessions, 108 turns.
  Opus at k=1 means its pass^k equals its pass@1; it says nothing about run-to-run spread.
- Live, on the fictional in-memory firm store. The auditor is `claude-opus-5-5` for both agent models.
- Graded by `wilco grade --traces` at the pinned public wilco commit (see `pyproject.toml`, extra `[eval]`).
  The wilco checks decide pass/fail. Nothing a model says is a verdict.
- Intervals are Wilson 95% on cases (n = 11 train, n = 10 held-out), train and held-out never summed.

## Budget, derived from the pre-run per-turn estimate

Pre-run estimate: about 6.0 model calls and about 40k estimated input tokens per turn, plus about 6k for each later turn of a
multi-turn case (chars / 4 of every request, history resent). 108 turns, 24 of them later turns:
**about 648 calls and about 4.46M estimated input tokens. Cap = estimate x 1.5 = 972 calls / 6.70M.**
The run aborts at the cap. Billed input has run about 1.5x the estimate before; the cap is on the estimate.

## Stage mapping, fixed before any result

A failed wilco check is filed under one stage of the agent's work:

| stage | wilco checks |
|---|---|
| selection | `tools_called`, `tools_not_called`, `tools_allowed`, `redundancy` |
| arguments | `args_match` |
| execution | `error_recovery`, `termination`, `max_steps`, `state_match`, `no_injected_write` |
| output handling | `answer_contains`, `answer_contains_any`, `answer_not_contains`, `facts_not_contradicted`, `grounded_answer`, `answer_present`, `scope_coverage` |

A session that failed several checks is filed under the stage of its first failing turn's most fundamental check
(wilco's own `root_check`); the table also lists every failed check, so nothing is hidden by that choice.

## Predictions

Counts are cases passed. Ranges are what I would not be surprised by; a number outside its range is a refuted prediction.

| # | quantity | prediction | range |
|---|---|---|---|
| P1 | Sonnet, train, pass@1 | 8 of 11 | 6 to 10 |
| P2 | Sonnet, train, pass^3 | 6 of 11 | 4 to 9 |
| P3 | Sonnet, held-out, pass@1 | 6 of 10 | 4 to 8 |
| P4 | Sonnet, held-out, pass^3 | 4 of 10 | 2 to 7 |
| P5 | Opus, train, pass@1 | 9 of 11 | 7 to 10 |
| P6 | Opus, held-out, pass@1 | 7 of 10 | 5 to 9 |
| P7 | held-out score is lower than train score for both models | yes | n/a |
| P8 | Opus and Sonnet intervals overlap on both splits (no measurable gap at these n) | yes | n/a |
| P9 | pass^3 is below pass@1 for Sonnet on at least one split | yes | n/a |
| P10 | share of turns with at least one repair round (both models together) | 15% | 5% to 30% |
| P11 | turns that end at the repair cap | 3 of 108 | 0 to 8 |
| P12 | turns with `fabricated` claims in the first draft | 12% of turns | 4% to 25% |
| P13 | turns with `fabricated` claims in the final answer | 3% of turns | 0% to 8% |
| P14 | `unverifiable` claims per turn, first draft vs final: roughly unchanged, because they are reported and never repaired | no change beyond noise | n/a |
| P15 | repair damage: turns that pass wilco on the first draft and fail on the final answer | 1 | 0 to 3 |
| P16 | sessions ending on `iteration_cap`, `max_tokens` or `refusal` | 2 of 84 | 0 to 6 |
| P17 | most common stage of a failed session | output handling | n/a |
| P18 | `recent-climate-deals` fails `args_match` again on at least one model | yes | n/a |
| P19 | billed input tokens exceed the estimate | by 1.3x to 1.8x | n/a |

Basis, so a miss can be read: the eight earlier recorded Opus sessions (`record_log.jsonl`) graded 7 of 8 on train (the one miss was
`recent-climate-deals`, `args_match`); no held-out case and no Sonnet session had run; no repair round has been
observed live yet; the calibration report puts 11 of 18 fair inferences as `unverifiable` and 0 of 38 legitimate
claims as `fabricated` on the single-firm contexts. Held-out cases were written to be harder (injection, pronouns
across turns), so P7 follows from the design, not from data.

## What would count as a result that matters

- A model gap (P8 refuted) would be the first thing in this repo that separates the two models. At n = 10 or 11 an
  interval of about 0.3 wide cannot show a gap smaller than about 4 cases.
- No repair rounds at all (P10 near 0) would mean the repair loop is untested by this exam, not that it works.
