"""A local model behind Ollama's chat API, shaped like the Anthropic client the loop calls. Stdlib only.

The loop needs one thing from a client: ``client.messages.create(**kwargs)`` taking Anthropic request fields and
returning an Anthropic-shaped message dict. This adapter translates both ways over ``POST /api/chat`` (no streaming):

- request: ``system`` becomes a system message; ``tools`` become function tools with the same JSON schema; an assistant
  message's ``tool_use`` blocks become ``tool_calls``; a user message of ``tool_result`` blocks becomes one ``tool``
  message per result, carrying the tool's name. ``max_tokens`` becomes ``num_predict``. ``tool_choice`` and
  ``output_config`` (effort) have no Ollama counterpart and are dropped.
- response: text becomes a text block, each tool call a ``tool_use`` block with a deterministic id (step and index).
  ``done_reason == "length"`` maps to ``max_tokens`` before anything else, so the loop never runs a cut-off tool call.
  ``prompt_eval_count`` / ``eval_count`` become ``usage``.

Two settings are always sent, never left to the model's defaults: ``think`` (gemma's default is thinking on) and
``num_ctx``. Ollama cuts a prompt that is longer than ``num_ctx`` without an error, and a model reading a cut prompt
looks fooled when it was blind. So a request whose chars / 4 estimate exceeds ``num_ctx`` minus the output budget
raises ``ContextOverflow`` before it is sent, and a response whose ``prompt_eval_count`` reaches ``num_ctx`` raises it
after. Either is an infrastructure failure of the session, not a model outcome.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

DEFAULT_URL = "http://127.0.0.1:11434"
DEFAULT_NUM_CTX = 16384
DEFAULT_NUM_PREDICT = 2048
DEFAULT_TIMEOUT_S = 600.0


class OllamaError(RuntimeError):
    pass


class ContextOverflow(OllamaError):
    pass


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content if b.get("type") == "text")


def to_ollama_messages(system: str, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = [{"role": "system", "content": system}] if system else []
    names: Dict[str, str] = {}
    for m in messages:
        content = m["content"]
        if m["role"] == "assistant":
            calls = []
            for b in content:
                if b.get("type") == "tool_use":
                    names[b["id"]] = b["name"]
                    calls.append({"function": {"name": b["name"], "arguments": dict(b.get("input") or {})}})
            msg: Dict[str, Any] = {"role": "assistant", "content": _text(content)}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
        elif isinstance(content, str):
            out.append({"role": "user", "content": content})
        else:
            for b in content:
                if b.get("type") == "tool_result":
                    out.append({"role": "tool", "tool_name": names.get(b["tool_use_id"], ""), "content": _text(b.get("content", ""))})
                elif b.get("type") == "text":
                    out.append({"role": "user", "content": b["text"]})
    return out


def to_ollama_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t["input_schema"]}}
        for t in tools
    ]


def _args(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"_unparsed_arguments": raw}
        return parsed if isinstance(parsed, dict) else {"_arguments": parsed}
    return {}


def from_ollama_response(resp: Dict[str, Any], step: int) -> Dict[str, Any]:
    msg = resp.get("message") or {}
    content: List[Dict[str, Any]] = []
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})
    for i, call in enumerate(msg.get("tool_calls") or []):
        fn = call.get("function") or {}
        content.append({"type": "tool_use", "id": f"toolu_local_{step}_{i}", "name": fn.get("name", ""), "input": _args(fn.get("arguments"))})
    if resp.get("done_reason") == "length":
        stop = "max_tokens"
    elif any(b["type"] == "tool_use" for b in content):
        stop = "tool_use"
    else:
        stop = "end_turn"
    return {
        "id": f"msg_local_{step}",
        "type": "message",
        "role": "assistant",
        "model": resp.get("model", ""),
        "content": content,
        "stop_reason": stop,
        "usage": {"input_tokens": int(resp.get("prompt_eval_count") or 0), "output_tokens": int(resp.get("eval_count") or 0)},
    }


class OllamaClient:
    """``OllamaClient(...).messages.create(**anthropic_kwargs)`` -> an Anthropic-shaped message dict."""

    def __init__(
        self,
        base_url: str = DEFAULT_URL,
        num_ctx: int = DEFAULT_NUM_CTX,
        num_predict: int = DEFAULT_NUM_PREDICT,
        think: bool = False,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        options: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.num_ctx, self.num_predict, self.think, self.timeout_s = num_ctx, num_predict, think, timeout_s
        self.options = dict(options or {})
        self.messages = self
        self.requests: List[Dict[str, Any]] = []  # the bodies sent, for the trace and tests

    def settings(self) -> Dict[str, Any]:
        return {"provider": "ollama", "num_ctx": self.num_ctx, "num_predict": self.num_predict, "think": self.think, **self.options}

    def body(self, **kwargs: Any) -> Dict[str, Any]:
        num_predict = min(int(kwargs.get("max_tokens") or self.num_predict), self.num_predict)
        return {
            "model": kwargs["model"],
            "messages": to_ollama_messages(kwargs.get("system") or "", kwargs.get("messages") or []),
            "tools": to_ollama_tools(kwargs.get("tools") or []),
            "stream": False,
            "think": self.think,
            "options": {**self.options, "num_ctx": self.num_ctx, "num_predict": num_predict},
        }

    def create(self, **kwargs: Any) -> Dict[str, Any]:
        body = self.body(**kwargs)
        est = len(json.dumps(body["messages"], ensure_ascii=False) + json.dumps(body["tools"], ensure_ascii=False)) // 4
        room = self.num_ctx - body["options"]["num_predict"]
        if est > room:
            raise ContextOverflow(f"prompt estimate {est} tokens is over num_ctx {self.num_ctx} minus num_predict {body['options']['num_predict']}")
        self.requests.append(body)
        req = urllib.request.Request(
            f"{self.base_url}/api/chat", data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as fh:
                resp = json.loads(fh.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise OllamaError(f"ollama HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}") from exc
        if resp.get("error"):
            raise OllamaError(f"ollama: {str(resp['error'])[:200]}")
        if int(resp.get("prompt_eval_count") or 0) >= self.num_ctx:
            raise ContextOverflow(f"prompt_eval_count {resp.get('prompt_eval_count')} reached num_ctx {self.num_ctx}: the prompt was cut")
        step = sum(1 for m in kwargs.get("messages") or [] if m["role"] == "assistant")
        return from_ollama_response(resp, step)
