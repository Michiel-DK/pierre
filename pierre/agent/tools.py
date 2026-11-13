"""Showcase tool bodies and the dispatcher that runs them, over a firm store (see backend.py)."""
from __future__ import annotations

import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from pierre.agent import _formatters as F
from pierre.agent._dispatch_dedup import CallDeduper, canonicalise_args
from pierre.agent.backend import AS_OF

logger = logging.getLogger(__name__)

KNOWN_FIRM_CATEGORIES: tuple[str, ...] = (
    "Venture Capital",
    "Accelerator",
    "Family Office",
)


def require_known_category_or_none(field: str, value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(
            f"{field} must be a string or None, got {type(value).__name__}."
        )
    if value not in KNOWN_FIRM_CATEGORIES:
        raise ValueError(
            f"{field}={value!r} is not a known firm category. Accepted "
            f"values: {list(KNOWN_FIRM_CATEGORIES)}. Omit {field} to search "
            f"across all categories."
        )
    return value


def resolve_firm_slug(
    store,
    firm_name_or_slug: str,
    firm_profiles: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    if firm_profiles and firm_name_or_slug in firm_profiles:
        return firm_profiles[firm_name_or_slug]
    profile = store.get_firm_by_slug(firm_name_or_slug)
    if profile:
        return profile
    matches = store.search_firm_by_name(firm_name_or_slug, limit=1)
    if matches:
        return store.get_firm_by_slug(matches[0]["firm_id"])
    return None


def resolve_firm_for_shortlist(
    store, slug: str, firm_profiles: Optional[Dict[str, Dict[str, Any]]] = None
) -> Optional[Dict[str, Any]]:
    profile = resolve_firm_slug(store, slug, firm_profiles)
    if not profile:
        return None
    return {
        "firm_slug": profile.get("firm_id") or profile.get("firm_slug"),
        "firm_name": profile.get("firm_name"),
    }


def compare_firms(
    store,
    firm_ids: List[str],
    firm_profiles: Dict[str, Dict[str, Any]],
    dimension: Optional[str] = None,
) -> Dict[str, Any]:
    def _resolver(fid: str) -> Optional[Dict[str, Any]]:
        return firm_profiles.get(fid) or store.get_firm_by_slug(fid)

    return F.format_compare_firms(firm_ids, _resolver, dimension)


def search_portfolio(
    store,
    query: str,
    top_k: int = 10,
    firm_category: Optional[str] = None,
) -> Dict[str, Any]:
    firm_category = require_known_category_or_none("firm_category", firm_category)
    results = store.search_portfolio_companies(
        query=query, n_results=top_k, firm_category=firm_category
    )
    return F.format_search_all_portfolio(results)


def lookup_firm(store, firm_name: str) -> Dict[str, Any]:
    matches = store.search_firm_by_name(firm_name, limit=3)
    if not matches:
        return F.format_lookup_firm_not_found(firm_name)
    firm = matches[0]
    firm_id = firm.get("firm_id")
    portfolio_count = store.get_portfolio_count(firm_id) if firm_id else 0
    return F.format_lookup_firm_found(matches, portfolio_count)


def get_portfolio_stats(
    store,
    firm_profiles: Dict[str, Dict[str, Any]],
    firm_ids: Optional[List[str]] = None,
    top_k: int = 20,
) -> Dict[str, Any]:
    resolved: List[str] = []
    for fid in (firm_ids or []):
        if fid in firm_profiles:
            resolved.append(fid)
        else:
            matches = store.search_firm_by_name(fid, limit=1)
            resolved.append(matches[0]["firm_id"] if matches else fid)
    slugs = resolved if resolved else None
    return store.get_portfolio_stats(firm_slugs=slugs, top_k=top_k)


def filter_portfolio_by_year(
    store,
    firm_profiles: Dict[str, Dict[str, Any]],
    investment_year: int,
    firm_ids: Optional[List[str]] = None,
    top_k: int = 50,
) -> Dict[str, Any]:
    slugs: List[str] = []
    for fid in (firm_ids or []):
        if fid in firm_profiles:
            slugs.append(fid)
        else:
            matches = store.search_firm_by_name(fid, limit=1)
            slugs.append(matches[0]["firm_id"] if matches else fid)
    result = store.get_portfolio_by_year(
        investment_year=investment_year,
        firm_slugs=slugs or None,
        top_k=top_k,
    )
    return {**result, "as_of": AS_OF}


def check_raise_fit(
    store,
    stage: str,
    sector: str,
    geography: str,
    amount_min: Optional[int] = None,
    amount_max: Optional[int] = None,
) -> Dict[str, Any]:
    results = store.search_firms(
        query=sector,
        n_results=20,
        stages=[stage],
        sectors=[sector],
        geography=[geography],
    )
    return F.format_check_raise_fit(results, amount_min=amount_min, amount_max=amount_max)


def find_recent_investments(
    store,
    sector: Optional[str] = None,
    stage: Optional[str] = None,
    months: int = 6,
) -> Dict[str, Any]:
    articles = store.get_recent_investment_news(
        sector=sector, stage=stage, months=months, limit=20,
    )
    if not articles and sector:
        terms = [t.strip() for t in re.split(r"[\s+/&,]+", sector) if t.strip() and len(t.strip()) > 2]
        if len(terms) > 1:
            seen_ids: set = set()
            merged_articles: list = []
            for term in terms:
                for a in store.get_recent_investment_news(
                    sector=term, stage=stage, months=months, limit=20,
                ):
                    key = (a.get("firm_slug"), a.get("title"))
                    if key not in seen_ids:
                        seen_ids.add(key)
                        merged_articles.append(a)
            articles = merged_articles[:20]
    return {**F.format_recent_investments(articles), "as_of": AS_OF}


def create_saved_list(
    store,
    user_id: Optional[int],
    name: str,
    firm_slugs: List[str],
    *,
    resolve_firm: Optional[Any] = None,
) -> Dict[str, Any]:
    if not user_id:
        return {
            "success": False,
            "error": "No user session — cannot save lists without a user",
        }
    if not name or not name.strip():
        return {"success": False, "error": "List name cannot be empty"}
    if not firm_slugs:
        return {"success": False, "error": "No firm slugs provided"}

    if resolve_firm is not None:
        canonical_slugs: List[str] = []
        for entry in firm_slugs:
            resolved = resolve_firm(entry) if isinstance(entry, str) and entry.strip() else None
            if resolved is None or not resolved.get("firm_slug"):
                return {"error": f"No firm matches '{entry}'. Nothing was saved."}
            if resolved["firm_slug"] not in canonical_slugs:
                canonical_slugs.append(resolved["firm_slug"])
        firm_slugs = canonical_slugs

    try:
        list_id = store.saved_list_create(
            user_id=user_id, name=name.strip(), firm_slugs=firm_slugs,
        )
        return {
            "success": True,
            "list_id": list_id,
            "name": name.strip(),
            "firm_slugs": firm_slugs,
            "message": f"Saved list '{name.strip()}' with {len(firm_slugs)} firms.",
        }
    except Exception as exc:
        logger.error("create_saved_list failed: %s", exc)
        return {"success": False, "error": str(exc)}


class ToolDispatcher:
    """Runs tool calls by name. With a deduper, a repeat of (tool, arguments) in one turn is served from its cache."""

    def __init__(
        self,
        tool_functions: Mapping[str, Callable[..., Any]],
        deduper: Optional[CallDeduper] = None,
        max_workers: int = 6,
    ) -> None:
        self.tool_functions = dict(tool_functions)
        self.deduper = deduper
        self.max_workers = max_workers
        self.dedup_hits = 0
        self._lock = threading.Lock()

    def reset_turn(self) -> None:
        if self.deduper is not None:
            self.deduper.reset()
        self.dedup_hits = 0

    def execute(self, tool_name: str, args: Dict[str, Any]) -> Any:
        if tool_name not in self.tool_functions:
            return {"error": f"Unknown tool: {tool_name}"}

        if self.deduper is not None:
            seen, cached = self.deduper.observe(tool_name, args)
            if seen:
                logger.warning(
                    "dispatch dedupe: skipping repeated tool call %s(%r) in same turn",
                    tool_name,
                    args,
                )
                with self._lock:
                    self.dedup_hits += 1
                return cached

        try:
            result = self.tool_functions[tool_name](**args)
        except Exception as exc:
            logger.error("Tool execution failed (%s): %s", tool_name, exc)
            result = {"error": f"Tool '{tool_name}' encountered an internal error. Please try rephrasing your question."}

        if self.deduper is not None:
            self.deduper.record(tool_name, args, result)
        return result

    def execute_parallel(self, calls: List[Tuple[str, Dict[str, Any]]]) -> List[Any]:
        """Run one turn's calls concurrently; results come back in call order.

        Repeats are resolved in call order before anything is submitted, so the dedup count does not depend on
        thread timing: an identical (tool, arguments) pair is run once and its result reused.
        """
        if not calls:
            return []
        results: Dict[int, Any] = {}
        first_index: Dict[Tuple[str, str], int] = {}
        repeat_of: Dict[int, int] = {}
        to_run: List[int] = []
        for idx, (name, args) in enumerate(calls):
            key = (name, canonicalise_args(args))
            if self.deduper is not None and name in self.tool_functions:
                if key in first_index:
                    repeat_of[idx] = first_index[key]
                    continue
                first_index[key] = idx
            to_run.append(idx)
        if to_run:
            with ThreadPoolExecutor(max_workers=min(len(to_run), self.max_workers)) as pool:
                futures = {pool.submit(self.execute, calls[i][0], dict(calls[i][1])): i for i in to_run}
                for future, idx in futures.items():
                    results[idx] = future.result()
        for idx, source in repeat_of.items():
            results[idx] = results[source]
            with self._lock:
                self.dedup_hits += 1
        return [results[i] for i in range(len(calls))]
