# pierre

The evaluation layer of a VC-matching system I built: it matches a startup pitch to European VC firms and writes an
LLM-generated report on each firm. The full system is private; this repo holds the parts that test whether its own
output can be trusted, plus a small ranking demo that runs on its own.

There are no real-world match outcomes behind any of this (the system has close to zero resolved matches). What it
can show is calibration: how often the fact-checker is right, and whether the matcher's thresholds ever fire.

## What the measurements found

**1. The fact-checker on the reports is accurate on a small labelled set, and its recall bound got worse in the port.**
In the full system every report goes through a claim auditor (`pierre/agents/_claim_auditor.py`): one Claude call (`claude-opus-5-5`)
extracts each factual claim and checks it against the evidence the report was built from, labelling it `verified`,
`unverifiable` or `fabricated`. Run over 57 claims written and labelled by Claude, not by hand, the committed capture
wrongly flagged 0 of 38 legitimate claims (target ≤ 15%) and caught 19 of 19 fabricated ones (target ≥ 80%).
The set is small, and its labels come from the same model family as the auditor under test: weaker evidence than a
human-labelled set. Zero false positives at that size still allow a true false-positive rate up to
~8%. The recall bound is weaker than before the port: the auditor used to run on Gemini Flash (19 of 19, one capture).
Re-running the port on the same fixtures twice gave 17 of 19 and then 19 of 19, so one capture is not a bound. The worse
capture allows a true recall as low as ~69% (Wilson, 95%), against ~84% for the old single capture. In each capture
one fixture came back with no extracted claim and no API error: a fabricated fixture, `q_fab_fund_size`, in the first,
and a fair-inference one, `q_fair_sector_adjacent`, in the second. The report lists both captures.
→ [report](docs/calibration/claim_auditor_calibration_report.md)

**2. The competitor warning never fired.**
The matcher was meant to flag a VC firm that had already backed one of the founder's competitors (a -20% penalty and
a "direct competitor" line in the report). It did that when the embedding similarity between the pitch and one of
the firm's portfolio companies reached 0.80. Across 170,088 real pitch↔company comparisons the highest similarity
was 0.733, so the warning could never trigger. The threshold had been tuned on company↔company pairs, which score
higher than a short pitch against a company description. Two related thresholds had the same problem.
→ [report](docs/calibration/threshold_reachability_report.md)

**3. No threshold could fix it.**
Against unrelated companies, a threshold of 0.608 looks fine: 18 of 20 labelled competitor pairs caught, no false
positives. That set is small: two test cases, and the 20 pairs are about 7 distinct companies counted once per investor.
The decisive test replays the real matcher on 6 test pitches and counts how many of the firms it had already
retrieved get flagged as conflicts. At 0.62, 45–95% of them are flagged; at 0.65, still 20–60%. Those firms invest
in the same sector, so they score in the same band as real competitors (0.57–0.67 in the labelled set). Raise the
bar to 0.68 and flagging drops to 0–35%, but no labelled competitor scores that high either. The harness's
`--check` exits 1 on purpose: that is the finding. In the private system the threshold was replaced by an LLM check
on the shortlisted pairs; that code is not in this repo.
→ [report](docs/calibration/competitor_threshold_calibration_report.md)

## Demo: rank your own company list

`pierre/byodata` takes a CSV or Excel file of companies and a paragraph describing what you are looking for, and
returns the list best-fit first. Every row is embedded and only the top 50 by cosine similarity are graded by the LLM
(in two batched calls), so a 5,000-row file costs the same as a 60-row one. Nothing is stored.

```
$ make demo
Embedder: offline hashing (lexical, deterministic)
Rows: 20 in file, 19 ranked, 1 skipped (too little description), 0 truncated

 #  similarity  grade  name — sector, stage, geo
 1      0.4000         Corvillex Grid — climate, Pre-seed, Spain
 2      0.3441         Umbrevia Bio — climate, Seed, Sweden
 3      0.3441         Bexmirra Materials — climate, Seed, UK
 4      0.2236         Yarvindel Clinics — healthtech, Seed, Italy
 ...
```

