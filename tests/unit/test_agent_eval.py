"""The live-eval plumbing, offline: the plan and its estimate, crash and resume handling, the budget abort, the report's counting
and that the committed report, grades and traces agree. No model, no key, no wilco."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from pierre.agent import eval_report
from pierre.agent.budget import BudgetExceeded
from pierre.agent.eval_run import MODELS, Session, estimate, is_complete, plan, run_all, run_session
from pierre.agent.factory import load_cases
from pierre.agent.repair import MAX_REPAIR_ROUNDS, TurnResult
from pierre.agent.trace import TraceWriter
from tests.unit._fake_claude import FakeClient, message, text

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data" / "agent_eval"


def test_plan_is_sonnet_k3_and_opus_k1_repeat_major():
    cases = load_cases()
    sessions = plan(cases)
    assert len(sessions) == 21 * 3 + 21 == 84
    assert {(s.model, s.repeat) for s in sessions} == {("claude-sonnet-5-5", 0), ("claude-sonnet-5-5", 1), ("claude-sonnet-5-5", 2), ("claude-opus-5-5", 0)}
    assert [s.repeat for s in sessions] == sorted(s.repeat for s in sessions)
    assert MODELS == (("claude-sonnet-5-5", 3), ("claude-opus-5-5", 1))


def test_estimate_is_the_pre_run_per_turn_figures_with_a_cap_of_one_and_a_half():
    est = estimate(load_cases(), plan(load_cases()))
    assert (est["sessions"], est["turns"], est["calls"], est["est_input_tokens"]) == (84, 108, 648, 4_464_000)
    assert (est["cap_calls"], est["cap_input_tokens"]) == (972, 6_696_000)


def test_repair_cap_constant_matches_the_loop():
    assert eval_report.MAX_REPAIR_ROUNDS == MAX_REPAIR_ROUNDS


class _Boom:
    def __init__(self, exc):
        self.exc, self.messages = exc, self

    def create(self, **kw):
        raise self.exc


def test_a_crashed_session_still_writes_every_turn_as_a_crash_termination(tmp_path):
    cases = load_cases()
    cid = "shortlist-swap-three-turns"
    s = Session(cid, "claude-sonnet-5-5", 1)
    run_session(_Boom(RuntimeError("x")), cases[cid], s, tmp_path)
    recs = [json.loads(x) for x in s.path(tmp_path).read_text().splitlines()]
    assert [r["turn"] for r in recs] == [0, 1, 2]
    assert {r["termination"]["cause"] for r in recs} == {"crash:RuntimeError"}
    assert recs[0]["repeat"] == 1 and "final_state" in recs[-1] and "final_state" not in recs[0]
    assert is_complete(s.path(tmp_path), 3)


def test_the_budget_abort_propagates_and_leaves_no_trace_file(tmp_path):
    cases = load_cases()
    cid = "hq-and-ticket-quillmoor"
    s = Session(cid, "claude-sonnet-5-5", 0)
    with pytest.raises(BudgetExceeded):
        run_session(_Boom(BudgetExceeded("cap")), cases[cid], s, tmp_path)
    assert not s.path(tmp_path).exists()


def test_run_all_skips_sessions_whose_trace_is_complete(tmp_path):
    cases = {k: v for k, v in load_cases().items() if k == "hq-and-ticket-quillmoor"}
    sessions = plan(cases, (("claude-sonnet-5-5", 2),))
    client = FakeClient([message([text("London.")])], repeat_last=True)
    assert run_all(client, cases, sessions, tmp_path, workers=1) == {"ran": 2, "skipped_complete": 0}
    n = len(client.requests)
    assert run_all(client, cases, sessions, tmp_path, workers=1) == {"ran": 0, "skipped_complete": 2}
    assert len(client.requests) == n


def test_repair_cap_is_meta_not_a_termination(tmp_path):
    w = TraceWriter(tmp_path / "t.jsonl", "s", "c", split="train")
    capped = w.write_turn(0, "q", TurnResult(answer="a", steps=1, termination="repair_cap"))
    cut = w.write_turn(1, "q2", TurnResult(answer="", steps=1, termination="iteration_cap"))
    assert capped["termination"] is None and capped["meta"]["repair_cap"] is True
    assert cut["termination"] == {"cause": "iteration_cap"} and "repair_cap" not in cut["meta"]


def _audit(ver, fab, unv):
    claims = [{"claim_text": "c", "claim_type": "other", "verdict": "verified", "evidence": "e"}] * ver
    claims += [{"claim_text": "f", "claim_type": "other", "verdict": "fabricated", "evidence": ""}] * fab
    claims += [{"claim_text": "u", "claim_type": "other", "verdict": "unverifiable", "evidence": ""}] * unv
    return {"verified": ver, "fabricated": fab, "unverifiable": unv, "claims": claims}


def _rec(rounds, termination=None, repair_cap=False):
    meta = {"rounds": rounds, "usage": [{"input_tokens": 10, "output_tokens": 2}], "auditor_usage": [{"input_tokens": 5, "output_tokens": 1}]}
    if repair_cap:
        meta["repair_cap"] = True
    return {"turn": 0, "meta": meta, "termination": termination, "output_parsed": {"answer": rounds[-1]["answer"]}, "steps": 3}


def test_turn_stats_counts_before_and_after_repair():
    repaired = _rec([{"round": 0, "answer": "d", "audit": _audit(2, 1, 1), "steps": 1}, {"round": 1, "answer": "f", "audit": _audit(1, 0, 1), "steps": 2}])
    clean = _rec([{"round": 0, "answer": "x", "audit": _audit(3, 0, 0), "steps": 1}])
    errored = _rec([{"round": 0, "answer": "x", "audit": {"error": "boom", "claims": []}, "steps": 1}])
    s = eval_report.turn_stats([repaired, clean, errored])
    assert (s["turns"], s["audited"], s["audit_error_turns"], s["turns_repaired"]) == (3, 2, 1, 1)
    assert (s["d_fab"], s["f_fab"], s["d_unv"], s["f_unv"]) == (1, 0, 1, 1)
    assert (s["fab_turns_d"], s["fab_cleared"], s["verified_lost"]) == (1, 1, 1)


def test_turn_stats_counts_a_flag_on_an_other_matches_firm_as_the_known_false_positive():
    def turn(claim_text, others):
        draft = _audit(1, 1, 0)
        draft["claims"] = draft["claims"][:1] + [{"claim_text": claim_text, "claim_type": "other", "verdict": "fabricated", "evidence": None}]
        rec = _rec([{"round": 0, "answer": "d", "audit": draft, "steps": 1}, {"round": 1, "answer": "f", "audit": _audit(1, 0, 0), "steps": 1}])
        rec["tool_calls"] = [{"tool": "lookup_firm", "args": {"firm_name": "Tessaly"}}]
        rec["tool_results"] = [{"found": True, "firm_name": "Tessaly Capital", "other_matches": others}]
        return rec

    s = eval_report.turn_stats([turn("The name also matches Tessaly Growth.", ["Tessaly Growth"]), turn("It backed Acme in 2019.", ["Tessaly Growth"]), turn("Also Tessaly Growth.", [])])
    assert (s["d_fab"], s["d_turns_fab"], s["turns_repaired"]) == (3, 3, 3)
    assert (s["d_fab_other_matches"], s["d_turns_fab_other_matches"], s["turns_repaired_other_matches"]) == (1, 1, 1)


def test_draft_trace_swaps_in_the_first_answer_only_for_repaired_turns(tmp_path):
    d = tmp_path / "m"
    d.mkdir()
    repaired = _rec([{"round": 0, "answer": "draft", "audit": None, "steps": 2}, {"round": 1, "answer": "final", "audit": None, "steps": 1}], repair_cap=True)
    plain = _rec([{"round": 0, "answer": "same", "audit": None, "steps": 1}])
    (d / "a.trace.jsonl").write_text(json.dumps(repaired) + "\n" + json.dumps(plain) + "\n")
    out = [json.loads(x) for x in eval_report.draft_trace(tmp_path, "m").splitlines()]
    assert out[0]["output_parsed"] == {"answer": "draft"} and out[0]["steps"] == 2 and "repair_cap" not in out[0]["meta"]
    assert out[1] == plain


def test_wilson_matches_known_values():
    lo, hi = eval_report.wilson(7, 8)
    assert (round(lo, 3), round(hi, 3)) == (0.529, 0.978)  # the interval wilco printed for 7 of 8
    assert eval_report.wilson(0, 0) == (0.0, 1.0)


def test_stage_mapping_is_the_one_fixed_in_the_predictions_file():
    doc = (ROOT / "docs" / "calibration" / "agent_eval_predictions.md").read_text()
    table = dict(re.findall(r"^\| (selection|arguments|execution|output handling) \| (.+) \|$", doc, re.M))
    for stage, checks in table.items():
        for name in re.findall(r"`([a-z_]+)`", checks):
            assert eval_report.STAGES[name] == stage, name


def test_every_prediction_in_the_file_is_evaluated_by_the_report():
    doc = (ROOT / "docs" / "calibration" / "agent_eval_predictions.md").read_text()
    ids = re.findall(r"^\| (P\d+) \|", doc, re.M)
    assert ids == [f"P{i}" for i in range(1, 20)]
    src = (ROOT / "pierre" / "agent" / "eval_report.py").read_text()
    for pid in ids:
        assert f'"{pid}"' in src, pid


def test_the_wilco_pin_is_a_full_commit_in_the_eval_extra():
    assert re.search(r"eval = \[\"wilco-exams @ git\+https://github\.com/Michiel-DK/wilco@[0-9a-f]{40}\"\]", (ROOT / "pyproject.toml").read_text())


# ---- the committed run (skipped until it exists) ----

committed = pytest.mark.skipif(not (DATA / "grades").exists(), reason="the live run is not committed yet")


@committed
def test_the_committed_report_is_what_the_renderer_produces():
    from scripts.analysis import agent_eval

    assert agent_eval.render_text() == (ROOT / "docs" / "calibration" / "agent_eval_report.md").read_text(encoding="utf-8")


@committed
def test_grades_were_made_from_these_traces_and_this_case_file():
    cases_sha = hashlib.sha256((ROOT / "evals" / "pierre-firm-agent" / "cases.json").read_bytes()).hexdigest()
    for model_dir in sorted((DATA / "traces").iterdir()):
        for label, text_ in ((model_dir.name, eval_report.concat_traces(DATA / "traces", model_dir.name)), (model_dir.name + ".draft", eval_report.draft_trace(DATA / "traces", model_dir.name))):
            g = json.loads((DATA / "grades" / f"{label}.json").read_text())
            assert g["provenance"]["traces_sha256"] == hashlib.sha256(text_.encode()).hexdigest(), label
            assert g["provenance"]["cases_sha256"] == cases_sha, label


@committed
def test_committed_eval_data_has_no_machine_paths_ids_or_keys():
    for path in DATA.rglob("*"):
        if path.is_file():
            body = path.read_text(encoding="utf-8")
            for needle in ("/Users/", "/private/", "req_", "request-id", "request_id", "sk-ant", "x-api-key", "api_key", "organization"):
                assert needle not in body, (path.name, needle)


@committed
def test_every_session_has_all_its_turns_and_the_requested_model():
    cases = load_cases()
    expected = {(s.model, s.case_id, s.repeat) for s in plan(cases)}
    seen = set()
    for model, sessions in eval_report.load_traces(DATA / "traces").items():
        for recs in sessions.values():
            cid, rep = recs[0]["case_id"], recs[0]["repeat"]
            seen.add((model, cid, rep))
            turns = cases[cid].get("turns") or [cases[cid]["input"]]
            assert [r["turn"] for r in recs] == list(range(len(turns)))
            assert all(r["model"] == model or (r["termination"] or {}).get("cause", "").startswith("crash") for r in recs)
    assert seen == expected


def test_hand_read_analysis_prints_only_for_the_run_it_was_written_for(tmp_path):
    import shutil

    from scripts.analysis.agent_eval import CASES, pinned_wilco_sha

    data = tmp_path / "agent_eval"
    shutil.copytree(DATA, data)
    marker = "A check false positive: `tool-failure-timeout`"
    assert marker in eval_report.render(data, CASES, pinned_wilco_sha())
    summary = json.loads((data / "run_summary.json").read_text())
    summary["runs"][-1]["at"] = "2027-01-01T00:00:00+00:00"
    (data / "run_summary.json").write_text(json.dumps(summary))
    out = eval_report.render(data, CASES, pinned_wilco_sha())
    assert marker not in out
    assert "Not redone for this run (2027-01-01T00:00:00+00:00)" in out
