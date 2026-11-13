"""The single tool registry: one entry per tool, used for the Claude tool list and the MCP server."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from pierre.agent import _shortlist, tools
from pierre.agent.backend import AS_OF, DEMO_USER_ID, FirmStore


@dataclass
class ToolContext:
    """Per-session state the tools run against."""

    store: FirmStore
    firm_profiles: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    user_id: int = DEMO_USER_ID

    def resolve_firm(self, slug: str) -> Optional[Dict[str, Any]]:
        return tools.resolve_firm_for_shortlist(self.store, slug, self.firm_profiles)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Dict[str, Any]
    run: Callable[..., Any]
    writes: bool = False


def _schema(properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


_STR = {"type": "string"}
_INT = {"type": "integer"}
_SLUGS = {"type": "array", "items": {"type": "string"}}


def _lookup_firm(ctx: ToolContext, firm_name: str) -> Any:
    return tools.lookup_firm(ctx.store, firm_name)


def _compare_firms(ctx: ToolContext, firm_ids: List[str], dimension: Optional[str] = None) -> Any:
    return tools.compare_firms(ctx.store, firm_ids, ctx.firm_profiles, dimension)


def _search_portfolio(ctx: ToolContext, query: str, top_k: int = 10, firm_category: Optional[str] = None) -> Any:
    return tools.search_portfolio(ctx.store, query, top_k, firm_category)


def _get_portfolio_stats(ctx: ToolContext, firm_ids: Optional[List[str]] = None, top_k: int = 20) -> Any:
    return tools.get_portfolio_stats(ctx.store, ctx.firm_profiles, firm_ids, top_k)


def _filter_portfolio_by_year(
    ctx: ToolContext, investment_year: int, firm_ids: Optional[List[str]] = None, top_k: int = 50
) -> Any:
    return tools.filter_portfolio_by_year(ctx.store, ctx.firm_profiles, investment_year, firm_ids, top_k)


def _find_recent_investments(
    ctx: ToolContext, sector: Optional[str] = None, stage: Optional[str] = None, months: int = 6
) -> Any:
    return tools.find_recent_investments(ctx.store, sector, stage, months)


def _check_raise_fit(
    ctx: ToolContext,
    stage: str,
    sector: str,
    geography: str,
    amount_min: Optional[int] = None,
    amount_max: Optional[int] = None,
) -> Any:
    return tools.check_raise_fit(ctx.store, stage, sector, geography, amount_min, amount_max)


def _list_shortlist(ctx: ToolContext) -> Any:
    return _shortlist.list_shortlist(ctx.store, ctx.user_id)


def _add_to_shortlist(ctx: ToolContext, firm_slug: str, notes: Optional[str] = None) -> Any:
    return _shortlist.add_to_shortlist(ctx.store, ctx.user_id, firm_slug, notes, resolve_firm=ctx.resolve_firm)


def _remove_from_shortlist(ctx: ToolContext, firm_slug: str) -> Any:
    return _shortlist.remove_from_shortlist(ctx.store, ctx.user_id, firm_slug)


def _create_saved_list(ctx: ToolContext, name: str, firm_slugs: List[str]) -> Any:
    return tools.create_saved_list(ctx.store, ctx.user_id, name, firm_slugs, resolve_firm=ctx.resolve_firm)


REGISTRY: tuple[ToolSpec, ...] = (
    ToolSpec(
        "lookup_firm",
        "Look up one investment firm in the demo dataset by name or slug. Returns its slug (firm_id), headquarters, "
        "stages, focus areas, a description, the minimum and maximum cheque size in EUR, and the number of portfolio "
        "companies on record. If the name matches several firms, the first is returned in full and the rest are "
        "listed by name in other_matches.",
        _schema({"firm_name": {"type": "string", "description": "A firm name or slug, e.g. 'Orrindale Fund'."}}, ["firm_name"]),
        _lookup_firm,
    ),
    ToolSpec(
        "compare_firms",
        "Compare investment firms side by side: headquarters, stages, focus areas and cheque-size range. "
        "firm_ids must be firm slugs (the firm_id field returned by lookup_firm); a name that is not a slug is "
        "skipped and returns no entry.",
        _schema(
            {
                "firm_ids": {**_SLUGS, "description": "Firm slugs to compare, e.g. ['brask-ventures', 'gravick-partners']."},
                "dimension": {"type": "string", "description": "Optional aspect the comparison is about."},
            },
            ["firm_ids"],
        ),
        _compare_firms,
    ),
    ToolSpec(
        "search_portfolio",
        "Search the portfolio companies of all firms in the demo dataset by words in the company name, "
        "description or sector. The similarity field is a lexical score: the share of query words found in the text, "
        "from 0 to 1. It is not a semantic score.",
        _schema(
            {
                "query": {"type": "string", "description": "Words to look for."},
                "top_k": {**_INT, "description": "Maximum number of companies to return."},
                "firm_category": {
                    "type": "string",
                    "enum": list(tools.KNOWN_FIRM_CATEGORIES),
                    "description": "Only companies of firms in this category.",
                },
            },
            ["query"],
        ),
        _search_portfolio,
    ),
    ToolSpec(
        "get_portfolio_stats",
        "Portfolio statistics: total companies, companies per sector and companies per investment year, for the "
        "firms named or, with no firm_ids, for the whole demo dataset. firm_ids may be slugs or names.",
        _schema(
            {
                "firm_ids": {**_SLUGS, "description": "Optional firm slugs or names to restrict the statistics to."},
                "top_k": {**_INT, "description": "Maximum number of sectors to return."},
            }
        ),
        _get_portfolio_stats,
    ),
    ToolSpec(
        "filter_portfolio_by_year",
        f"List portfolio companies by the year of the investment. Dates are relative to the demo clock, "
        f"{AS_OF}, which is returned as as_of. firm_ids may be slugs or names; with none, every firm is included.",
        _schema(
            {
                "investment_year": {**_INT, "description": "Four-digit year, e.g. 2024."},
                "firm_ids": {**_SLUGS, "description": "Optional firm slugs or names to restrict the list to."},
                "top_k": {**_INT, "description": "Maximum number of companies to return."},
            },
            ["investment_year"],
        ),
        _filter_portfolio_by_year,
    ),
    ToolSpec(
        "find_recent_investments",
        f"Find investment announcements in the demo news feed, newest first, grouped by firm. The window ends at the "
        f"demo clock, {AS_OF} (returned as as_of), and starts `months` months earlier. Optionally filter by sector "
        f"word (e.g. 'climate') and by stage (e.g. 'Seed').",
        _schema(
            {
                "sector": {"type": "string", "description": "Sector word to match, e.g. 'fintech'."},
                "stage": {"type": "string", "description": "Funding stage to match, e.g. 'Series A'."},
                "months": {**_INT, "description": "Length of the window in months (default 6)."},
            }
        ),
        _find_recent_investments,
    ),
    ToolSpec(
        "check_raise_fit",
        "Find firms that invest at a given stage, in a given sector, in a given country, and say whether a raise "
        "amount in EUR falls inside each firm's cheque range (fit_note). Use the country as the dataset spells it, "
        "e.g. 'UK' or 'Germany'. Pass amount_min and amount_max to check the cheque size.",
        _schema(
            {
                "stage": {"type": "string", "description": "e.g. 'Seed' or 'Series A'."},
                "sector": {"type": "string", "description": "e.g. 'fintech'."},
                "geography": {"type": "string", "description": "Country, e.g. 'UK'."},
                "amount_min": {**_INT, "description": "Lower end of the raise in EUR."},
                "amount_max": {**_INT, "description": "Upper end of the raise in EUR."},
            },
            ["stage", "sector", "geography"],
        ),
        _check_raise_fit,
    ),
    ToolSpec(
        "list_shortlist",
        "List the firms on the current shortlist, newest first, with any notes.",
        _schema({}),
        _list_shortlist,
    ),
    ToolSpec(
        "add_to_shortlist",
        "Add a firm to the shortlist. Only call this when the user asks for a firm to be shortlisted. "
        "A name or slug is accepted and stored as the firm's slug. Adding a firm that is already listed updates its notes.",
        _schema(
            {
                "firm_slug": {"type": "string", "description": "Firm slug or name."},
                "notes": {"type": "string", "description": "Optional note to keep with the firm."},
            },
            ["firm_slug"],
        ),
        _add_to_shortlist,
        writes=True,
    ),
    ToolSpec(
        "remove_from_shortlist",
        "Remove a firm from the shortlist. Only call this when the user asks for it. The slug must match the "
        "stored slug exactly; names are not resolved.",
        _schema({"firm_slug": {"type": "string", "description": "The exact slug on the shortlist."}}, ["firm_slug"]),
        _remove_from_shortlist,
        writes=True,
    ),
    ToolSpec(
        "create_saved_list",
        "Save a named list of firms. Only call this when the user asks for a list to be saved. "
        "Each entry may be a slug or a name and is stored as the firm's slug; if any entry matches no firm, "
        "nothing is saved.",
        _schema(
            {
                "name": {"type": "string", "description": "Name of the list."},
                "firm_slugs": {**_SLUGS, "description": "Firm slugs or names to put in the list."},
            },
            ["name", "firm_slugs"],
        ),
        _create_saved_list,
        writes=True,
    ),
)

BY_NAME: Dict[str, ToolSpec] = {spec.name: spec for spec in REGISTRY}
WRITE_TOOLS: tuple[str, ...] = tuple(spec.name for spec in REGISTRY if spec.writes)


def claude_tools() -> List[Dict[str, Any]]:
    return [
        {"name": s.name, "description": s.description, "input_schema": s.input_schema, "strict": True}
        for s in REGISTRY
    ]


def bind_tools(ctx: ToolContext) -> Dict[str, Callable[..., Any]]:
    return {s.name: (lambda _run=s.run, **args: _run(ctx, **args)) for s in REGISTRY}
