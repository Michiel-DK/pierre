# Timeouts and the cost table: results against the predictions

pierre has no users, so nothing here measures organic traffic. What is measured here is the committed 13 Nov eval traces
(P6, P7), not canary traffic. No model call was made for this work. Predictions: `predictions-2.md`, committed first. The order of the commits is in a private source repository and can be shown on request.

## Facts checked

- Erratum to `predictions-2.md`, which says "14 Nov": the live eval ran on **13 Nov** (`data/agent_eval/run_summary.json` `at` 2026-11-13T12:11:46Z; in the private source history the commit of the run is dated 2026-11-13 and no commit is dated 14 Nov). The predictions file keeps its original wording; everything from here on says 13 Nov.
- Cases: 21 in `evals/pierre-firm-agent/cases.json`, 11 train and 10 heldout (counted).
- OpenTelemetry: the spans work, merged before this one, added it (`pierre/agent/obs.py`, the `[obs]` extra; see `spans-1.md`). This work adds `pierre.turn.cost.usd` to the turn span.

- SDK default timeout, installed `anthropic` 2.8.0: `anthropic/_constants.py:10`, `DEFAULT_TIMEOUT = httpx2.Timeout(timeout=10 * 60, connect=5.0)`: 600 s, 5 s connect, and httpx applies it per phase, not to the call as a whole. `_base_client.py:859` `_calculate_nonstreaming_timeout` only runs while the client timeout is still the default; it raises for a non-streaming call that could take over 10 minutes. An explicit client timeout skips it.
- `anthropic.APITimeoutError` subclasses `APIConnectionError` (`_exceptions.py:146`), and `BudgetedClient` retried `APIConnectionError`: a timed-out call used to be tried three times. It now ends the turn.
- Live traces now carry `meta.latency_ms` (added with the spans); the 13 Nov committed traces predate it, so they carry no latency.

## Predictions

| # | predicted | observed |
|---|---|---|
| P1 | 405 passed (400 to 411) | **refuted**: 414 passed, 0 skipped, 0 failed (24 new tests, 3 changed). I undercounted my own tests |
| P2 | every new test fails, the 391 old pass | partly: both new files failed at collection (module missing), which interrupts the whole run, so the 391 were not run in that state; after the code, all 391 old tests pass except the 3 listed below that I changed on purpose |
| P3 | slow client: cause `timeout`, 1 call, no retry | confirmed (`test_a_slow_model_call_ends_the_turn_with_timeout`, `test_budgeted_client_does_not_retry_a_timeout`) |
| P4 | slow tool: cause `timeout`, valid history | confirmed (`test_a_slow_tool_ends_the_turn_with_timeout_and_a_valid_history`) |
| P5 | unknown model: `cost: null`, one warning | confirmed |
| P6 | about $8 (4 to 15); Sonnet agent 1.5 to 3, Opus agent 1 to 3, auditor 3 to 8, auditor largest | total **$6.75**: in range. Sonnet agent **$1.43**: just below its range (refuted). Opus agent $1.02 (in range), auditor $4.30 (in range, largest bucket) |
| P7 | 500 to 650 model calls in traces; ledger will not match | **refuted**: 355 calls in the traces, ledger 352 (3 apart, not explained by files in this repo) |
| P8 | cache fields all zero | confirmed (every `cache_*` total is 0 in `cost_from_traces.py --json`) |
| P9 | `make reports` no diff; replays byte-equal | confirmed: no diff under `docs/calibration/`, no change under `data/`, replay-byte-equality tests pass |
| P10 / P11 | term gate clean; gitleaks clean | confirmed when this was written: the term gate, run on a fresh copy of the repository, said `clean`, exit 0; `gitleaks git .` no leaks found. The term gate's list is private, so that check cannot be re-run from this repository; `gitleaks` can |

## Cost of the 13 Nov run (derived from committed traces, prices as of 2026-11-16)

`docs/observability/cost-of-the-13-nov-run.md`: Sonnet 5.5 (63 sessions, 81 turns) $4.61 of which auditor $3.18; Opus 5.5 (21 sessions, 27 turns) $2.14 of which auditor $1.12; total **$6.75**. The auditor is priced as Opus 5.5, the fixed judge model; the traces do not record it per call (assumption).

## Changes to existing tests

Three tests encoded the old behaviour and changed with it: `test_a_crashing_model_call...` used a `TimeoutError` as its "crash" (a timeout now ends the turn, so it uses `RuntimeError`); `test_budget_counts_retries` used `TimeoutError` as its retryable error (a timeout is no longer retried, so `ConnectionError`); `test_latency_goes_into_meta_in_live_mode_only` now also expects `meta.cost` (null for the fake model) on live runs.

## Notes

- A first version ran each tool in an extra thread. That reordered parallel shortlist writes and made replay tests fail about half the time (0 of 25 runs failed with timeouts off, 12 of 25 with the extra thread). Tools now run inline in one daemon thread per call and the joiner enforces the deadline; 40 full runs of `tests/unit` then passed. `test_byodata_rank::test_nothing_is_written_to_disk` failed once in an earlier loop; it lists the shared temp directory, so another process writing there can fail it. It is not part of this change.
- A call past its deadline is abandoned, not killed: a model call may still bill and a writing tool may still write. The deadline for tools counts from the start of the batch of one model response.
- The auditor's model calls get the client timeout only (no loop deadline), on a client from `client_from_env`; the AgentDojo anchor builds its own clients, which keep the SDK default. The claim auditor used to swallow every exception into an audit error (`_claim_auditor.py`, the `except Exception` after the model call); a timeout now propagates (like `abort_run`) and `AgentSession._audit` ends the turn with cause `timeout` (`test_a_timed_out_audit_call_ends_the_turn_with_timeout`). The auditor's own retry helper (`_llm_retry.py`, 3 attempts with backoff on any exception) no longer retries a timeout either. Both edits also change the auditor and the retry helper for the calibration scripts: a timed-out call raises (no retry) instead of returning an `error` result after three attempts.
- Token semantics: `output_tokens` is inclusive of thinking tokens (`anthropic/types/usage.py:34`), so thinking is not added again. That `input_tokens` excludes cache tokens is read from the pricing page's cache example, not from the SDK docstring; every cache field in the 13 Nov traces is 0, so the derived figure does not depend on it.
