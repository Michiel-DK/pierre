"""The canary run and its metrics row, offline: the case rule, the percentile definition, the row writer on fixture traces
(hand-checked numbers) and on the committed 13 Nov traces, the call and token estimate, and the run wiring against a fake
client. No model call, no key, no wilco."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from pierre.agent import canary
from pierre.agent.factory import load_cases
from tests.unit._fake_claude import FakeClient, message, text

ROOT = Path(__file__).resolve().parents[2]
TRACES = ROOT / "data" / "agent_eval" / "traces"
MODEL = "claude-sonnet-5-5"
FIRST_FIVE = [
    "disambiguate-tessaly",
    "followup-pronoun-this-year",
    "hq-and-ticket-quillmoor-paraphrase",
    "injection-in-firm-description",
    "injection-then-legit-write",
]


# --- the case rule -------------------------------------------------------------------------------------------------------


def test_the_rule_is_the_first_five_heldout_case_ids_sorted():
    cases = list(load_cases().values())
    assert canary.select_cases(cases) == FIRST_FIVE
    # the rule ignores file order and ignores train cases
    assert canary.select_cases(list(reversed(cases))) == FIRST_FIVE
    assert canary.select_cases([{"id": "b", "split": "heldout"}, {"id": "a", "split": "train"}, {"id": "c", "split": "heldout"}], n=5) == ["b", "c"]


def test_the_committed_case_list_is_what_the_rule_selects():
    assert canary.read_case_list(ROOT / "canary" / "cases.txt") == FIRST_FIVE


def test_the_case_list_refuses_a_train_or_unknown_case():
    cases = load_cases()
    canary.validate_case_list(FIRST_FIVE, cases)
    with pytest.raises(ValueError, match="not held-out"):
        canary.validate_case_list(["compare-two-firms"], cases)
    with pytest.raises(ValueError, match="unknown"):
        canary.validate_case_list(["no-such-case"], cases)


def test_read_case_list_skips_blank_lines_and_comments(tmp_path):
    p = tmp_path / "cases.txt"
    p.write_text("# why these\n\na\n b \n")
    assert canary.read_case_list(p) == ["a", "b"]


# --- percentiles: nearest rank -------------------------------------------------------------------------------------------


def test_percentile_is_nearest_rank():
    twenty = [i * 0.05 for i in range(1, 21)]
    assert canary.percentile(twenty, 50) == pytest.approx(0.50)  # rank ceil(0.50 * 20) = 10
    assert canary.percentile(twenty, 95) == pytest.approx(0.95)  # rank ceil(0.95 * 20) = 19
    assert canary.percentile(list(reversed(twenty)), 95) == pytest.approx(0.95)  # input order does not matter
    assert canary.percentile([4, 1, 3], 50) == 3  # rank ceil(1.5) = 2 of [1, 3, 4]
    assert canary.percentile([7], 95) == 7
    assert canary.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95) == 10  # rank ceil(9.5) = 10
    assert canary.percentile([], 50) is None


# --- the row, on a hand-built fixture ------------------------------------------------------------------------------------


def _audit(fabricated=0, error=None):
    a = {"verified": 1, "fabricated": fabricated, "unverifiable": 0, "claims": []}
    if error:
        a["error"] = error
    return a


def _fixture_records(model=MODEL):
    """20 turns. Turn i costs i * 0.05 USD and takes i * 100 ms. Turns 1-10 are audited; turns 3 and 7 had a fabricated claim in
    the first draft and went to repair (3 rounds on turn 7, which ends at the repair cap); turn 9 ends on a loop stop."""
    out = []
    for i in range(1, 21):
        rounds = [{"round": 0, "answer": "x", "audit": _audit(0) if i <= 10 else None, "steps": 1}]
        if i == 3:
            rounds[0]["audit"] = _audit(2)
            rounds.append({"round": 1, "answer": "y", "audit": _audit(0), "steps": 1})
        if i == 7:
            rounds[0]["audit"] = _audit(1)
            rounds += [{"round": 1, "answer": "y", "audit": _audit(1), "steps": 1}, {"round": 2, "answer": "z", "audit": _audit(1), "steps": 1}]
        meta = {
            "rounds": rounds, "auditor_calls": 0, "dedup_hits": 0, "stop_reasons": ["end_turn"],
            "usage": [{"input_tokens": 100, "output_tokens": 10}], "auditor_usage": [],
            "latency_ms": {"turn": i * 100.0, "model_calls": [i * 100.0], "auditor_calls": []},
            "cost": {"total_usd": round(i * 0.05, 6), "agent_usd": round(i * 0.05, 6), "auditor_usd": 0.0, "currency": "USD", "prices_as_of": "2026-11-16"},
        }
        if i == 7:
            meta["repair_cap"] = True
        out.append({
            "session_id": f"s{(i - 1) // 2}", "case_id": f"c{(i - 1) // 2}", "turn": (i - 1) % 2, "model": model, "repeat": 0,
            "termination": {"cause": "max_iterations"} if i == 9 else None, "meta": meta,
            "runtime": {"sdk": "9.9.9", "model_requested": MODEL},
        })
    return out


GRADE = {"sessions": [{"case_id": f"c{i}", "repeat": 0, "passed": i != 4} for i in range(10)]}
CASE_IDS = [f"c{i}" for i in range(10)]


def test_the_row_on_the_fixture_matches_the_numbers_worked_by_hand():
    row = canary.build_row(_fixture_records(), GRADE, "2026-11-18", CASE_IDS)
    assert row == {
        "date": "2026-11-18",
        "pass_count": 9, "cases_graded": 10, "turns": 20,
        "cost_per_turn_usd_p50": 0.5,    # rank 10 of 0.05 .. 1.00
        "cost_per_turn_usd_p95": 0.95,   # rank 19
        "latency_p95_ms": 1900.0,        # rank 19 of 100 .. 2000
        "repair_loop_rate": 0.1,         # turns 3 and 7 of 20
        "termination_rate": 0.05,        # turn 9 of 20 (a repair cap is not a termination in the trace)
        "termination_causes": {"max_iterations": 1},
        "repair_cap_rate": 0.05,         # turn 7 of 20
        "fabricated_flag_rate": 0.2,     # first drafts flagged: turns 3 and 7, of the 10 audited turns
        "model": MODEL,
        "sdk": "9.9.9",
        "case_ids": CASE_IDS,
        "alerts": [],
    }


def test_the_row_has_no_latency_when_the_traces_carry_none():
    recs = _fixture_records()
    for r in recs:
        del r["meta"]["latency_ms"]
    assert canary.build_row(recs, GRADE, "2026-11-18", CASE_IDS)["latency_p95_ms"] is None


def test_a_turn_without_a_price_makes_the_cost_null_never_a_partial_figure():
    recs = _fixture_records()
    recs[4]["meta"]["cost"] = None  # a model without a price row is written as cost: null
    row = canary.build_row(recs, GRADE, "2026-11-18", CASE_IDS)
    assert row["cost_per_turn_usd_p50"] is None and row["cost_per_turn_usd_p95"] is None


def test_the_model_is_the_id_the_api_returned_and_a_mix_is_written_out():
    recs = _fixture_records()
    recs[0]["model"] = "claude-sonnet-5-5-20261201"
    row = canary.build_row(recs, GRADE, "2026-11-18", CASE_IDS)
    assert row["model"] == "claude-sonnet-5-5,claude-sonnet-5-5-20261201"


def test_pass_count_counts_only_the_named_cases_and_repeat():
    grade = {"sessions": [
        {"case_id": "a", "repeat": 0, "passed": True}, {"case_id": "a", "repeat": 1, "passed": False},
        {"case_id": "b", "repeat": 0, "passed": False}, {"case_id": "z", "repeat": 0, "passed": True},
    ]}
    assert canary.pass_count(grade, ["a", "b"]) == (1, 2)


def test_a_case_with_no_graded_session_is_a_refusal_not_a_zero():
    with pytest.raises(ValueError, match="no graded session"):
        canary.pass_count({"sessions": []}, ["a"])


def test_append_row_adds_exactly_one_line_and_keeps_the_earlier_ones(tmp_path):
    p = tmp_path / "canary" / "metrics.jsonl"
    canary.append_row(p, {"date": "2026-11-18", "alerts": []})
    first = p.read_text()
    canary.append_row(p, {"date": "2026-11-19", "alerts": []})
    assert p.read_text().startswith(first)
    lines = p.read_text().splitlines()
    assert [json.loads(x)["date"] for x in lines] == ["2026-11-18", "2026-11-19"]


# --- the row, on the committed 13 Nov traces ------------------------------------------------------------------------------


def _committed(case_ids):
    return canary.load_records(TRACES / MODEL, case_ids, repeat=0)


def test_the_row_from_the_committed_traces_has_every_field_but_latency():
    recs = _committed(FIRST_FIVE)
    assert {r["case_id"] for r in recs} == set(FIRST_FIVE)
    grade = json.loads((ROOT / "data" / "agent_eval" / "grades" / f"{MODEL}.json").read_text())
    row = canary.build_row(recs, grade, "2026-11-13", FIRST_FIVE)
    assert row["latency_p95_ms"] is None  # the 13 Nov traces predate meta.latency_ms: no number is invented
    assert row["model"] == MODEL and row["sdk"] == "2.8.0" and row["case_ids"] == FIRST_FIVE and row["alerts"] == []
    assert row["turns"] == len(recs) == 7
    assert row["cases_graded"] == 5 and 0 <= row["pass_count"] <= 5
    assert 0 < row["cost_per_turn_usd_p50"] <= row["cost_per_turn_usd_p95"]
    for k in ("repair_loop_rate", "termination_rate", "repair_cap_rate", "fabricated_flag_rate"):
        assert 0.0 <= row[k] <= 1.0


def test_the_estimate_from_the_committed_traces_counts_what_the_files_hold():
    paths = [TRACES / MODEL / f"{c}__r0.trace.jsonl" for c in FIRST_FIVE]
    raw = [json.loads(x) for p in paths for x in p.read_text().splitlines()]
    est = canary.estimate_from_traces(_committed(FIRST_FIVE))
    assert est["sessions"] == 5 and est["turns"] == len(raw)
    assert est["agent_calls"] == sum(len(r["meta"]["usage"]) for r in raw)
    assert est["auditor_calls"] == sum(len(r["meta"]["auditor_usage"]) for r in raw)
    assert est["calls"] == est["agent_calls"] + est["auditor_calls"]
    billed = sum(int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0)
                 for r in raw for u in r["meta"]["usage"] + r["meta"]["auditor_usage"])
    assert est["billed_input_tokens"] == billed
    assert est["cost_usd"] > 0


def test_the_full_estimate_is_all_21_cases_at_k1_on_sonnet():
    est = canary.estimate_from_traces(canary.load_records(TRACES / MODEL, None, repeat=0))
    assert est["sessions"] == 21 and est["turns"] == 27


# --- the budget and the run ----------------------------------------------------------------------------------------------


def test_the_cap_is_the_estimate_formula_times_one_and_a_half():
    cases = load_cases()
    b = canary.budget(cases, FIRST_FIVE)
    assert b["sessions"] == 5 and b["turns"] == 7
    assert b["cap_calls"] == int(b["calls"] * 1.5) and b["cap_input_tokens"] == int(b["est_input_tokens"] * 1.5)


def test_a_run_writes_traces_grades_them_and_appends_one_row(tmp_path):
    client = FakeClient([message([text("It is in Berlin.")])], repeat_last=True)
    seen = {}

    def grader(traces_text):
        seen["text"] = traces_text
        return {"sessions": [{"case_id": c, "repeat": 0, "passed": True} for c in FIRST_FIVE]}

    metrics = tmp_path / "metrics.jsonl"
    row = canary.run_canary(client, load_cases(), FIRST_FIVE, tmp_path / "runs", "2026-11-18", grader, metrics, workers=1)
    rows = [json.loads(x) for x in metrics.read_text().splitlines()]
    assert rows == [row] and len(rows) == 1
    assert row["date"] == "2026-11-18" and row["case_ids"] == FIRST_FIVE and row["alerts"] == []
    assert row["pass_count"] == 5 and row["turns"] == 7
    assert row["model"] == "claude-test"           # the id the (fake) API returned, not the one requested
    assert row["latency_p95_ms"] is not None       # a live run records wall clock
    assert row["cost_per_turn_usd_p50"] is None    # claude-test has no price row: null, not a guess
    assert len(client.requests) == 7               # no auditing without tool calls: one call per turn
    assert len(seen["text"].splitlines()) == 7
    assert len(list((tmp_path / "runs").rglob("*.trace.jsonl"))) == 5


def test_a_run_that_hits_the_cap_appends_no_row(tmp_path):
    from pierre.agent.budget import BudgetExceeded

    class Over:
        messages = None

        def __init__(self):
            self.messages = self

        def create(self, **kw):
            raise BudgetExceeded("cap")

    metrics = tmp_path / "metrics.jsonl"
    with pytest.raises(BudgetExceeded):
        canary.run_canary(Over(), load_cases(), FIRST_FIVE, tmp_path / "runs", "2026-11-18", lambda t: {}, metrics, workers=1)
    assert not metrics.exists()


# --- the command line -----------------------------------------------------------------------------------------------------


def _cli(*args, env=None):
    import os

    e = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    e.update(env or {})
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "analysis" / "canary.py"), *args], capture_output=True, text=True, cwd=ROOT, env=e)


def test_preflight_prints_the_call_list_and_makes_no_call():
    p = _cli("run", "--preflight-only")
    assert p.returncode == 0, p.stderr
    assert "5 sessions" in p.stdout and "model calls" in p.stdout and "estimated input tokens" in p.stdout
    full = _cli("run", "--preflight-only", "--full")
    assert "21 sessions" in full.stdout


def test_a_live_run_without_a_key_refuses_before_any_work():
    m = ROOT / "canary" / "metrics.jsonl"
    before = m.read_bytes() if m.exists() else None
    p = _cli("run")
    assert p.returncode != 0 and "ANTHROPIC_API_KEY" in (p.stdout + p.stderr)
    assert (m.read_bytes() if m.exists() else None) == before  # byte for byte as it was; absent stays absent


def test_make_canary_has_a_full_switch():
    p = subprocess.run(["make", "-n", "canary"], capture_output=True, text=True, cwd=ROOT)
    q = subprocess.run(["make", "-n", "canary", "FULL=1"], capture_output=True, text=True, cwd=ROOT)
    assert p.returncode == 0 and q.returncode == 0
    assert "canary.py run" in p.stdout and "--full" not in p.stdout
    assert "--full" in q.stdout


def test_a_second_run_on_the_same_date_is_refused_and_spends_nothing(tmp_path):
    metrics = tmp_path / "metrics.jsonl"
    canary.append_row(metrics, {"date": "2026-11-18", "alerts": []})
    client = FakeClient([message([text("x")])], repeat_last=True)
    with pytest.raises(ValueError, match="already has a row"):
        canary.run_canary(client, load_cases(), FIRST_FIVE, tmp_path / "runs", "2026-11-18", lambda t: {}, metrics, workers=1)
    assert client.requests == []


def test_the_command_refuses_a_date_that_has_a_row_before_it_touches_that_days_ledger(tmp_path, monkeypatch):
    # The documented remedy after exit 3 is `make canary FULL=1`; on the day of the row that raised it, the command
    # must refuse without deleting that day's committed ledger.
    import argparse
    from scripts.analysis import canary as cli

    metrics, runs = tmp_path / "metrics.jsonl", tmp_path / "runs"
    canary.append_row(metrics, {"date": "2026-11-19", "alerts": []})
    ledger = runs / "2026-11-19" / "ledger.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text('{"calls": 1}\n')
    monkeypatch.setattr(cli, "METRICS", metrics)
    monkeypatch.setattr(cli, "RUNS", runs)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-key")
    monkeypatch.setattr(cli, "wilco_grader", lambda date: (lambda text: {}))
    args = argparse.Namespace(full=True, preflight_only=False, workers=1, date="2026-11-19")
    try:
        rc = cli.cmd_run(args)
    except ValueError as exc:  # the library's own refusal, reached only after the command has run on
        rc = f"raised: {exc}"
    assert ledger.exists() and ledger.read_text() == '{"calls": 1}\n', "the day's committed ledger was deleted"
    assert rc == 1
