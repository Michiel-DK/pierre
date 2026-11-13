"""In-memory firm store for the showcase agent, loaded from the fictional data in data/agent/firms.json."""
from __future__ import annotations

import calendar
import json
import re
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

AS_OF = "2026-06-30"
DEMO_USER_ID = 1
FAULT_SLUG = "ferrin-lane-capital"
DATA_PATH = Path(__file__).resolve().parents[2] / "data" / "agent" / "firms.json"

_FIRST_ADDED_AT = datetime(2026, 6, 30, 9, 0, 0)


class FirmStore(Protocol):
    """What the carved tools need from a firm store."""

    as_of: str

    def get_firm_by_slug(self, firm_id: str) -> Optional[Dict[str, Any]]: ...

    def search_firm_by_name(self, name: str, limit: int = 5) -> List[Dict[str, Any]]: ...

    def get_portfolio_count(self, firm_slug: str) -> int: ...

    def search_portfolio_companies(
        self, query: str, n_results: int = 20, firm_category: Optional[str] = None
    ) -> List[Dict[str, Any]]: ...

    def get_portfolio_stats(
        self, firm_slugs: Optional[List[str]] = None, top_k: int = 20
    ) -> Dict[str, Any]: ...

    def get_portfolio_by_year(
        self, investment_year: int, firm_slugs: Optional[List[str]] = None, top_k: int = 50
    ) -> Dict[str, Any]: ...

    def search_firms(
        self,
        query: str,
        n_results: int = 20,
        stages: Optional[List[str]] = None,
        sectors: Optional[List[str]] = None,
        geography: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]: ...

    def get_recent_investment_news(
        self,
        sector: Optional[str] = None,
        stage: Optional[str] = None,
        months: int = 6,
        limit: int = 20,
    ) -> List[Dict[str, Any]]: ...

    def shortlist_add(self, user_id: int, firm_slug: str, notes: Optional[str]) -> int: ...

    def shortlist_list(self, user_id: int) -> List[Dict[str, Any]]: ...

    def shortlist_remove(self, user_id: int, firm_slug: str) -> int: ...

    def saved_list_create(self, user_id: int, name: str, firm_slugs: List[str]) -> int: ...

    def final_state(self) -> Dict[str, Any]: ...


def _tokens(text: str) -> set:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _lexical_score(query: str, text: str) -> float:
    q = _tokens(query)
    if not q:
        return 0.0
    return len(q & _tokens(text)) / len(q)


def _months_before(day: date, months: int) -> date:
    index = day.year * 12 + (day.month - 1) - months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


