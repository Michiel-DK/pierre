"""Shared groundedness → confidence-band mapping.

Single source of truth for the coarse ``high`` / ``medium`` / ``low``
grounding band, used by BOTH:

- the source system's chat-answer pipeline, and
- the report claim-auditor (``pierre/agents/_claim_auditor.report_grounding_band``).

It lives in a neutral package so both callers reuse one function instead of
forking it: chat answers and deep reports speak ONE grounding-level language
with one threshold set.

**CI-strip-safe**: imports ONLY the standard library + ``typing``. Must stay
importable with database drivers and ``google.genai`` absent.

Confidence is GROUNDEDNESS, never routing cosine, never
ROUGE/BLEU n-gram overlap. A groundedness judge decomposes the answer into
atomic claims and verifies each is entailed by the retrieved chunks; the
fraction of supported claims maps to a coarse band:

    fraction == 1.0           → "high"   (every claim is grounded)
    HIGH > fraction >= MEDIUM → "medium" (most claims grounded)
    fraction <  MEDIUM        → "low"    (weakly / partly unsupported)

The band is deliberately coarse — a raw 0–1 number is false precision.
``None`` means "no confidence judgement available"
(judge disabled, errored, or there was no grounded answer to score).

Honest boundary: groundedness != correctness. The band measures
"stuck to the sources," not "the sources are right."
"""
from __future__ import annotations

from typing import Optional

# Band thresholds, as a fraction of claims entailed by the
# retrieved chunks. Tune on demo traffic; start values per the RAGAS-
# faithfulness shape (only a fully-supported answer earns "high").
HIGH_BAND = 1.0  # every claim grounded → "high"
MEDIUM_BAND = 0.5  # >= half the claims grounded → "medium" (else "low")


def groundedness_to_band(supported: int, total: int) -> Optional[str]:
    """Map a (supported, total) claim count to a coarse confidence band.

    ``supported`` = number of claims entailed by the available evidence.
    ``total``     = number of claims the judge decomposed the answer into.

    Returns ``"high" | "medium" | "low"`` for a valid judgement, or ``None``
    when no band can be derived:

    * ``total <= 0`` — no checkable claims (e.g. a pure no-answer / hedge
      sentence, or — on the report side under Policy B — a report with zero
      ``verified`` and zero ``fabricated`` claims); there is nothing to
      score → ``None``.
    * ``supported`` outside ``[0, total]`` — a malformed result; refuse to
      fabricate a band rather than clamp silently → ``None``.
    """
    if total <= 0:
        return None
    if supported < 0 or supported > total:
        return None

    fraction = supported / total
    if fraction >= HIGH_BAND:
        return "high"
    if fraction >= MEDIUM_BAND:
        return "medium"
    return "low"
