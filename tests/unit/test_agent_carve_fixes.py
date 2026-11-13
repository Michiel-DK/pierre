from pierre.agent import tools
from pierre.agent.backend import InMemoryFirmStore


def _companies(result):
    return sorted(c["company_name"] for c in result["companies"])


def test_filter_by_year_with_a_firm_name_keeps_only_that_firm():
    store = InMemoryFirmStore()
    result = tools.filter_portfolio_by_year(store, {}, 2024, ["Merrowfield Ventures"])
    assert _companies(result) == ["Bexmirra Materials", "Umbrevia Bio"]


def test_filter_by_year_with_a_slug_keeps_only_that_firm():
    store = InMemoryFirmStore()
    result = tools.filter_portfolio_by_year(store, {}, 2024, ["merrowfield-ventures"])
    assert _companies(result) == ["Bexmirra Materials", "Umbrevia Bio"]


def test_filter_by_year_without_firms_covers_every_firm():
    store = InMemoryFirmStore()
    result = tools.filter_portfolio_by_year(store, {}, 2024)
    assert {c["firm_slug"] for c in result["companies"]} > {"merrowfield-ventures", "quillmoor-capital"}


def test_filter_by_year_unknown_firm_returns_no_rows():
    store = InMemoryFirmStore()
    assert tools.filter_portfolio_by_year(store, {}, 2024, ["No Such Firm"])["count"] == 0


def test_saved_list_canonicalises_names_to_slugs():
    store = InMemoryFirmStore()
    resolver = lambda s: tools.resolve_firm_for_shortlist(store, s, {})
    result = tools.create_saved_list(
        store, 1, "climate-hardware", ["Merrowfield Ventures", "brask-ventures"], resolve_firm=resolver
    )
    assert result["success"] is True
    assert store.final_state() == {"saved_lists": {"climate-hardware": ["brask-ventures", "merrowfield-ventures"]}}


def test_saved_list_with_unknown_firm_saves_nothing():
    store = InMemoryFirmStore()
    resolver = lambda s: tools.resolve_firm_for_shortlist(store, s, {})
    result = tools.create_saved_list(store, 1, "x", ["brask-ventures", "No Such Firm"], resolve_firm=resolver)
    assert "error" in result
    assert store.final_state() == {}