Without `GOOGLE_API_KEY` it uses a word-overlap embedder so it runs offline; that ranking is lexical, not semantic
(hence a healthtech company at #4). With a key it uses Gemini embeddings plus the LLM grade. The sample companies
are made up.

## The agent

A small tool-use agent over the same fictional data, built to be tested rather than shown off. Everything it knows
comes from 11 tools over 30 invented firms (`data/agent/firms.json`); no real firm, user or outcome is involved, and
nothing here has users.

**The tool-use loop** (`pierre/agent/loop.py`). A manual loop on `client.messages.create`, default model
`claude-opus-5-5`, effort pinned to `medium`, `tool_choice` `auto` only (a forced choice is a 400 on these models).
It stops at 8 model calls per draft. `stop_reason` is read before any tool runs: `refusal` ends the turn, and a
`max_tokens` response has its `tool_use` blocks left unrun because their input may be cut off. The `tool_use` blocks
of one response run in parallel and all their results go back in one user message, in call order. A repeated
identical call within a turn is served from a cache, and the trace still records what the model emitted. The loop
takes the tool list, a dispatcher and a system prompt as arguments and knows nothing about firms.

**The tools** (`pierre/agent/registry.py`). One registry holds each tool's name, description, closed input schema and
function; the Claude tool list and the MCP server (`python -m pierre.mcp_server`, stdio) are both generated from it,
and a test asserts the two lists are equal. Eight tools read, three write (shortlist add and remove, saved list).
Search is lexical, and its tool description says so.

**The repair loop** (`pierre/agent/repair.py`). After each draft, code (not the model) runs the claim auditor over the
tool results gathered so far. A claim the auditor marks `fabricated` is sent back as a new user message, and the
answer is audited again, until it is clean or two repair rounds have run (then the turn ends as `repair_cap`).
`unverifiable` claims are counted and reported but never sent back. The history is append-only: the draft stays, the
repair is a new message after it. The auditor is the ported one below; it is a judge with known error, not a gate.
Auditor calls are counted in `meta.auditor_calls` and are not agent steps, so `steps` (at most 16 per turn: 8 for the
draft, 4 for each repair round) counts only the agent's own model calls.

**Recorded turns.** `tests/fixtures/agent_recordings/` holds one file per demo session, recorded by `make record-agent`
from real model calls (never written by hand), with every request hash and response body but no headers, request ids
or keys. Replay serves them in order and refuses any request whose hash differs from the recorded one, so a changed
prompt, tool description or loop fails the replay tests until the sessions are recorded again. `make test` and
`make demo-agent` replay them with no key and no network, and a replay writes a trace byte-identical to the committed
one. `record_log.jsonl` has one line per recording with its sha256. Tests that need a repair round or the iteration cap
use a scripted client, which is a test double and is named as one.

**Traces.** `pierre/agent/trace.py` writes one JSON line per turn in the agent-trace record format a separate grader
reads (`source: external:pierre`, tool calls and results index-aligned, `steps`, `termination`, `final_state` on the last
turn). Timestamps come from a fixed demo clock so that replays are byte-stable.

**What the recordings show, and what they do not.** `make record-agent` records 8 train-case sessions (12 turns) on
`claude-opus-5-5`: 27 agent calls and 12 auditor calls in the committed files. No recording contains a repair round,
because the auditor flagged nothing `fabricated` in the final run of any of them, so the repair paths are tested with a
scripted client. That is a limit of the recordings. These are demo sessions, not an evaluation, and they carry no
claim about how well the agent works. Repair rounds did run live in the agent
eval of 13 Nov (4 of 108 turns) and in the AgentDojo run (1 of 160 sessions), both below. In 3 of those 4 eval turns the
only claim flagged in the repaired draft was true: until 19 Nov 2026 the auditor's evidence left out the `other_matches` that `lookup_firm`
returns, so a true claim (for example "The name also matches another firm, Tessaly Growth.") was called `fabricated` (the context now passes
it; that run was not repeated). Every canary row up to and including 2026-11-19 had this same false positive in its `disambiguate-tessaly` turn:
one repair round on 18 and 19 Nov, two (the cap) on 17 Nov (counted by hand from `meta.rounds` of the
committed canary traces, on the day of the last of those rows).

Two defects of mine showed up in the first recording and were fixed before the committed one: the audit context left
out the descriptions `list_shortlist` returns, so the auditor marked true claims `fabricated`, and the resulting repair
round produced a reply addressed to the checker instead of the user (the repair prompt now says to answer the user
directly; no recording runs that wording on a real repair). A turn that only called `find_recent_investments` also skipped
its audit. The two affected sessions were recorded again; their first recordings were discarded and are not in the repo (`record_log.jsonl` still holds their two log lines, which match no file).
`followup-pronoun-this-year` moved to the held-out split, so `recent-climate-deals` (train, one turn) replaces it.

**The claim auditor in the agent.** The auditor was ported from Gemini to Claude (see finding 1). On a 12-claim spot
check over contexts built from tool results, 6 true by the planted data and 6 false by it, it flagged 0 of 6 true claims
and caught 6 of 6 false ones. Twelve claims bound nothing; it is reported beside the 57-claim number and not merged with
it ([report](docs/calibration/agent_context_spot_check_report.md)). Its 6 true claims name only facts the audit context
keeps (HQ, cheque range, portfolio count, stages, investment year, a shortlist action, a tool error), so it did not test a
true claim about something the agent saw and the context left out, which is the false positive above. The auditor is an imperfect judge, never a gate.

**The live evaluation.** `make agent-eval` ran all 21 cases live: `claude-sonnet-5-5` three times each and `claude-opus-5-5` once
(84 sessions, 108 turns, 352 metered calls), graded by [wilco](https://github.com/Michiel-DK/wilco) at a pinned commit
(`pip install -e ".[eval]"` names it; `make install-eval` installs it as a checkout). Deterministic checks decide pass or fail;
the auditor's verdicts are counted beside the score and never gate it. Sonnet passed 9 of 11 train and 8 of 10 held-out
cases on a single run, and 8 of 11 and 7 of 10 on all three runs (pass^3); Opus, at one run, 9 of 11 and 8 of 10. The
Wilson 95% intervals are about 0.4 to 0.95 wide, so no gap between the models can be read from these numbers. Most failures
are the strict number check (`grounded_answer`) firing on correct restatements such as 1.5M for a stored 1500000; the report
lists them. Predictions were committed before the run (that order is in a private source repository and can be shown on
request) and are scored in the report, including the ones it refuted (the
repair loop fired on 4 of 108 turns, below the predicted 5% to 30%).
These are scores on 21 cases of fictional data, written by the same model family that sat the exam. They say nothing about real
firm data and nothing about real-world match outcomes. →
[report](docs/calibration/agent_eval_report.md), [predictions](docs/calibration/agent_eval_predictions.md)

**A public anchor.** AgentDojo's whole banking suite (160 sessions: 16 user tasks alone and with each of 9
injection tasks) was run through this same loop on `claude-sonnet-5-5`, graded by the benchmark's own checks, because the 21 pierre
cases are written in-house and a reader should see the loop on cases we did not write. Result: 16/16 utility without
injection, 0/144 targeted attacks succeeded (95% Wilson upper bound 2.6%), 134/144 utility under attack; k = 1, and the
repair loop ran once in the whole suite, so nothing is shown about repair under attack. The run used one attack,
AgentDojo's `important_instructions`; other attacks are untested. The defence arms (`ARM=confirm-on-mutate|spotlight`)
and the local-model adapter ship unrun: no session of theirs is committed, and with no targeted attack succeeding there
was nothing for a defence to stop, so a defence's effect on attack success is unmeasured on this suite and model. Details and limits:
[report](docs/calibration/public_anchor_agentdojo.md); predictions in `evals/public-anchor/PREDICTIONS.md` (those for the 48-session slice of 13 Nov fixed beforehand, those for the other 112 sessions written after the slice result; that order is in a private source repository and can be shown on request).
That run is committed and is never run again: `make anchor-eval` refuses it. `make anchor-eval MODEL=<id> ARM=<arm>` runs one new
defence arm; it needs `ANCHOR_ENV=<file with an ANTHROPIC_API_KEY line>` and spends tokens. `make reports` re-renders the report offline.

## Watching it run

pierre has no users, so there is no organic traffic to watch. The traffic here is a scheduled canary: the same 5 held-out
cases (`canary/cases.txt`) run live on weekdays by `make canary`, so the input is the same in every 5-case row. A change between those rows comes
from the model, the SDK, the tools or the prompt, or from sampling alone: each row is one run (k = 1) of a sampled model. The telemetry and the alerts are real; the traffic is a scheduled canary and not users.

**What is traced.** Every turn is one JSON line in the trace file (tool calls, usage, rounds, stop reasons, and on live runs
the wall-clock latency of the turn and each model call, and its cost in USD at dated list prices). The same turns are
emitted as OpenTelemetry spans, which an optional local Langfuse dashboard can show
([how](docs/observability/langfuse.md)). Each canary run appends one row to `canary/metrics.jsonl`: pass count, cost per turn
(p50, p95), p95 turn latency, repair-loop rate, terminations by cause, fabricated-claim rate, model id, SDK version
([the row](canary/README.md)).

**The four alert rules** (`make canary-check`; thresholds in `canary/thresholds.json`, dated 2026-11-17 in `canary/thresholds_added.json`, the date of the first
row: a test checks that no row is older; the commit order behind that date is in a private source repository and can be shown on request;
"previous 7 rows" means runs, not days, because the canary runs on weekdays only and only rows that ran the same cases count):

| rule | fires when | exit |
|---|---|---|
| pass count | at least 2 below the median of the previous 7 rows, on 2 runs in a row (one bad run is noise at n=5) | 1 |
| cost | cost per turn (the row's p50) above 1.5 x the median of the previous 7 rows | 1 |
| termination | any turn ended on `timeout` or `max_tokens` | 1 |
| model or SDK changed | the model id or the `anthropic` version differs from the previous row. Not a failure, exit code 3: run `make canary FULL=1` | 3 |

The reasons are written into the newest row's `alerts`. With fewer than 7 comparable previous rows the pass and cost rules
are not evaluated (a median of two or three runs at n=5 is noise) and the command says so; the termination and change
rules still run, so the first pass or cost alert can come on row 8 (cost) or row 9 (pass, which needs two runs in a row).
`make` reports any non-zero recipe as exit 2; run `python scripts/analysis/canary_check.py` to get the 1 or the 3.
The model and SDK rule compares with the previous row only. 3 rows exist in `canary/metrics.jsonl`, the first dated
2026-11-17.

**Chart.** Once there are 14 rows, the series (pass count and cost per turn by run) goes here. There is no chart yet.

## Run it

Python 3.11+.

```bash
make install   # venv + package + test deps + the anchor, obs and langfuse extras
make test      # unit tests: no network, no database, no API key
make demo      # the ranking demo above
make demo-agent   # replay two recorded agent sessions, offline
make reports   # re-render the reports (the agent-eval one from its committed traces and grades, no model call); fails if any changes
make agent-eval  # the live run: needs ANTHROPIC_API_KEY, spends tokens, capped at 1.5x its printed estimate
```

The claim-auditor result can be re-measured with an Anthropic key
(`python scripts/analysis/claim_auditor_calibration_harness.py`); `make record-agent` re-records the agent sessions. The two threshold reports were measured on the
private system's database; this repo keeps the snapshots, re-renders them and tests the verdict logic, but cannot
re-measure them.

## Layout

```
pierre/agent/      the tool registry, tool-use loop, repair loop, trace writer, record/replay; fictional firm store
pierre/agents/     claim auditor, its calibration fixtures, LLM retry helper
pierre/mcp_server.py  the same tools over MCP (stdio)
pierre/matching/   competitor-threshold calibration logic, LLM reranker
pierre/byodata/    the bring-your-own-list ranker
pierre/utils/      grounding checks, LLM cost tracking
scripts/analysis/  the harnesses / renderers
scripts/demo/      the ranking demo, and the agent record / replay scripts
tests/             unit tests, golden snapshots, recorded agent sessions
evals/             the agent's case file (graded outside this repo)
docs/calibration/  the generated reports
```

MIT licence.
