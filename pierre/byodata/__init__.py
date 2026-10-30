"""BYO-data ingest — rank a USER's own company list against a criteria paragraph.

The honest slice of "run the matcher on a user's own data" that is buildable
without a committed user: upload a spreadsheet, describe what you're looking for, get
their rows back best-fit first.

**Ephemeral by construction.** Nothing here writes to any table and nothing
touches the filesystem — rows live in memory for the duration of one request
and are discarded. That is what keeps multi-tenant isolation out of scope:
there is no stored user data to wall off.

Module layout mirrors the CI-strip split (pure modules import without the
heavy dependencies, so unit tests run in a minimal environment):

- ``schema``  — PURE stdlib. Header aliasing, row normalisation, the
  thin-row partition, cosine. Imports cleanly with pandas/openpyxl/
  google-genai/sqlalchemy all absent.
- ``parse``   — file bytes -> rows. ``openpyxl`` imported lazily inside the
  ``.xlsx`` branch only; ``.csv`` goes through stdlib ``csv``.
- ``rank``    — the orchestrator: embed -> cosine top-K -> optional judge.
- ``_judge``  — the LLM fit-judge. Same never-raises / fall-back-to-incoming
  -order contract as ``pierre/matching/reranker.py``, whose pure
  ``reorder_by_grade`` it reuses.
"""
from __future__ import annotations

from pierre.byodata.schema import (
    CANONICAL_FIELDS,
    MIN_DESCRIPTION_CHARS,
    UserRow,
    SkippedRow,
    cosine,
    map_headers,
    partition_rows,
    row_document_text,
)

__all__ = [
    "CANONICAL_FIELDS",
    "MIN_DESCRIPTION_CHARS",
    "UserRow",
    "SkippedRow",
    "cosine",
    "map_headers",
    "partition_rows",
    "row_document_text",
]
