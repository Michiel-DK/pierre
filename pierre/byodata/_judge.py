"""LLM fit-judge for BYO-data rows.

Same architecture as the retrieval reranker (``pierre/matching/reranker.py``)
— listwise grading in batches, ``temperature=0``, structured output, and
the never-raises contract where a failed batch degrades to "ungraded, keep
incoming order" rather than an empty result. Its pure ``reorder_by_grade`` is
imported directly.

The prompt is NOT reused. ``reranker`` grades *VC firms* against a *startup
pitch* — its system prompt, its ``firm_slug`` schema key and its
``bundle_text`` (which renders a portfolio) all say so. Here the candidates are
the user's own companies and the query is an acquisition/sourcing criteria
paragraph. Feeding companies through a prompt that calls them firms and asks
about their portfolios would quietly degrade grade quality, so this module
states the real task and keeps the proven control flow.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional, Sequence

from pierre.byodata.schema import UserRow
from pierre.config import GEMINI_MATCHING_MODEL
from pierre.matching.reranker import RerankGrade, reorder_by_grade
from pierre.utils.llm_cost import compute_llm_cost, extract_usage_from_response

logger = logging.getLogger(__name__)

JUDGE_MODEL = GEMINI_MATCHING_MODEL
DEFAULT_BATCH_SIZE = 25
DEFAULT_MAX_RETRIES = 2

SYS_PROMPT = (
    "You grade how well each COMPANY fits a stated SEARCH CRITERIA, for a "
    "professional evaluating a list of companies they already hold.\n\n"
    "Grade each company 0-3:\n"
    "  3 = strong fit — clearly matches the core of the criteria\n"
    "  2 = plausible fit — matches partly, or matches an adjacent space\n"
    "  1 = weak fit — same broad market only\n"
    "  0 = no fit\n\n"
    "Judge ONLY from the text given for each company. Do not use outside "
    "knowledge about these companies, and do not infer facts the text does "
    "not state — if the text is too thin to tell, grade it 1 and say so. "
    "Keep each reason under 20 words, concrete, and grounded in the company's "
    "own text."
)

GRADE_SCHEMA: Dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "row_id": {"type": "string"},
            "grade": {"type": "integer"},
            "reason": {"type": "string"},
        },
        "required": ["row_id", "grade", "reason"],
    },
}


def company_text(row: UserRow) -> str:
    """Pure. Render one row into its prompt block."""
    tags = " ".join(
        f"{label}={value}"
        for label, value in (("sector", row.sector), ("stage", row.stage), ("location", row.geo))
        if value
    )
    head = f"COMPANY row_id={row.row_id} | name={row.name}"
    if tags:
        head += f"\n  {tags}"
    return f"{head}\n  description: {row.description}"


def build_batch_prompt(criteria: str, batch: Sequence[UserRow]) -> str:
    """Pure. Build the user turn for one grading batch."""
    head = (
        f"SEARCH CRITERIA:\n{criteria.strip()}\n\n"
        "Grade EACH company below (return one object per company, echo row_id "
        "exactly):\n\n"
    )
    return head + "\n\n".join(company_text(row) for row in batch)


def _default_client() -> Any:
    import os

    from google import genai

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    return genai.Client(api_key=api_key)


def _grade_batch(
    client: Any,
    model: str,
    criteria: str,
    batch: Sequence[UserRow],
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> "tuple[Dict[str, RerankGrade], float]":
    """Impure. One Gemini call per attempt. Never raises — returns ({}, 0.0)
    after ``max_retries`` failures, having logged a WARNING."""
    from google.genai import types

    prompt = build_batch_prompt(criteria, batch)
    valid_ids = {row.row_id for row in batch}
    last_exc: Optional[Exception] = None
    for _attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYS_PROMPT,
                    temperature=0,
                    response_mime_type="application/json",
                    response_schema=GRADE_SCHEMA,
                    max_output_tokens=8000,
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                ),
            )
            usage = extract_usage_from_response(response)
            cost = compute_llm_cost(model, **usage)
            parsed = json.loads(response.text)
            grades = {
                obj["row_id"]: RerankGrade(grade=int(obj["grade"]), reason=obj.get("reason", ""))
                for obj in parsed
                if obj.get("row_id") in valid_ids
            }
            return grades, cost
        except Exception as exc:  # noqa: BLE001 — any failure retries, then degrades
            last_exc = exc
    logger.warning(
        "byodata judge: batch grading failed after %d attempt(s) for %d row(s): %s",
        max_retries, len(batch), last_exc,
    )
    return {}, 0.0


def grade_rows(
    criteria: str,
    rows: Sequence[UserRow],
    *,
    model: str = JUDGE_MODEL,
    batch_size: int = DEFAULT_BATCH_SIZE,
    client: Any = None,
    client_factory: Callable[[], Any] = _default_client,
) -> "tuple[List[UserRow], Dict[str, RerankGrade], float, bool]":
    """Grade ``rows`` against ``criteria``; return them reordered.

    Returns ``(ordered_rows, grades, cost_usd, degraded)``. Batches run
    sequentially — the caller caps ``rows`` at top-K (default 50), so this is
    2 calls, and sequential keeps the failure story simple.

    Never raises. A client that cannot be constructed, or a batch that fails
    every retry, leaves those rows ungraded, which ``reorder_by_grade`` keeps
    in their incoming (cosine) order. ``degraded`` is True whenever any row
    went ungraded, so the caller can say so rather than present a partly-judged
    list as fully judged.
    """
    if not rows:
        return [], {}, 0.0, False

    if client is None:
        try:
            client = client_factory()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "byodata judge: could not construct Gemini client (%s) — keeping "
                "similarity order for all %d row(s)",
                exc, len(rows),
            )
            return list(rows), {}, 0.0, True

    grades: Dict[str, RerankGrade] = {}
    total_cost = 0.0
    for start in range(0, len(rows), batch_size):
        batch = list(rows[start : start + batch_size])
        batch_grades, batch_cost = _grade_batch(client, model, criteria, batch)
        grades.update(batch_grades)
        total_cost += batch_cost

    by_id = {row.row_id: row for row in rows}
    ordered_ids = reorder_by_grade(
        [row.row_id for row in rows], {rid: g.grade for rid, g in grades.items()}
    )
    ordered = [by_id[rid] for rid in ordered_ids]
    return ordered, grades, total_cost, len(grades) < len(rows)
