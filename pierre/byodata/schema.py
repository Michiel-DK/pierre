"""Pure schema handling for BYO-data uploads — stdlib only.

CI-strip safe: no pandas, no openpyxl, no google-genai,
no sqlalchemy. Everything here is a data transform over strings and floats.

Two decisions live in this module, both of them the difference between "works
on the user's actual export" and "fails in the meeting":

1. **Header aliasing.** Nobody's spreadsheet uses our column names. A real
   book arrives with ``Company`` / ``Organisation`` / ``Blurb`` / ``What they
   do``. ``map_headers`` resolves those to our canonical fields with a static
   alias table — deliberately NOT an LLM call, so it is free, deterministic
   and testable.

2. **Thin rows are excluded and REPORTED, never silently ranked.** A row with
   a blank or one-word description embeds to noise, and cosine will happily
   rank that noise somewhere plausible. Ranking it anyway repeats a failure
   seen in the source system's profiler (silently filled defaults) — a
   confident answer built on a fabricated input. ``partition_rows`` splits them out
   with a reason so the caller can surface the count.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

#: Canonical field names. ``name`` + ``description`` are required for a row to
#: be rankable; the rest are optional colour that ride along into the judge.
CANONICAL_FIELDS: Tuple[str, ...] = ("name", "description", "sector", "stage", "geo", "url")

#: Minimum description length (characters, after whitespace collapse) for a row
#: to be embedded. Below this there is not enough signal to rank honestly.
MIN_DESCRIPTION_CHARS: int = 25

#: Header aliases -> canonical field. Keys are compared after
#: ``_normalise_header`` (lowercase, non-alphanumerics collapsed to single
#: spaces), so "Company Name", "company_name" and "COMPANY-NAME" all match the
#: single entry "company name".
_HEADER_ALIASES: Dict[str, str] = {
    # name
    "name": "name",
    "company": "name",
    "company name": "name",
    "organisation": "name",
    "organization": "name",
    "org": "name",
    "account": "name",
    "account name": "name",
    "business": "name",
    "business name": "name",
    "startup": "name",
    "target": "name",
    "target name": "name",
    "title": "name",
    # description
    "description": "description",
    "desc": "description",
    "about": "description",
    "blurb": "description",
    "summary": "description",
    "overview": "description",
    "what they do": "description",
    "business description": "description",
    "company description": "description",
    "one liner": "description",
    "oneliner": "description",
    "pitch": "description",
    "notes": "description",
    "details": "description",
    # sector
    "sector": "sector",
    "sectors": "sector",
    "industry": "sector",
    "industries": "sector",
    "vertical": "sector",
    "category": "sector",
    "market": "sector",
    "segment": "sector",
    # stage
    "stage": "stage",
    "stages": "stage",
    "round": "stage",
    "funding stage": "stage",
    "investment stage": "stage",
    "maturity": "stage",
    # geo
    "geo": "geo",
    "geography": "geo",
    "country": "geo",
    "region": "geo",
    "location": "geo",
    "hq": "geo",
    "headquarters": "geo",
    "city": "geo",
    "based in": "geo",
    # url
    "url": "url",
    "website": "url",
    "web": "url",
    "site": "url",
    "domain": "url",
    "homepage": "url",
    "link": "url",
}

_WS_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class UserRow:
    """One rankable row from the user's file.

    ``row_number`` is the 1-based line in the source file (header excluded), so
    a caller can point the user at the offending row of their own export.
    """

    row_number: int
    name: str
    description: str
    sector: str = ""
    stage: str = ""
    geo: str = ""
    url: str = ""

    @property
    def row_id(self) -> str:
        """Stable per-request id. Row number, not name — names repeat."""
        return f"r{self.row_number}"


@dataclass(frozen=True)
class SkippedRow:
    """A row we refused to rank, with the reason to show the user."""

    row_number: int
    name: str
    reason: str


@dataclass
class HeaderMapping:
    """Result of resolving a file's header row against ``CANONICAL_FIELDS``."""

    #: canonical field -> column index in the source row
    columns: Dict[str, int] = field(default_factory=dict)
    #: source headers we could not place (shown back to the user)
    unmapped: List[str] = field(default_factory=list)

    @property
    def missing_required(self) -> List[str]:
        return [f for f in ("name", "description") if f not in self.columns]


