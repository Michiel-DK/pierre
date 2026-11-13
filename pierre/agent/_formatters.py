"""Shape store rows into the dicts the showcase tools return to the model."""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def format_compare_firms(
    firm_ids: List[str],
    profile_resolver,
    dimension: Optional[str] = None,
) -> Dict[str, Any]:
    firms = []
    for fid in firm_ids:
        profile = profile_resolver(fid)
        if not profile:
            continue
        firms.append({
            "firm_name": profile.get("firm_name"),
            "firm_id": fid,
            "hq": profile.get("hq"),
            "stages": profile.get("stages") or [],
            "focus_areas": (profile.get("focus_areas") or [])[:6],
            "check_size_range": (
                f"{profile.get('check_size_minimum') or '?'} – "
                f"{profile.get('check_size_maximum') or '?'}"
            ),
            "geographies": (profile.get("geographies") or [])[:5],
            "latest_fund_size": profile.get("latest_fund_size"),
            "latest_fund_date": profile.get("latest_fund_date"),
            "fund_size_label": profile.get("fund_size_label"),
            "fund_size_source": profile.get("fund_size_source"),
        })
    return {"firms": firms, "dimension": dimension}


def format_search_all_portfolio(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "count": len(results),
        "companies": [
            {
                "company_name": r.get("company_name"),
                "vc_firm": r.get("firm_name"),
                "sector": r.get("sector"),
                "description": (r.get("description") or "")[:300],
                "similarity": round(float(r.get("similarity") or 0), 3),
                "company_url": r.get("company_url"),
                "investment_date": r.get("investment_date"),
            }
            for r in results
        ],
    }


def format_lookup_firm_not_found(firm_name: str) -> Dict[str, Any]:
    return {
        "found": False,
        "firm_name": firm_name,
        "message": f"No firm named '{firm_name}' found in the database.",
    }


def format_lookup_firm_found(
    matches: List[Dict[str, Any]],
    portfolio_count: int,
) -> Dict[str, Any]:
    firm = matches[0]
    return {
        "found": True,
        "firm_id": firm.get("firm_id"),
        "firm_name": firm.get("firm_name"),
        "hq": firm.get("hq"),
        "category": firm.get("category"),
        "stages": firm.get("stages") or [],
        "focus_areas": firm.get("focus_areas") or [],
        "description": (firm.get("description") or "")[:500],
        "check_size_minimum": firm.get("check_size_minimum"),
        "check_size_maximum": firm.get("check_size_maximum"),
        "portfolio_count": portfolio_count,
        "other_matches": [m.get("firm_name") for m in matches[1:]],
    }


def parse_check_size(raw) -> Optional[float]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = (
        str(raw)
        .lower()
        .replace(",", "")
        .replace(" ", "")
        .replace("€", "")
        .replace("$", "")
        .replace("£", "")
    )
    try:
        if s.endswith("m"):
            return float(s[:-1]) * 1_000_000
        elif s.endswith("k"):
            return float(s[:-1]) * 1_000
        return float(s)
    except (ValueError, AttributeError):
        return None


def format_check_raise_fit(
    results: List[Dict[str, Any]],
    amount_min: Optional[int] = None,
    amount_max: Optional[int] = None,
) -> Dict[str, Any]:
    matches = []
    for r in results:
        check_min_raw = r.get("check_size_minimum")
        check_max_raw = r.get("check_size_maximum")

        if amount_min is not None or amount_max is not None:
            raise_mid: Optional[float] = None
            if amount_min is not None and amount_max is not None:
                raise_mid = (amount_min + amount_max) / 2
            elif amount_min is not None:
                raise_mid = float(amount_min)
            elif amount_max is not None:
                raise_mid = float(amount_max)

            c_min = parse_check_size(check_min_raw)
            c_max = parse_check_size(check_max_raw)

            if c_min is None and c_max is None:
                fit_note = "check size unknown"
            elif raise_mid is not None and c_min is not None and raise_mid < c_min:
                fit_note = "check size too large (raise below minimum)"
            elif raise_mid is not None and c_max is not None and raise_mid > c_max:
                fit_note = "check size too small (raise above maximum)"
            else:
                fit_note = "check size matches"
        else:
            fit_note = "check size not evaluated"

        matches.append({
            "firm_name": r.get("firm_name"),
            "firm_id": r.get("firm_id"),
            "hq": r.get("hq"),
            "stages": r.get("stages") or [],
            "check_size_range": f"{check_min_raw or '?'} – {check_max_raw or '?'}",
            "fit_note": fit_note,
            "similarity": round(float(r.get("similarity") or 0), 3),
        })
    return {"count": len(matches), "matches": matches}


def format_recent_investments(articles: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_firm: Dict[str, list] = {}
    for a in articles:
        fn = a.get("firm_name") or a.get("firm_slug") or "unknown"
        by_firm.setdefault(fn, []).append({
            "title": a.get("title"),
            "company_mentioned": a.get("company_mentioned"),
            "sectors": a.get("sectors"),
            "stage": a.get("stage"),
            "publication_date": str(a.get("publication_date") or ""),
        })
    return {
        "count": len(articles),
        "by_firm": by_firm,
        "articles": [
            {
                "firm_name": a.get("firm_name"),
                "firm_slug": a.get("firm_slug"),
                "title": a.get("title"),
                "company_mentioned": a.get("company_mentioned"),
                "sectors": a.get("sectors"),
                "stage": a.get("stage"),
                "publication_date": str(a.get("publication_date") or ""),
            }
            for a in articles
        ],
    }
