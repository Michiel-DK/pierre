"""File bytes -> canonical rows, for the two formats a user actually sends.

``.csv`` goes through the stdlib path in ``schema``; ``.xlsx`` needs
``openpyxl``, imported lazily inside its own branch so a CSV-only caller (and
the CI-strip unit job) never needs the dependency.

``.xls`` is deliberately unsupported — it needs a separate ``xlrd`` dependency
for a format Excel itself has discouraged since 2007.
"""
from __future__ import annotations

import io
from typing import Dict, List, Tuple

from pierre.byodata.schema import (
    HeaderMapping,
    build_row,
    collapse_whitespace,
    map_headers,
    parse_csv_text,
)

SUPPORTED_EXTENSIONS: Tuple[str, ...] = (".csv", ".xlsx")

#: Hard cap on rows ranked from one upload — above this we would be embedding
#: an unbounded file inside a single request.
#:
#: Overflow is COUNTED and returned as ``truncated_rows``, never dropped in
#: silence: telling a user "5000 rows ranked" when they sent 6000 is the
#: same dishonesty the ``skipped_rows`` report exists to prevent, and it is
#: worse here because they cannot see it from the output.
MAX_ROWS: int = 5000


class UnsupportedFileType(ValueError):
    """Raised for an extension outside ``SUPPORTED_EXTENSIONS``."""


class UnreadableFile(ValueError):
    """Raised when the bytes do not parse as the extension claims."""


def _decode_csv(data: bytes) -> str:
    """Decode CSV bytes, tolerating the BOM Excel writes on 'Save as CSV'."""
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise UnreadableFile("Could not decode the file as text (tried UTF-8 and Latin-1).")


def _parse_xlsx(data: bytes) -> Tuple[List[Tuple[int, Dict[str, str]]], HeaderMapping]:
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise UnreadableFile(
            "Excel support requires the openpyxl package on the server."
        ) from exc

    try:
        workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 — openpyxl raises a zoo of types
        raise UnreadableFile(f"Could not read the .xlsx file: {exc}") from exc

    try:
        sheet = workbook[workbook.sheetnames[0]]
        row_iter = sheet.iter_rows(values_only=True)
        try:
            header = next(row_iter)
        except StopIteration:
            return [], HeaderMapping()

        mapping = map_headers(list(header))
        rows: List[Tuple[int, Dict[str, str]]] = []
        for row_number, cells in enumerate(row_iter, start=1):
            cell_list = list(cells)
            # Blank-check BEFORE any cap so a .csv and the same file saved as
            # .xlsx count identically — the cap applies to data rows in both.
            if not any(collapse_whitespace(c) for c in cell_list):
                continue
            rows.append((row_number, build_row(row_number, cell_list, mapping)))
        return rows, mapping
    finally:
        workbook.close()


def parse_upload(
    filename: str, data: bytes
) -> Tuple[List[Tuple[int, Dict[str, str]]], HeaderMapping, int]:
    """Parse an uploaded ``.csv`` / ``.xlsx`` into canonical rows.

    Returns ``(rows, mapping, truncated_rows)`` where each row is
    ``(row_number, {field: str})`` and ``truncated_rows`` is how many data rows
    past ``MAX_ROWS`` were left unranked — 0 for any normal file.

    Nothing is written to disk: the bytes are parsed in memory and dropped.
    """
    lowered = (filename or "").lower()
    if lowered.endswith(".xlsx"):
        rows, mapping = _parse_xlsx(data)
    elif lowered.endswith(".csv"):
        rows, mapping = parse_csv_text(_decode_csv(data))
    else:
        raise UnsupportedFileType(
            f"Unsupported file type. Send one of: {', '.join(SUPPORTED_EXTENSIONS)}"
        )

    truncated = max(0, len(rows) - MAX_ROWS)
    return rows[:MAX_ROWS], mapping, truncated
