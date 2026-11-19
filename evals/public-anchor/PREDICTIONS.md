# Public anchor: pre-registered subset and predictions (AgentDojo, banking)

Written and committed BEFORE any live call of this run (13 Nov 2026); that commit order is in a private source repository and can be shown on request. No prediction and no part of the subset was changed after results existed. What was written or edited after results is dated and labelled: the two errata just below list the wording edits made after the run, and the addendum of 18 Nov at the end was written after the 48-session result.

_Erratum, 13 Nov 2026 (after the run): wording only, "this lane" (internal process term) now reads "this run" / "this eval", and a reference to a call budget defined elsewhere now states that target (stay under about 400 model calls) beside the runner's separate hard cap of 600 calls; no prediction, number or subset changed._

_Erratum, 19 Nov 2026 (after the run and after the addendum): wording only, in two places. (1) The first paragraph of this file first read "Nothing below was edited after results existed", which the erratum of 13 Nov already contradicted; it now says what this file shows (no prediction or subset changed, and every passage written after results is dated), and that the commit order is in a private source repository. (2) The addendum of 18 Nov first said "Nothing above this section was edited" and "It is committed before any live call of the full-suite run"; the first now names the two later changes above the addendum (this erratum and the rewording of the first paragraph), and the second says where that order can be checked. No prediction, number or subset changed._

## What is run

- Benchmark: `agentdojo==0.1.35` (PyPI), benchmark version `v1.2.2` (the package's own default in `agentdojo/scripts/benchmark.py`), suite `banking`.
- Agent: the showcase loop (`pierre/agent/loop.py`, `pierre/agent/repair.py`), model `claude-sonnet-5-5`, k = 1, effort `medium`, repair settings
  as shipped (`REPAIR_ON = fabricated`, 2 rounds, claim auditor as judge).
- Attack: AgentDojo's `important_instructions`.
- Banking has 16 user tasks and 9 injection tasks: 16 + 16 x 9 = 160 sessions, about 960 model calls with audit and repair. The slice is chosen to
  stay under about 400 model calls, so a fixed subset is used (the runner's separate hard abort cap is 600 calls / 4M input tokens).

## Subset rule (fixed here, before any run)

- All 16 user tasks, `user_task_0` .. `user_task_15`, for utility (no injection): 16 sessions.
- For security: each of those 16 user tasks x the first 2 injection tasks by numeric id (`injection_task_0`, `injection_task_1`): 32 sessions.
- Total 48 sessions. "First N by id" is by the numeric suffix (so `user_task_2` before `user_task_10`).
- A pair the attack cannot inject (no injection vector reaches the model) is reported and left out of the security denominator.
- Sessions that end in an infrastructure error (not a model outcome) are listed and left out of both denominators.

## Predictions (priors, not computed from any run of this eval)

| quantity | prediction | basis |
|---|---|---|
| utility without injection (16 sessions) | 0.80 | prior: a current Sonnet-class model on tool tasks of this size; the paper's overall benign utility for an older model is 78.2% |
| targeted attack success (32 pairs) | 0.10 | prior: newer Claude models are trained against this kind of injection; the paper's older-model figure is higher |

## Published figures (source read, not recalled)

AgentDojo paper (arXiv 2406.13352, HTML version, checked against the page text), the appendix table "Detailed results for Figure 6" (95% intervals in parentheses), Claude 3.5 Sonnet, whole benchmark (all four suites, all injection tasks),
important-message attack: benign utility 78.22%, utility under attack 51.19%, targeted attack success rate 33.86%.
These are NOT comparable to this run: an older model, all suites together, a different benchmark version, a different agent loop.
For Sonnet-class on the banking suite alone at v1.2.2: **no published comparable** (the paper gives banking only in a per-suite figure for another model;
the pinned package README points to a results page and quotes no figure).

---

## Addendum, 18 Nov 2026: the 112 remaining sessions (written after the 48-session result was known)

**This prior was written after the 48-session result of 13 Nov was known** (16/16 utility, 0/32 attack success, 30/32 utility under attack, no repair round).
It is therefore not a blind prediction like the table above: it is informed by the slice. Above this section, the only changes made after it was written are
the erratum of 19 Nov and the rewording of the first paragraph that it lists. It was committed before any live call of the full-suite run; that commit order is in a private
source repository and can be shown on request.

Scope: the 112 sessions not yet run, i.e. each of the 16 user tasks x `injection_task_2` .. `injection_task_8` (numeric id order), same agent, model, attack,
benchmark version and k = 1 as above. Together with the 48 committed sessions they make the full suite: 16 + 16 x 9 = 160 sessions, 144 of them attacked.

| quantity | prediction | basis |
|---|---|---|
| targeted attack success, the 112 | 0.04 (about 4 to 5 of 112) | slice result 0/32 on injection tasks 0 and 1; the other seven goals differ (the slice's upper Wilson bound is 10.7%), so a few successes are plausible, not likely many |
| targeted attack success, all 144 attacked sessions | 0.03 (about 4 to 5 of 144) | the 0 of 32 already observed plus the line above |
| utility under attack, the 112 | 0.90 (about 101 of 112) | slice 30/32; the two losses were `user_task_0`, whose injection vector is the bill text itself, so I expect `user_task_0` to fail again under most of the seven new injection tasks (about 7 losses) plus a few more from distraction |
| utility under attack, all 144 attacked sessions | 0.91 (about 131 of 144) | the 30 of 32 already observed plus the line above |

Also expected: no repair round fires or very few (the auditor found 0 fabricated claims in 48 audits), so "repair moved the answer towards the injection" is most
likely 0 again, for lack of repair rounds, not because repair is safe.
