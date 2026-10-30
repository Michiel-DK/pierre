"""The BYO-data orchestrator: file bytes + criteria paragraph -> ranked rows.

Two stages, mirroring the retrieve-then-rerank split so cost stays flat as the
user's file grows:

1. **Embed everything, cosine-rank it.** Cheap and linear in row count.
2. **LLM-judge the top-K only** (default 50). A 5,000-row book therefore costs
   the same two Gemini grading calls as a 60-row one.

Nothing here persists: rows are parsed from the uploaded bytes in memory,
ranked, returned, and dropped when the request ends.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from pierre.byodata.parse import parse_upload
from pierre.byodata.schema import (
    UserRow,
    HeaderMapping,
    SkippedRow,
    cosine,
    partition_rows,
    row_document_text,
)

logger = logging.getLogger(__name__)

#: How many cosine-ranked rows get sent to the LLM judge.
DEFAULT_TOP_K: int = 50

#: How many ranked rows come back by default.
DEFAULT_LIMIT: int = 20


@dataclass
class RankedRow:
    row_number: int
    name: str
    description: str
    sector: str = ""
    stage: str = ""
    geo: str = ""
    url: str = ""
    similarity: float = 0.0
    grade: Optional[int] = None
    reason: str = ""


@dataclass
class RankResult:
    """Everything the caller needs to render an honest answer."""

    results: List[RankedRow] = field(default_factory=list)
    #: Data rows in the file as sent, INCLUDING any past the MAX_ROWS cap.
    total_rows: int = 0
    ranked_rows: int = 0
    skipped_rows: int = 0
    #: Rows past the cap that were never read. Non-zero means the answer does
    #: not cover the whole file, and the caller must say so.
    truncated_rows: int = 0
    skipped: List[SkippedRow] = field(default_factory=list)
    unmapped_columns: List[str] = field(default_factory=list)
    mapped_columns: Dict[str, int] = field(default_factory=dict)
    judged: bool = False
    judge_degraded: bool = False
    cost_usd: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "results": [asdict(r) for r in self.results],
            "total_rows": self.total_rows,
            "ranked_rows": self.ranked_rows,
            "skipped_rows": self.skipped_rows,
            "truncated_rows": self.truncated_rows,
            # Cap the echoed skip list — the COUNT is the headline; a 4,000-row
            # skip dump would bury it.
            "skipped": [asdict(s) for s in self.skipped[:50]],
            "unmapped_columns": self.unmapped_columns,
            "mapped_columns": sorted(self.mapped_columns),
            "judged": self.judged,
            "judge_degraded": self.judge_degraded,
            "cost_usd": round(self.cost_usd, 6),
        }


class EmbeddingCountMismatch(RuntimeError):
    """The embedder returned a different number of vectors than rows sent.

    Loud on purpose: the alternative is ``zip`` quietly shortening the ranking
    so rows disappear from the answer while the counts still claim them.
    """


class NoRankableRows(ValueError):
    """Raised when a file parses but yields nothing rankable.

    Carries the diagnosis (missing required columns, or every row too thin) so
    the caller can tell the user which of the two happened — the difference
    between "your header row is unusual" and "your descriptions are empty".
    """

    def __init__(self, message: str, mapping: HeaderMapping, skipped: Sequence[SkippedRow]):
        super().__init__(message)
        self.mapping = mapping
        self.skipped = list(skipped)


def _embedder():
    """Lazy import — keeps ``rank`` importable without google-genai present."""
    from pierre.byodata._embedder import GeminiEmbedder

    return GeminiEmbedder()


def rank_upload(
    filename: str,
    data: bytes,
    criteria: str,
    *,
    top_k: int = DEFAULT_TOP_K,
    limit: int = DEFAULT_LIMIT,
    use_judge: bool = True,
    embedder: Any = None,
    judge: Any = None,
) -> RankResult:
    """Rank the rows of an uploaded file against a criteria paragraph.

    ``embedder`` / ``judge`` are injection seams for tests (and for any future
    caller that wants to supply its own): both default to the real services.
    """
    rows_raw, mapping, truncated = parse_upload(filename, data)
    rankable, skipped = partition_rows(rows_raw)

    if not rankable:
        missing = mapping.missing_required
        if missing:
            message = (
                f"Could not find a {' and '.join(missing)} column. "
                f"Columns seen: {', '.join(mapping.unmapped) or '(none)'}."
            )
        elif skipped:
            message = (
                f"All {len(skipped)} row(s) were skipped — none had enough "
                "description text to rank."
            )
        else:
            message = "The file has no data rows."
        raise NoRankableRows(message, mapping, skipped)

    result = RankResult(
        total_rows=len(rows_raw) + truncated,
        ranked_rows=len(rankable),
        skipped_rows=len(skipped),
        truncated_rows=truncated,
        skipped=skipped,
        unmapped_columns=mapping.unmapped,
        mapped_columns=dict(mapping.columns),
    )
    if truncated:
        logger.warning(
            "byodata: file had %d data rows; ranking the first %d and reporting "
            "%d as truncated",
            result.total_rows, len(rows_raw), truncated,
        )

    service = embedder if embedder is not None else _embedder()
    texts = [row_document_text(r) for r in rankable]
    row_vectors = service.embed_documents(texts)
    if len(row_vectors) != len(texts):
        # zip() would silently truncate the ranking here, dropping rows from
        # the result while ranked_rows still claimed the full count. Same
        # never-silently-drop contract the judge holds.
        raise EmbeddingCountMismatch(
            f"Embedding returned {len(row_vectors)} vector(s) for {len(texts)} row(s)."
        )
    query_vector = service.embed_query(criteria)

    scored = sorted(
        zip(rankable, (cosine(v, query_vector) for v in row_vectors)),
        key=lambda pair: pair[1],
        reverse=True,
    )
    similarity_by_id = {row.row_id: score for row, score in scored}
    shortlist: List[UserRow] = [row for row, _ in scored[:top_k]]

    grades: Dict[str, Any] = {}
    if use_judge and shortlist:
        grade_fn = judge if judge is not None else _default_judge
        shortlist, grades, cost, degraded = grade_fn(criteria, shortlist)
        # ``judged`` means "grades came back", not "we tried". A judge that
        # failed on every batch returns nothing, and reporting that as judged
        # would render as a partial result when the honest word is none.
        result.judged = bool(grades)
        result.judge_degraded = degraded
        result.cost_usd = cost

    for row in shortlist[:limit]:
        grade = grades.get(row.row_id)
        result.results.append(
            RankedRow(
                row_number=row.row_number,
                name=row.name,
                description=row.description,
                sector=row.sector,
                stage=row.stage,
                geo=row.geo,
                url=row.url,
                similarity=round(similarity_by_id.get(row.row_id, 0.0), 4),
                grade=grade.grade if grade else None,
                reason=grade.reason if grade else "",
            )
        )

    return result


def _default_judge(criteria: str, rows: Sequence[UserRow]):
    from pierre.byodata._judge import grade_rows

    return grade_rows(criteria, rows)
