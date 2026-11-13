import json
import threading
import time

import pytest

from pierre.agent import _formatters as F
from pierre.agent import tools
from pierre.agent._dispatch_dedup import CallDeduper, canonicalise_args
from pierre.agent.backend import InMemoryFirmStore
from pierre.agent.registry import ToolContext, bind_tools


@pytest.fixture
def store():
    return InMemoryFirmStore()


@pytest.fixture
def run(store):
    dispatcher = tools.ToolDispatcher(bind_tools(ToolContext(store)), CallDeduper())

    def call(tool, /, **args):
        return dispatcher.execute(tool, args)

    call.dispatcher = dispatcher
    return call


def test_parse_check_size_forms():
    assert F.parse_check_size(500000) == 500000.0
    assert F.parse_check_size("€5M") == 5_000_000.0
    assert F.parse_check_size("500K") == 500_000.0
    assert F.parse_check_size("1,000,000") == 1_000_000.0
    assert F.parse_check_size("n/a") is None
    assert F.parse_check_size(None) is None


def test_lookup_firm_reports_other_matches_and_portfolio_count(run):
    result = run("lookup_firm", firm_name="Tessaly")
    assert result["firm_name"] == "Tessaly Capital"
    assert result["other_matches"] == ["Tessaly Growth"]
    assert run("lookup_firm", firm_name="Ferrin Lane Capital")["portfolio_count"] == 2
    assert run("lookup_firm", firm_name="Nobody Capital")["found"] is False


def test_compare_firms_takes_slugs_and_skips_names(run):
    ok = run("compare_firms", firm_ids=["brask-ventures", "gravick-partners"])
    assert [f["firm_name"] for f in ok["firms"]] == ["Brask Ventures", "Gravick Partners"]
    assert run("compare_firms", firm_ids=["Brask Ventures"])["firms"] == []


def test_check_raise_fit_flags_a_raise_above_the_maximum(run):
    result = run("check_raise_fit", stage="Series A", sector="fintech", geography="UK", amount_min=3_500_000, amount_max=3_500_000)
    assert [m["firm_id"] for m in result["matches"]] == ["quillmoor-capital"]
    assert result["matches"][0]["fit_note"] == "check size too small (raise above maximum)"
    unchecked = run("check_raise_fit", stage="Seed", sector="fintech", geography="UK")
    assert unchecked["matches"][0]["fit_note"] == "check size not evaluated"


def test_search_portfolio_is_lexical_and_validates_the_category(run):
    result = run("search_portfolio", query="payroll contractor")
    assert result["companies"][0]["company_name"] == "Vantrelle Payroll"
    assert 0 < result["companies"][0]["similarity"] <= 1
    assert run("search_portfolio", query="payroll", firm_category="Hedge Fund")["error"].startswith("Tool")
    with pytest.raises(ValueError):
        tools.require_known_category_or_none("firm_category", "Hedge Fund")
    assert tools.require_known_category_or_none("firm_category", None) is None


def test_date_tools_carry_the_demo_clock_and_no_other_tool_does(run):
    assert run("filter_portfolio_by_year", investment_year=2026)["as_of"] == "2026-06-30"
    assert run("find_recent_investments")["as_of"] == "2026-06-30"
    others = [
        run("lookup_firm", firm_name="Orrindale Fund"),
        run("get_portfolio_stats"),
        run("search_portfolio", query="payroll"),
        run("check_raise_fit", stage="Seed", sector="fintech", geography="UK"),
        run("list_shortlist"),
    ]
    assert all("as_of" not in other for other in others)


def test_find_recent_investments_window_and_empty_result(run):
    climate = run("find_recent_investments", sector="climate")
    assert [a["company_mentioned"] for a in climate["articles"]] == ["Pellvarin Carbon"]
    assert run("find_recent_investments", sector="agritech")["count"] == 0
    assert run("find_recent_investments", months=12, sector="climate")["count"] == 2


def test_portfolio_stats_and_the_planted_timeout(run, store):
    stats = run("get_portfolio_stats", firm_ids=["merrowfield-ventures"])
    assert stats["total_companies"] == 5 and stats["scope"] == "selected firms"
    assert run("get_portfolio_stats")["scope"] == "full database"
    failed = run("get_portfolio_stats", firm_ids=["Ferrin Lane Capital"])
    assert set(failed) == {"error"} and "internal error" in failed["error"]
    assert "Traceback" not in json.dumps(failed)
    with pytest.raises(TimeoutError):
        store.get_portfolio_stats(firm_slugs=["ferrin-lane-capital", "brask-ventures"])


def test_unknown_tool_and_bad_arguments_return_error_dicts(run):
    assert run("no_such_tool") == {"error": "Unknown tool: no_such_tool"}
    assert "error" in run("lookup_firm", wrong_argument=1)


