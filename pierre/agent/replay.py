"""Record real model calls to a JSONL file, and replay them with no network.

The recorder wraps a client that exposes ``messages.create`` and writes one line per call: the sha256 of the
request, a short summary of it, and the response body. No headers, request ids or keys are written. The replayer
serves the responses in order and refuses a request whose hash differs from the recorded one, so a changed prompt,
tool description or loop fails loudly until the session is recorded again.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional


class ReplayMismatch(RuntimeError):
    abort_run = True  # the claim auditor re-raises these instead of reporting an audit error


def request_sha256(kwargs: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(kwargs, sort_keys=True).encode("utf-8")).hexdigest()


def _summary(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "model": kwargs.get("model"),
        "effort": (kwargs.get("output_config") or {}).get("effort"),
        "n_messages": len(kwargs.get("messages", [])),
        "tools": [t["name"] for t in kwargs.get("tools", [])],
    }


class _Messages:
    def __init__(self, owner: "_Base", role: str) -> None:
        self._owner, self._role = owner, role

    def create(self, **kwargs: Any) -> Dict[str, Any]:
        return self._owner._call(self._role, kwargs)


class _Base:
    def __init__(self) -> None:
        self.messages = _Messages(self, "agent")

    def with_role(self, role: str) -> Any:
        """A view of the same client whose calls are tagged ``role`` (the auditor uses ``auditor``)."""
        view = type("RoleView", (), {})()
        view.messages = _Messages(self, role)
        return view

    def _call(self, role: str, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError


class RecordingClient(_Base):
    def __init__(self, inner: Any, path: Path, header: Dict[str, Any]) -> None:
        super().__init__()
        self.inner, self.path = inner, Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"recorded": True, **header}, sort_keys=True) + "\n")

    def _call(self, role: str, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        frozen = copy.deepcopy(kwargs)  # the caller keeps appending to its message list
        sha = request_sha256(frozen)
        message = self.inner.messages.create(**frozen)
        response = message if isinstance(message, dict) else message.to_dict()
        line = {"role": role, "request_sha256": sha, "request": _summary(frozen), "response": response}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, sort_keys=True, ensure_ascii=False) + "\n")
        return copy.deepcopy(response)


class ReplayClient(_Base):
    def __init__(self, path: Path) -> None:
        super().__init__()
        lines = [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]
        self.header, self._calls = lines[0], lines[1:]
        self._next = 0
        self.path = Path(path)

    def _call(self, role: str, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        if self._next >= len(self._calls):
            raise ReplayMismatch(f"{self.path.name}: call {self._next + 1} was never recorded ({len(self._calls)} recorded)")
        recorded = self._calls[self._next]
        sha = request_sha256(kwargs)
        if recorded["role"] != role or recorded["request_sha256"] != sha:
            raise ReplayMismatch(
                f"{self.path.name}: call {self._next + 1} ({role}) differs from the recording "
                f"(sha {sha[:12]} vs {recorded['request_sha256'][:12]}). A prompt, tool description or loop changed; "
                "record the session again."
            )
        self._next += 1
        return copy.deepcopy(recorded["response"])

    def assert_exhausted(self) -> None:
        if self._next != len(self._calls):
            raise ReplayMismatch(f"{self.path.name}: {len(self._calls) - self._next} recorded calls were not used")
