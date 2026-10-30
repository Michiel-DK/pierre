"""BYO-data demo — rank a company list against a criteria paragraph.

The first demo because it needs no database and no seed: the file is parsed,
ranked and printed in memory (``pierre.byodata`` never persists anything).

    python scripts/demo/byodata_demo.py                  # bundled sample CSV
    python scripts/demo/byodata_demo.py my_list.xlsx --criteria "..."

Embeddings:
- ``GOOGLE_API_KEY`` set (and not ``dummy``): the real Gemini embedder, plus the
  LLM fit-judge on the shortlist unless ``--no-judge``.
- otherwise, or with ``--offline``: a deterministic bag-of-words hashing
  embedder (stdlib only, no network). Rankings are lexical, not semantic —
  good enough to show the flow, not a quality claim.

The bundled ``data/demo/sample_companies.csv`` is fictional.
"""
from __future__ import annotations

import argparse
import hashlib
import math
import os
import re
import sys
from pathlib import Path
from typing import List, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pierre.byodata.rank import NoRankableRows, RankResult, rank_upload  # noqa: E402

DEFAULT_FILE = REPO_ROOT / "data" / "demo" / "sample_companies.csv"
DEFAULT_CRITERIA = (
    "Climate software or materials companies that cut carbon emissions or "
    "energy use, at seed or Series A, based in Europe."
)

_TOKEN = re.compile(r"[a-z0-9]+")
_DIM = 512


class HashingEmbedder:
    """Deterministic offline stand-in for ``EmbeddingService``.

    Each token is hashed into one of ``dim`` buckets; the vector is the
    L2-normalised bucket count. Same text in, same vector out, on any machine.
    """

    def __init__(self, dim: int = _DIM):
        self.dim = dim

    def _embed(self, text: str) -> List[float]:
        vec = [0.0] * self.dim
        for token in _TOKEN.findall(text.lower()):
            if len(token) < 3:
                continue
            bucket = int(hashlib.sha256(token.encode()).hexdigest(), 16) % self.dim
            vec[bucket] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def embed_documents(self, texts: Sequence[str]) -> List[List[float]]:
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> List[float]:
        return self._embed(text)


def use_live_embedder(offline: bool) -> bool:
    key = os.environ.get("GOOGLE_API_KEY", "")
    return not offline and bool(key) and key != "dummy"


def run(path: Path, criteria: str, *, offline: bool, use_judge: bool, limit: int) -> RankResult:
    live = use_live_embedder(offline)
    return rank_upload(
        path.name,
        path.read_bytes(),
        criteria,
        limit=limit,
        use_judge=use_judge and live,
        embedder=None if live else HashingEmbedder(),
    )


def format_result(result: RankResult, *, live: bool) -> str:
    lines = [
        f"Embedder: {'Gemini (live)' if live else 'offline hashing (lexical, deterministic)'}",
        f"Rows: {result.total_rows} in file, {result.ranked_rows} ranked, "
        f"{result.skipped_rows} skipped (too little description), "
        f"{result.truncated_rows} truncated",
        f"Judged by LLM: {'yes' if result.judged else 'no'}",
        "",
        f"{'#':>2}  {'similarity':>10}  {'grade':>5}  name — sector, stage, geo",
    ]
    for i, row in enumerate(result.results, 1):
        grade = "" if row.grade is None else str(row.grade)
        lines.append(
            f"{i:>2}  {row.similarity:>10.4f}  {grade:>5}  "
            f"{row.name} — {row.sector}, {row.stage}, {row.geo}"
        )
    for skipped in result.skipped:
        lines.append(f"skipped row {skipped.row_number}: {skipped.reason}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file", nargs="?", type=Path, default=DEFAULT_FILE)
    parser.add_argument("--criteria", default=DEFAULT_CRITERIA)
    parser.add_argument("--offline", action="store_true", help="force the hashing embedder")
    parser.add_argument("--no-judge", action="store_true", help="skip the LLM fit-judge")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args(argv)

    print(f"Criteria: {args.criteria}\nFile: {args.file}\n")
    offline = args.offline
    try:
        result = run(args.file, args.criteria, offline=offline,
                     use_judge=not args.no_judge, limit=args.limit)
    except NoRankableRows as exc:
        print(f"Nothing to rank: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        if not use_live_embedder(offline):
            raise
        # A placeholder or invalid key: rank the same file offline instead.
        print(f"Live Gemini call failed ({type(exc).__name__}: {exc}); "
              "falling back to the offline embedder.\n", file=sys.stderr)
        offline = True
        result = run(args.file, args.criteria, offline=offline,
                     use_judge=False, limit=args.limit)
    print(format_result(result, live=use_live_embedder(offline)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
