"""LLM listwise fit-judge reranker.

Promotes a fit-judge prototype (the same judge that produced the source
system's retrieval-eval labels — see the circularity caveat below) into an
OPTIONAL rerank stage over a matcher's candidate pool. Earlier experiments
in the source system established that the judge reads REAL portfolio-company
evidence (not just a firm's self-described tags) and grades relevance 0-3
from the founder's point of view; that the ceiling is a RANKING problem, not
a candidate-pool-size problem; and that lexical hits carry real signal for a
DOWNSTREAM consumer even though rank-fusing them regresses raw retrieval —
the right use is lexical-as-extra-candidates, which the caller implements by
feeding an expanded candidate window in, not by changing anything in this
module.

Two-layer split (the unit tests must run without google-genai or database
drivers on the path):

  - ``reorder_by_grade`` — PURE. No imports beyond stdlib + dataclasses.
    Stable-sorts a candidate-id list by an externally-supplied grade map.
    This is the only part with a dedicated CI-strip-safe unit test.
  - ``grade_candidates`` — IMPURE. The only function in this module that
    imports ``google.genai`` (lazily, inside the function body) or reads
    GEMINI_API_KEY/GOOGLE_API_KEY. Threads a client in (mirrors the
    ``pierre/agents/_claim_auditor.py`` "caller threads the client" pattern)
    so tests can inject a fake client for fault-injection coverage.

Fallback contract (never a silent empty result):
when grading a candidate fails (single batch after 3 retries, or the
Gemini client can't even be constructed), that candidate is simply absent
from the ``grades`` dict. ``reorder_by_grade`` treats an absent grade as
the lowest-priority sentinel value and Python's sort is stable, so
ungraded candidates keep their ORIGINAL RELATIVE ORDER — the pre-rerank
order handed in by the caller (in the source system, the fully-boosted/
scored pipeline order, NOT raw cosine). If EVERY batch fails, every candidate is ungraded, every candidate
ties at the sentinel, and the output is byte-identical to the input: the
"total grader failure" fallback falls out of the same code path as
"partial batch failure" with no special-casing required. A WARNING is
logged in both cases so a dead grader is never silently invisible.

CIRCULARITY CAVEAT (surfaced in a labeling review — read before trusting
a harness number): the source system's retrieval-eval labels were
themselves produced by this same fit-judge (same model, same prompt shape,
temperature 0). Reranking with THIS judge and then scoring against
labels THIS judge produced is partially circular — a firm the judge once
called "relevant" is likely to be judged "relevant" again at rerank time,
independent of whether that's true for a real founder. nDCG@25 is the more
circular of the two harness metrics (it's graded directly against the
judge's own grade scale); recall@50 is less circular (grades collapse to a
binary relevant/not cutoff) but not immune. Treat any harness improvement
as "the fit-judge is self-consistent at rerank time" rather than "retrieval
quality improved for a real founder" until an independent (non-judge)
labeling pass exists.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from pierre.config import GEMINI_MATCHING_MODEL
from pierre.utils.llm_cost import compute_llm_cost, extract_usage_from_response

logger = logging.getLogger(__name__)

# Matching/reports use case (no hardcoded model literals outside
# pierre/config.py).
RERANK_MODEL = GEMINI_MATCHING_MODEL
# 12 (not the prototype's 6): cost is dominated by OUTPUT tokens (the
# per-firm "reason" string), and every batch re-pays the pitch-description +
# system-instruction input overhead once. Doubling the batch size roughly
# halves the number of batches — and therefore that repeated overhead — for
# a given pool size, without materially hurting listwise-grading quality at
# this batch scale (per a cost diagnosis on the source system).
DEFAULT_BATCH_SIZE = 12
DEFAULT_MAX_WORKERS = 8
DEFAULT_MAX_RETRIES = 3

# Candidates with no grade entry sort strictly below every real grade
# (0-3) but tie among themselves — Python's stable sort then preserves
# their original relative order (see reorder_by_grade docstring).
UNGRADED_SENTINEL = -1

SYS_PROMPT = (
    "You grade how relevant a VC firm is to a specific startup, from the FOUNDER's view: "
    "would this founder actually raise from this firm? Relevance = the firm demonstrably invests in the "
    "startup's SECTOR/DOMAIN (judge from its ACTUAL portfolio companies, not just its self-described tags), "
    "AND a compatible stage, AND a compatible geography.\n"
    "Grades:\n"
    "3 = best-fit: portfolio shows REPEATED investment in this exact domain; stage & geo both fit. Reserve for clear specialists.\n"
    "2 = clearly relevant: invests in this domain (some portfolio evidence), stage/geo fit or minor stretch.\n"
    "1 = marginal: only tangential/one-off exposure, OR a real stage/geo mismatch despite domain fit.\n"
    "0 = not relevant: no genuine domain evidence in portfolio (a broad generalist that lists the tag but shows no real presence = 0), or wrong geo/stage entirely.\n"
    "Be strict and use the full range: most firms in a broad pool are 0 or 1. Tags are cheap; portfolio evidence is decisive.\n"
    "'reason' MUST be terse: 6 words or fewer, no full sentences (e.g. "
    "'repeated fintech deals, seed, EU fit'). Output tokens are billed ~8x "
    "input tokens on this model — a long reason is the dominant cost driver "
    "of this call, so keep it to the minimum that's still useful for a "
    "human spot-checking the grade."
)

GRADE_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "firm_slug": {"type": "string"},
            "grade": {"type": "integer"},
            "reason": {"type": "string"},
        },
        "required": ["firm_slug", "grade", "reason"],
    },
}


# ---------------------------------------------------------------------------
# Pure types + pure reorder logic — CI-strip safe, unit-tested in isolation.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RerankGrade:
    grade: int
    reason: str = ""


@dataclass
class RerankResult:
    """Result of a full grade_candidates() call.

    ``candidate_ids`` is the reordered id list (same members as the input,
    just reordered — never a subset, per the loud-failure contract).
    """

    candidate_ids: List[str]
    grades: Dict[str, RerankGrade] = field(default_factory=dict)
    n_requested: int = 0
    n_graded: int = 0
    cost_usd: float = 0.0
    fallback_used: bool = False
    error: Optional[str] = None


def reorder_by_grade(candidate_ids: Sequence[str], grades: Dict[str, int]) -> List[str]:
    """Pure. Stable-sort ``candidate_ids`` by ``grades.get(id)`` descending.

    Candidates absent from ``grades`` sort at ``UNGRADED_SENTINEL`` (below
    every real 0-3 grade). Because Python's ``sort`` is stable and we
    tie-break explicitly on original index, candidates that tie on grade
    (including ALL of them tying at the sentinel, i.e. an empty ``grades``
    dict) keep their ORIGINAL RELATIVE ORDER. This is the single mechanism
    behind both:

      - "grader failed completely" -> grades={} -> output == input order
        (the documented fallback-to-incoming-order contract), and
      - "grader graded some, missed others" -> ungraded ones keep their
        spot relative to each other, graded ones float per their grade.

    No exceptions raised — pure data transform, no IO.
    """
    indexed = list(enumerate(candidate_ids))
    indexed.sort(key=lambda pair: (-grades.get(pair[1], UNGRADED_SENTINEL), pair[0]))
    return [cid for _, cid in indexed]


def bundle_text(bundle: Dict[str, Any]) -> str:
    """Pure. Render one candidate bundle dict into the judge's prompt block.

    ``bundle`` shape (built by the caller):
        {"firm_id": str, "name": str, "focus": [...], "industries": [...],
         "stages": [...], "geographies": [...],
         "portfolio": [{"name": str, "sector": str, "description": str}, ...]}
    """
    cos = bundle.get("portfolio") or []
    cos_text = "; ".join(
        f"{c.get('name', '')} ({c.get('sector', '')}): {c.get('description', '')}".strip()
        for c in cos[:15]
    ) or "(no portfolio companies on file)"
    return (
        f"FIRM slug={bundle.get('firm_id', '')} | name={bundle.get('name', '')}\n"
        f"  self-tags focus={list(bundle.get('focus') or [])[:8]} industries={list(bundle.get('industries') or [])[:8]}\n"
        f"  stages={bundle.get('stages') or []} geographies={bundle.get('geographies') or []}\n"
        f"  portfolio: {cos_text}"
    )


def build_batch_prompt(
    pitch_description: str, project_profile: Optional[Dict[str, Any]], batch: Sequence[Dict[str, Any]]
) -> str:
    """Pure. Build the user-turn text for one grading batch."""
    profile = project_profile or {}
    head = (
        f"STARTUP PITCH:\n{pitch_description}\n"
        f"sectors={profile.get('sectors')} stage={profile.get('stages')} geography={profile.get('geography')}\n\n"
        f"Grade EACH firm below (return one object per firm, echo firm_slug exactly):\n\n"
    )
    return head + "\n\n".join(bundle_text(b) for b in batch)


# ---------------------------------------------------------------------------
# Impure grading — the only part of this module that touches google.genai.
# ---------------------------------------------------------------------------


def _default_client():
    import os

    from google import genai

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    return genai.Client(api_key=api_key)


def _grade_batch(
    client: Any,
    model: str,
    pitch_description: str,
    project_profile: Optional[Dict[str, Any]],
    batch: Sequence[Dict[str, Any]],
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> "tuple[Dict[str, RerankGrade], float]":
    """Impure. Grade ONE batch via a single Gemini call, retrying up to
    ``max_retries`` times on any exception (malformed JSON, transient API
    error, schema mismatch). On final failure returns ({}, 0.0) — the
    caller logs the WARNING; this function never raises.
    """
    from google.genai import types

    prompt = build_batch_prompt(pitch_description, project_profile, batch)
    valid_slugs = {b.get("firm_id") for b in batch}
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
            arr = json.loads(response.text)
            grades = {
                obj["firm_slug"]: RerankGrade(grade=int(obj["grade"]), reason=obj.get("reason", ""))
                for obj in arr
                if obj.get("firm_slug") in valid_slugs
            }
            return grades, cost
        except Exception as exc:  # noqa: BLE001 — any failure retries, then falls back
            last_exc = exc
    logger.warning(
        "reranker: batch grading failed after %d attempt(s) for %d firm(s): %s",
        max_retries, len(batch), last_exc,
    )
    return {}, 0.0


def grade_candidates(
    pitch_description: str,
    project_profile: Optional[Dict[str, Any]],
    candidate_bundles: Sequence[Dict[str, Any]],
    *,
    model: str = RERANK_MODEL,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_workers: int = DEFAULT_MAX_WORKERS,
    client: Any = None,
    client_factory: Callable[[], Any] = _default_client,
) -> RerankResult:
    """Impure orchestrator. Grades ``candidate_bundles`` in concurrent
    batches of ``batch_size`` via ``client`` (or one built by
    ``client_factory`` if ``client`` is None — the seam fault-injection
    tests use to hand in a raising fake), then reorders by
    ``reorder_by_grade``.

    Never raises: client-construction failure and any per-batch failure
    both degrade to "that candidate is ungraded" -> falls back to the
    incoming order for the affected candidates (see reorder_by_grade). A
    WARNING is always logged when any degradation occurs.
    """
    candidate_ids = [b.get("firm_id") for b in candidate_bundles]
    if not candidate_bundles:
        return RerankResult(candidate_ids=[], n_requested=0, n_graded=0)

    if client is None:
        try:
            client = client_factory()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "reranker: could not construct Gemini client (%s) — falling back to "
                "incoming (pre-rerank) order for all %d candidates",
                exc, len(candidate_ids),
            )
            return RerankResult(
                candidate_ids=list(candidate_ids),
                n_requested=len(candidate_ids),
                n_graded=0,
                cost_usd=0.0,
                fallback_used=True,
                error=str(exc),
            )

    import concurrent.futures as cf

    batches = [
        list(candidate_bundles[i : i + batch_size])
        for i in range(0, len(candidate_bundles), batch_size)
    ]

    grades: Dict[str, RerankGrade] = {}
    total_cost = 0.0
    n_batch_failures = 0
    with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {
            ex.submit(_grade_batch, client, model, pitch_description, project_profile, batch): batch
            for batch in batches
        }
        for fut in cf.as_completed(futs):
            batch = futs[fut]
            try:
                batch_grades, batch_cost = fut.result()
            except Exception as exc:  # noqa: BLE001 — defensive; _grade_batch shouldn't raise
                logger.warning(
                    "reranker: unexpected error grading batch of %d firm(s): %s", len(batch), exc
                )
                batch_grades, batch_cost = {}, 0.0
            if not batch_grades:
                n_batch_failures += 1
            grades.update(batch_grades)
            total_cost += batch_cost

    fallback_used = len(grades) == 0
    if fallback_used:
        logger.warning(
            "reranker: ALL %d batch(es) failed to grade — falling back to incoming "
            "(pre-rerank) order for all %d candidates",
            len(batches), len(candidate_ids),
        )
    elif n_batch_failures:
        logger.warning(
            "reranker: %d/%d batch(es) failed — %d candidate(s) ungraded, kept at "
            "their incoming relative position",
            n_batch_failures, len(batches), len(candidate_ids) - len(grades),
        )

    grade_ints = {fid: g.grade for fid, g in grades.items()}
    reordered = reorder_by_grade(candidate_ids, grade_ints)
    return RerankResult(
        candidate_ids=reordered,
        grades=grades,
        n_requested=len(candidate_ids),
        n_graded=len(grades),
        cost_usd=total_cost,
        fallback_used=fallback_used,
    )
