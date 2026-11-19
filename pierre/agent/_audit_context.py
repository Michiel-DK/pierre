"""Build the claim auditor's evidence dict from the tool calls and results of a session."""
from __future__ import annotations

import json
from typing import Any, Dict, List

# Every firm fact a tool result shows the agent, so a true claim about it has evidence. other_matches (lookup_firm): the
# other firms the name matched. geographies and the fund-size keys (compare_firms). Empty values are dropped, as before.
# Not here yet: lookup_firm's category and list_shortlist's firm_status. Adding them changes the recorded auditor requests,
# and the recordings can only be made again with live calls.
_PROFILE_KEYS = (
    "firm_name", "hq", "stages", "focus_areas", "description", "portfolio_count", "fit_note", "other_matches",
    "geographies", "latest_fund_size", "latest_fund_date", "fund_size_label", "fund_size_source",
)


def _cheque(source: Dict[str, Any]) -> str | None:
    lo, hi = source.get("check_size_minimum"), source.get("check_size_maximum")
    if lo is not None and hi is not None:
        return f"EUR {lo}-{hi}"
    return str(source["check_size_range"]) if source.get("check_size_range") else None


def _profile(source: Dict[str, Any]) -> Dict[str, Any]:
    profile = {k: source[k] for k in _PROFILE_KEYS if source.get(k) not in (None, "", [])}
    if cheque := _cheque(source):
        profile["cheque_range"] = cheque
    return profile


def _company(row: Dict[str, Any]) -> Dict[str, Any]:
    firm = row.get("vc_firm") or row.get("firm_name") or ""
    year = row.get("investment_date") or ""
    return {
        "name": row.get("company_name"),
        "sector": row.get("sector"),
        "description": f"[{firm}, {year}] {row.get('description') or ''}",
    }


def audit_context_from_turn(tool_calls: List[Dict[str, Any]], tool_results: List[Any]) -> Dict[str, Any]:
    """Map tool results onto the auditor's keys. ``tool_calls`` and ``tool_results`` are index-aligned."""
    profiles: Dict[str, Dict[str, Any]] = {}
    companies: List[Dict[str, Any]] = []
    deals: List[Dict[str, Any]] = []
    actions: List[str] = []
    errors: List[str] = []

    def add_profile(source: Dict[str, Any]) -> None:
        key = source.get("firm_id") or source.get("firm_name") or ""
        profiles.setdefault(key, {}).update(_profile(source))

    for call, result in zip(tool_calls, tool_results):
        tool, args = call["tool"], call.get("args") or {}
        if not isinstance(result, dict):
            continue
        if "error" in result:
            errors.append(f"{tool}({json.dumps(args, sort_keys=True)}): {result['error']}")
            continue
        if tool == "lookup_firm" and result.get("found"):
            add_profile(result)
        elif tool == "compare_firms":
            for firm in result.get("firms", []):
                add_profile(firm)
        elif tool == "check_raise_fit":
            for firm in result.get("matches", []):
                add_profile(firm)
        elif tool in ("search_portfolio", "filter_portfolio_by_year"):
            companies.extend(_company(r) for r in result.get("companies", []))
        elif tool == "get_portfolio_stats":
            scope = ", ".join(args.get("firm_ids") or []) or "all firms"
            profiles[f"portfolio statistics: {scope}"] = {
                "firm_name": f"(portfolio statistics for {scope})",
                "portfolio_count": result.get("total_companies"),
                "focus_areas": [f"{s['sector']}: {s['count']}" for s in result.get("top_sectors", [])],
                "description": "Companies per investment year: "
                + ", ".join(f"{y['year']}: {y['count']}" for y in result.get("investment_by_year", [])),
            }
        elif tool == "find_recent_investments":
            for a in result.get("articles", []):
                deals.append(
                    {
                        "title": f"{a.get('title')} ({a.get('publication_date')})",
                        "excerpt": f"{a.get('firm_name')}; stage {a.get('stage')}; sectors {', '.join(a.get('sectors') or [])}",
                        "company_mentioned": a.get("company_mentioned"),
                    }
                )
        elif tool == "list_shortlist":
            for e in result.get("shortlist", []):
                add_profile({"firm_id": e["firm_slug"], "firm_name": e.get("firm_name"), "description": e.get("description")})
            entries = [f"{e['firm_slug']} ({'notes: ' + e['notes'] if e.get('notes') else 'no notes'})" for e in result.get("shortlist", [])]
            actions.append("Shortlist now contains (newest first): " + (", ".join(entries) or "nothing"))
        elif tool in ("add_to_shortlist", "remove_from_shortlist", "create_saved_list"):
            actions.append(str(result.get("message", "")))
    return {
        "firm_profiles": list(profiles.values()),
        "portfolio_companies": companies,
        "recent_deals": deals,
        "actions_taken": [a for a in actions if a],
        "tool_errors": errors,
    }
