"""Cost per turn: tokens times the dated price table in ``prices.json``. Stdlib only.

pierre has no users, so this prices the scheduled canary and past eval runs. It prices tokens at list price: it knows
nothing about discounts, batch, the US-only multiplier, or a model's price changing after ``as_of``.
"""
from __future__ import annotations

import json
import logging
import threading
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)

PRICES_PATH = Path(__file__).with_name("prices.json")
_MTOK = Decimal(1_000_000)
_warned: set = set()
_lock = threading.Lock()


@lru_cache(maxsize=1)
def prices() -> Dict[str, Any]:
    return json.loads(PRICES_PATH.read_text(encoding="utf-8"))


def reset_warnings() -> None:
    with _lock:
        _warned.clear()


def _warn_once(model: str) -> None:
    with _lock:
        if model in _warned:
            return
        _warned.add(model)
    logger.warning("no price row for model %r in prices.json (as of %s): cost is null, not guessed", model, prices()["as_of"])


def _tokens(usage: Mapping[str, Any], key: str) -> int:
    return int(usage.get(key) or 0)


def _usd(model: str, usage: Mapping[str, Any]) -> Optional[Decimal]:
    # ``input_tokens`` is taken as EXCLUDING cache reads and writes, which have their own fields (the pricing page's cache
    # example lists them separately; the SDK docstring does not say). Every cache field in the 13 Nov traces is 0.
    row = prices()["models"].get(model)
    if row is None:
        _warn_once(model)
        return None
    created = usage.get("cache_creation") or {}
    w5, w1h = _tokens(created, "ephemeral_5m_input_tokens"), _tokens(created, "ephemeral_1h_input_tokens")
    if not (w5 or w1h):
        w5 = _tokens(usage, "cache_creation_input_tokens")  # no split given: the lower (5 minute) write rate
    parts = (
        (_tokens(usage, "input_tokens"), "input"),
        (w5, "cache_write_5m"),
        (w1h, "cache_write_1h"),
        (_tokens(usage, "cache_read_input_tokens"), "cache_read"),
        (_tokens(usage, "output_tokens"), "output"),  # inclusive of thinking tokens (anthropic/types/usage.py:34)
    )
    return sum((Decimal(n) * Decimal(str(row[k])) / _MTOK for n, k in parts), Decimal(0))


def usage_cost(model: str, usage: Mapping[str, Any]) -> Optional[float]:
    """USD for one usage record, or None (plus one warning per model) when the model has no price row."""
    value = _usd(model, usage)
    return None if value is None else float(value)


def _sum(model: str, usages: Iterable[Mapping[str, Any]]) -> Optional[Decimal]:
    total = Decimal(0)
    for u in usages:
        value = _usd(model, u)
        if value is None:
            return None
        total += value
    return total


def turn_cost(turn: Any) -> Optional[Dict[str, Any]]:
    """``{"total_usd", "agent_usd", "auditor_usd", "currency", "prices_as_of"}`` for a TurnResult, or None when any
    model it used has no price row (never a partial total)."""
    agent = _sum(turn.model or "unknown", turn.usage)
    auditor = _sum(turn.auditor_model or "unknown", turn.auditor_usage) if turn.auditor_usage else Decimal(0)
    if agent is None or auditor is None:
        return None
    return {
        "total_usd": round(float(agent + auditor), 6),
        "agent_usd": round(float(agent), 6),
        "auditor_usd": round(float(auditor), 6),
        "currency": prices()["currency"],
        "prices_as_of": prices()["as_of"],
    }
