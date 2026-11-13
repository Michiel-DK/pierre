"""Per-turn cache of executed tool calls, so a repeated identical call is served from memory."""
from __future__ import annotations

import json
from typing import Any, Dict, Mapping, Optional, Tuple


def canonicalise_args(args: Optional[Mapping[str, Any]]) -> str:
    if not args:
        return "{}"
    return json.dumps(args, sort_keys=True, default=str, ensure_ascii=False)


class CallDeduper:
    """Remembers each (tool, arguments) result for one user turn."""

    __slots__ = ("_cache",)

    def __init__(self) -> None:
        self._cache: Dict[Tuple[str, str], Any] = {}

    def reset(self) -> None:
        self._cache.clear()

    def observe(
        self,
        tool_name: str,
        args: Optional[Mapping[str, Any]],
    ) -> Tuple[bool, Any]:
        key = self._key(tool_name, args)
        if key in self._cache:
            return True, self._cache[key]
        return False, None

    def record(
        self,
        tool_name: str,
        args: Optional[Mapping[str, Any]],
        result: Any,
    ) -> None:
        self._cache[self._key(tool_name, args)] = result

    def get_or_run(
        self,
        tool_name: str,
        args: Optional[Mapping[str, Any]],
        runner,
    ) -> Tuple[bool, Any]:
        seen, cached = self.observe(tool_name, args)
        if seen:
            return True, cached
        result = runner()
        self.record(tool_name, args, result)
        return False, result

    def size(self) -> int:
        return len(self._cache)

    def keys(self) -> list[Tuple[str, str]]:
        return list(self._cache.keys())

    @staticmethod
    def _key(tool_name: str, args: Optional[Mapping[str, Any]]) -> Tuple[str, str]:
        return (tool_name, canonicalise_args(args))