class InMemoryFirmStore:
    """Deterministic store over the fictional firms. One instance is one session and one demo user."""

    def __init__(self, path: Optional[Path] = None) -> None:
        raw = json.loads(Path(path or DATA_PATH).read_text(encoding="utf-8"))
        self.as_of: str = raw["as_of"]
        self._firms: Dict[str, Dict[str, Any]] = {f["firm_id"]: f for f in raw["firms"]}
        self._portfolio: List[Dict[str, Any]] = raw["portfolio"]
        self._news: List[Dict[str, Any]] = raw["news"]
        self._lock = threading.RLock()
        self._shortlist: List[Dict[str, Any]] = []
        self._saved_lists: Dict[str, List[str]] = {}
        self._next_shortlist_id = 1
        self._next_list_id = 1

    @staticmethod
    def _check_user(user_id: int) -> None:
        if user_id != DEMO_USER_ID:
            raise ValueError(f"unknown user_id {user_id!r}: this store serves one demo user")

    def get_firm_by_slug(self, firm_id: str) -> Optional[Dict[str, Any]]:
        firm = self._firms.get(firm_id)
        return dict(firm) if firm else None

    def search_firm_by_name(self, name: str, limit: int = 5) -> List[Dict[str, Any]]:
        lowered = name.lower()
        for guess in (lowered.replace(" ", "_").replace("-", "_"), lowered.replace(" ", "-").replace("_", "-"), name):
            if guess in self._firms:
                return [dict(self._firms[guess])]
        hits = [f for slug, f in sorted(self._firms.items()) if lowered in f["firm_name"].lower()]
        return [dict(f) for f in hits[:limit]]

    def get_portfolio_count(self, firm_slug: str) -> int:
        return sum(1 for p in self._portfolio if p["firm_slug"] == firm_slug)

    def search_portfolio_companies(
        self, query: str, n_results: int = 20, firm_category: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        scored = []
        for p in self._portfolio:
            if firm_category and self._firms[p["firm_slug"]]["category"] != firm_category:
                continue
            score = _lexical_score(query, f"{p['company_name']} {p['description']} {p['sector']}")
            if score > 0:
                scored.append((score, p))
        scored.sort(key=lambda item: (-item[0], item[1]["firm_slug"], item[1]["company_name"]))
        return [
            {
                "company_name": p["company_name"],
                "firm_name": p["firm_name"],
                "firm_slug": p["firm_slug"],
                "sector": p["sector"],
                "description": p["description"],
                "company_url": p["company_url"],
                "investment_date": p["investment_date"],
                "similarity": score,
            }
            for score, p in scored[:n_results]
        ]

    def get_portfolio_stats(
        self, firm_slugs: Optional[List[str]] = None, top_k: int = 20
    ) -> Dict[str, Any]:
        if firm_slugs and FAULT_SLUG in firm_slugs:
            raise TimeoutError("portfolio statistics query timed out")
        rows = [p for p in self._portfolio if not firm_slugs or p["firm_slug"] in firm_slugs]
        by_sector: Dict[str, int] = {}
        by_year: Dict[int, int] = {}
        for p in rows:
            by_sector[p["sector"]] = by_sector.get(p["sector"], 0) + 1
            year = int(p["investment_date"][:4])
            by_year[year] = by_year.get(year, 0) + 1
        sectors = sorted(by_sector.items(), key=lambda item: (-item[1], item[0]))[:top_k]
        return {
            "total_companies": len(rows),
            "scope": "selected firms" if firm_slugs else "full database",
            "top_sectors": [{"sector": s, "count": c} for s, c in sectors],
            "investment_by_year": [{"year": y, "count": by_year[y]} for y in sorted(by_year, reverse=True)],
        }

    def get_portfolio_by_year(
        self, investment_year: int, firm_slugs: Optional[List[str]] = None, top_k: int = 50
    ) -> Dict[str, Any]:
        year = f"{int(investment_year):04d}"
        rows = [
            p
            for p in self._portfolio
            if p["investment_date"][:4] == year and (not firm_slugs or p["firm_slug"] in firm_slugs)
        ]
        rows.sort(key=lambda p: (p["investment_date"], p["firm_slug"], p["company_name"]), reverse=True)
        rows = rows[:top_k]
        return {
            "year": investment_year,
            "count": len(rows),
            "note": "Results are filtered by the investment_date field of each portfolio row.",
            "companies": [
                {
                    "company_name": p["company_name"],
                    "firm_slug": p["firm_slug"],
                    "firm_name": p["firm_name"],
                    "sector": p["sector"],
                    "investment_date": p["investment_date"],
                    "description": p["description"][:200],
                    "company_url": p["company_url"],
                }
                for p in rows
            ],
        }

    def search_firms(
        self,
        query: str,
        n_results: int = 20,
        stages: Optional[List[str]] = None,
        sectors: Optional[List[str]] = None,
        geography: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        scored = []
        for firm in self._firms.values():
            if stages and not any(s.lower() == f.lower() for s in stages for f in firm["stages"]):
                continue
            if sectors and not any(
                s.lower() in f.lower() or f.lower() in s.lower() for s in sectors for f in firm["focus_areas"]
            ):
                continue
            if geography:
                place = " ".join(firm["geographies"] + [firm["hq"]]).lower()
                if not any(g.lower() in place for g in geography):
                    continue
            text = f"{firm['firm_name']} {firm['description']} {' '.join(firm['focus_areas'])} {' '.join(firm['stages'])}"
            scored.append((_lexical_score(query, text), firm))
        scored.sort(key=lambda item: (-item[0], item[1]["firm_id"]))
        return [dict(firm, similarity=score) for score, firm in scored[:n_results]]

    def get_recent_investment_news(
        self,
        sector: Optional[str] = None,
        stage: Optional[str] = None,
        months: int = 6,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        end = date.fromisoformat(self.as_of)
        start = _months_before(end, months)
        rows = []
        for item in self._news:
            published = date.fromisoformat(item["publication_date"])
            if not (start < published <= end):
                continue
            if sector and not any(sector.lower() in s.lower() for s in item["sectors"]):
                continue
            if stage and stage.lower() != item["stage"].lower():
                continue
            rows.append(item)
        rows.sort(key=lambda item: (item["publication_date"], item["firm_slug"]), reverse=True)
        return [dict(item) for item in rows[:limit]]

    def shortlist_add(self, user_id: int, firm_slug: str, notes: Optional[str]) -> int:
        self._check_user(user_id)
        with self._lock:
            for row in self._shortlist:
                if row["firm_slug"] == firm_slug:
                    row["notes"] = notes
                    return row["id"]
            row = {"id": self._next_shortlist_id, "firm_slug": firm_slug, "notes": notes}
            self._next_shortlist_id += 1
            self._shortlist.append(row)
            return row["id"]

    def shortlist_list(self, user_id: int) -> List[Dict[str, Any]]:
        self._check_user(user_id)
        with self._lock:
            rows = []
            for row in reversed(self._shortlist):
                firm = self._firms.get(row["firm_slug"]) or {}
                rows.append(
                    {
                        "id": row["id"],
                        "firm_slug": row["firm_slug"],
                        "firm_name": firm.get("firm_name"),
                        "description": firm.get("description"),
                        "firm_status": "active" if firm else None,
                        "notes": row["notes"],
                        "added_at": _FIRST_ADDED_AT + timedelta(seconds=row["id"]),
                    }
                )
            return rows

    def shortlist_remove(self, user_id: int, firm_slug: str) -> int:
        self._check_user(user_id)
        with self._lock:
            before = len(self._shortlist)
            self._shortlist = [row for row in self._shortlist if row["firm_slug"] != firm_slug]
            return before - len(self._shortlist)

    def saved_list_create(self, user_id: int, name: str, firm_slugs: List[str]) -> int:
        self._check_user(user_id)
        with self._lock:
            self._saved_lists[name] = sorted(set(firm_slugs))
            list_id = self._next_list_id
            self._next_list_id += 1
            return list_id

    def final_state(self) -> Dict[str, Any]:
        with self._lock:
            state: Dict[str, Any] = {}
            if self._shortlist:
                state["shortlist"] = [row["firm_slug"] for row in self._shortlist]
            if self._saved_lists:
                state["saved_lists"] = {name: list(slugs) for name, slugs in self._saved_lists.items()}
            return state