def _normalise_header(raw: Any) -> str:
    """Lowercase, strip, collapse every non-alphanumeric run to one space."""
    text = str(raw or "").strip().lower()
    text = _NON_ALNUM_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def collapse_whitespace(raw: Any) -> str:
    """Cell value -> single-spaced trimmed string. ``None`` becomes ""."""
    if raw is None:
        return ""
    return _WS_RE.sub(" ", str(raw)).strip()


def map_headers(headers: Sequence[Any]) -> HeaderMapping:
    """Resolve a header row to canonical fields.

    First match wins, so a file carrying both ``Description`` and ``Notes``
    keeps the earlier (more specific) column and reports the later one as
    unmapped rather than overwriting.
    """
    mapping = HeaderMapping()
    for index, raw in enumerate(headers):
        normalised = _normalise_header(raw)
        if not normalised:
            continue
        canonical = _HEADER_ALIASES.get(normalised)
        if canonical and canonical not in mapping.columns:
            mapping.columns[canonical] = index
        else:
            mapping.unmapped.append(str(raw).strip())
    return mapping


def build_row(row_number: int, cells: Sequence[Any], mapping: HeaderMapping) -> Dict[str, str]:
    """Project one source row onto canonical fields. Missing cells -> ""."""
    out: Dict[str, str] = {}
    for canonical, index in mapping.columns.items():
        out[canonical] = collapse_whitespace(cells[index]) if index < len(cells) else ""
    return out


def partition_rows(
    raw_rows: Iterable[Tuple[int, Mapping[str, str]]],
    *,
    min_description_chars: int = MIN_DESCRIPTION_CHARS,
) -> Tuple[List[UserRow], List[SkippedRow]]:
    """Split canonical rows into rankable and skipped-with-a-reason.

    Skip reasons, in precedence order:

    - ``missing name``          — nothing to show the user in the result
    - ``no description``        — blank description cell
    - ``description too short`` — under ``min_description_chars``; embedding
      this produces noise, and ranking noise is the failure mode above

    Returning the skipped rows (rather than dropping them) is the point: the
    count is the most useful thing we can tell a prospect about their own data.
    """
    rankable: List[UserRow] = []
    skipped: List[SkippedRow] = []

    for row_number, values in raw_rows:
        name = collapse_whitespace(values.get("name"))
        description = collapse_whitespace(values.get("description"))

        if not name:
            skipped.append(SkippedRow(row_number, name or "(unnamed)", "missing name"))
            continue
        if not description:
            skipped.append(SkippedRow(row_number, name, "no description"))
            continue
        if len(description) < min_description_chars:
            skipped.append(
                SkippedRow(
                    row_number,
                    name,
                    f"description too short (<{min_description_chars} chars)",
                )
            )
            continue

        rankable.append(
            UserRow(
                row_number=row_number,
                name=name,
                description=description,
                sector=collapse_whitespace(values.get("sector")),
                stage=collapse_whitespace(values.get("stage")),
                geo=collapse_whitespace(values.get("geo")),
                url=collapse_whitespace(values.get("url")),
            )
        )

    return rankable, skipped


def row_document_text(row: UserRow) -> str:
    """Render one row into the text we embed.

    Optional fields are appended only when present — an empty ``sector=`` tail
    on every row is uniform noise that shifts all vectors the same way.
    """
    parts = [f"{row.name}: {row.description}"]
    for label, value in (("Sector", row.sector), ("Stage", row.stage), ("Location", row.geo)):
        if value:
            parts.append(f"{label}: {value}")
    return " | ".join(parts)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity. Zero-magnitude vectors score 0.0, never NaN.

    ``EmbeddingService`` already returns normalised vectors, for which this is
    a plain dot product — but it is cheap to stay correct for unnormalised
    input rather than depend on a caller's invariant.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(y * y for y in b))
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)


def parse_csv_text(text: str) -> Tuple[List[Tuple[int, Dict[str, str]]], HeaderMapping]:
    """Parse CSV *text* into canonical rows. Stdlib ``csv`` — no pandas.

    Lives here rather than in ``parse`` precisely because it needs nothing
    beyond the standard library, which keeps the CSV path importable under
    CI-strip.
    """
    import csv
    import io

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return [], HeaderMapping()

    mapping = map_headers(header)
    rows: List[Tuple[int, Dict[str, str]]] = []
    for row_number, cells in enumerate(reader, start=1):
        if not any(collapse_whitespace(c) for c in cells):
            continue  # wholly blank line — not worth reporting as skipped
        rows.append((row_number, build_row(row_number, cells, mapping)))
    return rows, mapping
