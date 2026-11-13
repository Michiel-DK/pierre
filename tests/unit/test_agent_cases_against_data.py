"""For each case, call the expected tool on the fictional data and check the planted answer is reachable."""
import json
from pathlib import Path

import pytest

from pierre.agent import tools
from pierre.agent.backend import InMemoryFirmStore
from pierre.agent.registry import ToolContext, bind_tools
from pierre.agent._dispatch_dedup import CallDeduper

CASES = {c["id"]: c for c in json.loads((Path(__file__).resolve().parents[2] / "evals" / "pierre-firm-agent" / "cases.json").read_text(encoding="utf-8"))["cases"]}


@pytest.fixture
def store():
    return InMemoryFirmStore()


@pytest.fixture
def call(store):
    dispatcher = tools.ToolDispatcher(bind_tools(ToolContext(store)), CallDeduper())
    return lambda tool, /, **args: dispatcher.execute(tool, args)


def _blob(result):
    return json.dumps(result, ensure_ascii=False)


def _expected(case_id, turn=None):
    case = CASES[case_id]
    if turn is None:
        return case["expected"]
    return case["turns_expected"][turn]["expected"]


def _assert_needles(case_id, result, turn=None):
    expected = _expected(case_id, turn)
    blob = _blob(result)
    for needle in expected.get("answer_contains", []):
        assert needle in blob, (case_id, needle)
    for needle in expected.get("answer_not_contains", []):
        assert needle not in blob, (case_id, needle)


def test_hq_and_ticket(call):
    for case_id in ("hq-and-ticket-quillmoor", "hq-and-ticket-quillmoor-paraphrase"):
        result = call("lookup_firm", firm_name="Quillmoor")
        _assert_needles(case_id, result)
        assert (result["check_size_minimum"], result["check_size_maximum"]) == (500000, 3000000)


def test_portfolio_year_cases(call):
    for case_id in ("portfolio-year-merrowfield", "portfolio-year-merrowfield-paraphrase"):
        result = call("filter_portfolio_by_year", investment_year=2024, firm_ids=["Merrowfield Ventures"])
        _assert_needles(case_id, result)
        assert result["count"] == 2
    brask = call("filter_portfolio_by_year", investment_year=2023, firm_ids=["brask-ventures"])
    _assert_needles("portfolio-year-brask-int-arg", brask)
    assert brask["count"] == 1


def test_unfiltered_year_call_would_name_other_firms(call):
    blob = _blob(call("filter_portfolio_by_year", investment_year=2024))
    assert any(n in blob for n in _expected("portfolio-year-merrowfield")["answer_not_contains"])


def test_recent_deals_cases(call):
    _assert_needles("recent-climate-deals", call("find_recent_investments", sector="climate"))
    empty = call("find_recent_investments", sector="agritech")
    assert empty["count"] == 0
    for name in _expected("recent-deals-empty-result")["answer_not_contains"]:
        assert name not in _blob(empty)


def test_raise_fit_and_saved_list_source(call):
    result = call("check_raise_fit", stage="Series A", sector="fintech", geography="UK", amount_min=3500000, amount_max=3500000)
    assert [m["firm_name"] for m in result["matches"]] == ["Quillmoor Capital"]
    assert "above maximum" in result["matches"][0]["fit_note"]
    seed = call("check_raise_fit", stage="Seed", sector="fintech", geography="UK")
    assert [m["firm_name"] for m in seed["matches"]] == ["Quillmoor Capital"]


def test_compare_two_firms(call):
    result = call("compare_firms", firm_ids=["brask-ventures", "gravick-partners"])
    _assert_needles("compare-two-firms", result)
    assert [f["hq"] for f in result["firms"]] == ["Berlin, Germany", "Warsaw, Poland"]


def test_tool_failure_and_the_firm_still_readable(call):
    assert "error" in call("get_portfolio_stats", firm_ids=["ferrin-lane-capital"])
    firm = call("lookup_firm", firm_name="Ferrin Lane Capital")
    assert firm["portfolio_count"] == 2 and firm["focus_areas"] == ["edtech", "saas"]


def test_injection_text_reaches_the_model_and_names_the_case_markers(call):
    result = call("lookup_firm", firm_name="Sollund Ventures")
    case = CASES["injection-then-legit-write"]
    session = case["session"]["no_injected_write"]
    assert session["marker"] in result["description"]
    assert all(term in result["description"] for term in session["payload_terms"])


def test_disambiguation_and_cross_turn_facts(call):
    tessaly = call("lookup_firm", firm_name="Tessaly")
    assert {tessaly["firm_name"], *tessaly["other_matches"]} == {"Tessaly Capital", "Tessaly Growth"}
    lumary = call("lookup_firm", firm_name="Lumary Capital")
    assert lumary["check_size_minimum"] == 5000000 and "Seed" not in lumary["stages"]
    kestrabel = call("lookup_firm", firm_name="Kestrabel Ventures")
    assert kestrabel["portfolio_count"] == 3 and "founded in 2012" in kestrabel["description"]
    counts = {n: call("lookup_firm", firm_name=n)["portfolio_count"] for n in ("Quillmoor Capital", "Vellisk Partners", "Orrindale Fund")}
    assert counts == {"Quillmoor Capital": 6, "Vellisk Partners": 2, "Orrindale Fund": 2}


def test_followup_pronoun_case(call):
    _assert_needles("followup-pronoun-this-year", call("lookup_firm", firm_name="Nimbrel Capital"), turn=0)
    year = call("filter_portfolio_by_year", investment_year=2026, firm_ids=["nimbrel-capital"])
    _assert_needles("followup-pronoun-this-year", year, turn=1)
    assert year["count"] == 1


def test_write_cases_reach_their_final_state(call, store):
    call("add_to_shortlist", firm_slug="Quillmoor Capital")
    call("add_to_shortlist", firm_slug="Vellisk Partners")
    assert call("remove_from_shortlist", firm_slug="Vellisk Partners")["success"] is False
    call("remove_from_shortlist", firm_slug="vellisk-partners")
    call("add_to_shortlist", firm_slug="Orrindale Fund")
    assert store.final_state() == {"shortlist": ["quillmoor-capital", "orrindale-fund"]}
    assert set(CASES["shortlist-swap-three-turns"]["session"]["state_match"]["shortlist"]["set"]) == set(store.final_state()["shortlist"])

    call("create_saved_list", name="climate-hardware", firm_slugs=["Merrowfield Ventures", "Brask Ventures"])
    want = CASES["injection-then-legit-write"]["session"]["state_match"]["saved_lists"]["equals"]
    assert store.final_state()["saved_lists"] == want
