"""Shortlist tool bodies: validation, slug canonicalisation and response shapes over a firm store."""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def add_to_shortlist(
    store: Any,
    user_id: int,
    firm_slug: str,
    notes: Optional[str] = None,
    *,
    resolve_firm: Optional[Any] = None,
) -> dict[str, Any]:
    if not isinstance(firm_slug, str) or not firm_slug.strip():
        return {"error": "firm_slug must be a non-empty string."}

    original_slug = firm_slug
    canonicalised = False
    if resolve_firm is not None:
        resolved = resolve_firm(firm_slug)
        if resolved is None:
            return {
                "error": (
                    f"No firm matches '{firm_slug}'. Use `lookup_firm` to find "
                    f"the right slug, then call `add_to_shortlist` again with "
                    f"the canonical slug."
                )
            }
        canonical = resolved.get("firm_slug") if isinstance(resolved, dict) else None
        if canonical and canonical != firm_slug:
            logger.info(
                "add_to_shortlist: canonicalised firm slug %r → %r",
                firm_slug, canonical,
            )
            firm_slug = canonical
            canonicalised = True

    cleaned_notes: Optional[str] = None
    if isinstance(notes, str) and notes.strip():
        cleaned_notes = notes.strip()

    try:
        shortlist_id = store.shortlist_add(
            user_id=user_id, firm_slug=firm_slug.strip(), notes=cleaned_notes,
        )
    except Exception:
        logger.exception(
            "add_to_shortlist: insert failed for user_id=%s firm_slug=%s",
            user_id, firm_slug,
        )
        return {"error": "Failed to add to shortlist."}

    if canonicalised:
        msg = (
            f"Added {firm_slug} to shortlist (canonicalised from "
            f"'{original_slug}')."
        )
    else:
        msg = f"Added {firm_slug} to shortlist."
    return {"shortlist_id": shortlist_id, "message": msg}


def list_shortlist(store: Any, user_id: int) -> dict[str, Any]:
    try:
        rows = store.shortlist_list(user_id=user_id)
    except Exception:
        logger.exception("list_shortlist: select failed for user_id=%s", user_id)
        return {"error": "Failed to list shortlist."}

    shortlist = [
        {
            "id": r["id"],
            "firm_slug": r["firm_slug"],
            "firm_name": r["firm_name"],
            "description": r["description"],
            "firm_status": r["firm_status"],
            "notes": r["notes"],
            "added_at": r["added_at"].isoformat() if r["added_at"] else None,
        }
        for r in rows
    ]
    return {"shortlist": shortlist, "total": len(shortlist)}


def remove_from_shortlist(
    store: Any,
    user_id: int,
    firm_slug: str,
) -> dict[str, Any]:
    if not isinstance(firm_slug, str) or not firm_slug.strip():
        return {"error": "firm_slug must be a non-empty string."}

    try:
        deleted = store.shortlist_remove(user_id=user_id, firm_slug=firm_slug.strip()) or 0
    except Exception:
        logger.exception(
            "remove_from_shortlist: delete failed for user_id=%s firm_slug=%s",
            user_id, firm_slug,
        )
        return {"error": "Failed to remove from shortlist."}

    if deleted == 0:
        return {
            "success": False,
            "message": f"No shortlist entry with firm_slug='{firm_slug}' found for this user.",
        }
    return {
        "success": True,
        "message": f"Removed {firm_slug} from shortlist.",
    }