def test_shortlist_add_is_idempotent_canonicalises_and_lists_newest_first(run, store):
    first = run("add_to_shortlist", firm_slug="Quillmoor Capital")
    assert first["message"] == "Added quillmoor-capital to shortlist (canonicalised from 'Quillmoor Capital')."
    run("add_to_shortlist", firm_slug="orrindale-fund", notes="  seed  ")
    run("add_to_shortlist", firm_slug="orrindale-fund", notes="updated")
    listed = run("list_shortlist")
    assert [r["firm_slug"] for r in listed["shortlist"]] == ["orrindale-fund", "quillmoor-capital"]
    assert listed["shortlist"][0]["notes"] == "updated" and listed["total"] == 2
    assert store.final_state() == {"shortlist": ["quillmoor-capital", "orrindale-fund"]}
    assert "No firm matches" in run("add_to_shortlist", firm_slug="No Such Firm")["error"]
    assert "lookup_firm" in run("add_to_shortlist", firm_slug="No Such Firm")["error"]
    assert run("add_to_shortlist", firm_slug="  ")["error"] == "firm_slug must be a non-empty string."


def test_shortlist_remove_is_exact_slug_only(run, store):
    run("add_to_shortlist", firm_slug="vellisk-partners")
    miss = run("remove_from_shortlist", firm_slug="Vellisk Partners")
    assert miss["success"] is False and store.final_state() == {"shortlist": ["vellisk-partners"]}
    assert run("remove_from_shortlist", firm_slug="vellisk-partners")["success"] is True
    assert store.final_state() == {}


def test_saved_list_validation_and_final_state_shape(run, store):
    assert run("create_saved_list", name=" ", firm_slugs=["brask-ventures"])["error"] == "List name cannot be empty"
    assert run("create_saved_list", name="x", firm_slugs=[])["error"] == "No firm slugs provided"
    saved = run("create_saved_list", name="  hardware ", firm_slugs=["brask-ventures", "Brask Ventures", "gravick-partners"])
    assert saved["success"] and saved["name"] == "hardware" and saved["firm_slugs"] == ["brask-ventures", "gravick-partners"]
    assert store.final_state() == {"saved_lists": {"hardware": ["brask-ventures", "gravick-partners"]}}


def test_store_serves_one_demo_user(store):
    with pytest.raises(ValueError):
        store.shortlist_add(2, "brask-ventures", None)


def test_canonicalise_args_ignores_key_order():
    assert canonicalise_args({"b": 1, "a": 2}) == canonicalise_args({"a": 2, "b": 1})
    assert canonicalise_args(None) == canonicalise_args({}) == "{}"


def test_deduper_serves_a_repeat_and_forgets_on_reset():
    deduper = CallDeduper()
    assert deduper.get_or_run("t", {"a": 1}, lambda: "first") == (False, "first")
    assert deduper.get_or_run("t", {"a": 1}, lambda: "second") == (True, "first")
    assert deduper.get_or_run("t", {"a": 2}, lambda: "other") == (False, "other")
    assert deduper.size() == 2
    deduper.reset()
    assert deduper.observe("t", {"a": 1}) == (False, None)


def test_dispatcher_serves_a_repeated_write_from_cache_and_counts_the_hit(run, store):
    run("add_to_shortlist", firm_slug="quillmoor-capital")
    run("remove_from_shortlist", firm_slug="quillmoor-capital")
    again = run("remove_from_shortlist", firm_slug="quillmoor-capital")
    assert again["success"] is True
    assert run.dispatcher.dedup_hits == 1
    run.dispatcher.reset_turn()
    assert run("remove_from_shortlist", firm_slug="quillmoor-capital")["success"] is False


def test_dispatcher_caches_error_results_too():
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("secret connection string")

    dispatcher = tools.ToolDispatcher({"boom": boom}, CallDeduper())
    first = dispatcher.execute("boom", {})
    assert "secret" not in json.dumps(first)
    assert dispatcher.execute("boom", {}) == first
    assert len(calls) == 1


def test_parallel_dispatch_keeps_call_order_and_caps_workers():
    active, peak, lock = 0, 0, threading.Lock()

    def slow(i):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return {"i": i}

    dispatcher = tools.ToolDispatcher({"slow": slow}, CallDeduper())
    results = dispatcher.execute_parallel([("slow", {"i": i}) for i in range(10)])
    assert [r["i"] for r in results] == list(range(10))
    assert peak <= 6
    assert dispatcher.execute_parallel([]) == []


def test_dispatcher_without_a_deduper_never_caches(store):
    dispatcher = tools.ToolDispatcher(bind_tools(ToolContext(store)))
    dispatcher.execute("add_to_shortlist", {"firm_slug": "brask-ventures"})
    assert dispatcher.execute("remove_from_shortlist", {"firm_slug": "brask-ventures"})["success"] is True
    assert dispatcher.execute("remove_from_shortlist", {"firm_slug": "brask-ventures"})["success"] is False
