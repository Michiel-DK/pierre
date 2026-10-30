"""Unit tests for pierre/byodata/schema.py.

CI-strip-safe: everything imported here reaches only
stdlib. ``pierre.byodata.schema`` imports ``math``/``re``/``csv`` and nothing
else, so this module loads with psycopg2, sqlalchemy, pandas, openpyxl and
google-genai all absent.

The judge's prompt builders are covered in ``tests/unit/test_byodata_rank.py``
instead, because ``pierre.byodata._judge`` imports ``pierre.config``.
"""
from __future__ import annotations

import pytest

from pierre.byodata.schema import (
    MIN_DESCRIPTION_CHARS,
    UserRow,
    build_row,
    collapse_whitespace,
    cosine,
    map_headers,
    parse_csv_text,
    partition_rows,
    row_document_text,
)


class TestMapHeaders:
    """The alias table is what makes a real user export work at all."""

    def test_canonical_headers_map_to_themselves(self):
        mapping = map_headers(["name", "description", "sector", "stage", "geo", "url"])
        assert mapping.columns == {
            "name": 0, "description": 1, "sector": 2, "stage": 3, "geo": 4, "url": 5
        }
        assert mapping.unmapped == []
        assert mapping.missing_required == []

    @pytest.mark.parametrize(
        "header,expected",
        [
            ("Company", "name"),
            ("COMPANY NAME", "name"),
            ("company_name", "name"),
            ("Organisation", "name"),
            ("Account Name", "name"),
            ("Blurb", "description"),
            ("What They Do", "description"),
            ("Business Description", "description"),
            ("Industry", "sector"),
            ("Vertical", "sector"),
            ("Funding Stage", "stage"),
            ("HQ", "geo"),
            ("Country", "geo"),
            ("Website", "url"),
            ("Domain", "url"),
        ],
    )
    def test_real_world_aliases(self, header, expected):
        assert map_headers([header]).columns == {expected: 0}

    def test_punctuation_and_case_are_normalised(self):
        # "Company-Name!" and "company name" must land on the same key.
        assert map_headers(["Company-Name!"]).columns == {"name": 0}

    def test_first_match_wins_and_loser_is_reported(self):
        # A file with both Description and Notes keeps the specific one and
        # reports the other rather than silently overwriting it.
        mapping = map_headers(["Company", "Description", "Notes"])
        assert mapping.columns == {"name": 0, "description": 1}
        assert mapping.unmapped == ["Notes"]

    def test_unknown_headers_are_reported_not_dropped(self):
        mapping = map_headers(["Company", "Description", "Deal Owner", "ARR"])
        assert mapping.unmapped == ["Deal Owner", "ARR"]

    def test_missing_required_is_diagnosable(self):
        assert map_headers(["Company", "Sector"]).missing_required == ["description"]
        assert map_headers(["Sector"]).missing_required == ["name", "description"]

    def test_blank_header_cells_are_ignored(self):
        mapping = map_headers(["Company", None, "", "   ", "About"])
        assert mapping.columns == {"name": 0, "description": 4}


class TestPartitionRows:
    """Thin rows must be excluded AND reported — never silently ranked."""

    def _rows(self, *values):
        return [(i + 1, v) for i, v in enumerate(values)]

    def test_good_row_is_rankable(self):
        rankable, skipped = partition_rows(
            self._rows({"name": "Acme", "description": "A" * (MIN_DESCRIPTION_CHARS + 5)})
        )
        assert len(rankable) == 1 and skipped == []
        assert rankable[0].row_number == 1 and rankable[0].row_id == "r1"

    def test_blank_description_is_skipped_with_reason(self):
        rankable, skipped = partition_rows(self._rows({"name": "Acme", "description": ""}))
        assert rankable == []
        assert skipped[0].reason == "no description"
        assert skipped[0].name == "Acme"

    def test_short_description_is_skipped_not_ranked(self):
        rankable, skipped = partition_rows(self._rows({"name": "Acme", "description": "logistics"}))
        assert rankable == []
        assert "too short" in skipped[0].reason

    def test_missing_name_is_skipped(self):
        rankable, skipped = partition_rows(
            self._rows({"name": "", "description": "B" * 60})
        )
        assert rankable == []
        assert skipped[0].reason == "missing name"

    def test_row_numbers_survive_partitioning(self):
        # The user needs to find the offending line in THEIR file, so the
        # numbering must not be re-based after skips.
        rankable, skipped = partition_rows(
            self._rows(
                {"name": "Good", "description": "G" * 40},
                {"name": "Thin", "description": "x"},
                {"name": "Also good", "description": "H" * 40},
            )
        )
        assert [r.row_number for r in rankable] == [1, 3]
        assert [s.row_number for s in skipped] == [2]

    def test_threshold_is_configurable(self):
        rankable, _ = partition_rows(
            self._rows({"name": "Acme", "description": "short one"}),
            min_description_chars=5,
        )
        assert len(rankable) == 1

    def test_optional_fields_default_to_empty_not_none(self):
        rankable, _ = partition_rows(self._rows({"name": "Acme", "description": "D" * 40}))
        assert rankable[0].sector == "" and rankable[0].geo == ""


class TestCollapseWhitespaceAndRowText:
    def test_collapse_handles_none_and_newlines(self):
        assert collapse_whitespace(None) == ""
        assert collapse_whitespace("  a\n\n  b\t c ") == "a b c"

    def test_numeric_cells_become_strings(self):
        # openpyxl hands back ints/floats/datetimes, not strings.
        assert collapse_whitespace(2026) == "2026"

    def test_document_text_omits_empty_optional_fields(self):
        row = UserRow(1, "Acme", "Ships things", sector="Logistics")
        text = row_document_text(row)
        assert text == "Acme: Ships things | Sector: Logistics"
        assert "Stage" not in text and "Location" not in text

    def test_document_text_includes_all_present_fields(self):
        row = UserRow(1, "Acme", "Ships things", sector="Logistics", stage="Series A", geo="NL")
        assert row_document_text(row) == (
            "Acme: Ships things | Sector: Logistics | Stage: Series A | Location: NL"
        )


class TestCosine:
    def test_identical_vectors_score_one(self):
        assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)

    def test_orthogonal_vectors_score_zero(self):
        assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_zero_magnitude_returns_zero_not_nan(self):
        # A zero vector must not produce NaN — NaN sorts unpredictably and
        # would scramble the ranking silently.
        assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0

    def test_length_mismatch_and_empty_return_zero(self):
        assert cosine([1.0], [1.0, 2.0]) == 0.0
        assert cosine([], [1.0]) == 0.0

    def test_unnormalised_vectors_still_correct(self):
        assert cosine([3.0, 0.0], [7.0, 0.0]) == pytest.approx(1.0)


class TestParseCsvText:
    def test_parses_aliased_headers_into_canonical_rows(self):
        rows, mapping = parse_csv_text(
            "Company,Blurb,Country\nAcme,Ships pallets across Europe efficiently,NL\n"
        )
        assert mapping.columns == {"name": 0, "description": 1, "geo": 2}
        assert rows[0][0] == 1
        assert rows[0][1]["name"] == "Acme"
        assert rows[0][1]["geo"] == "NL"

    def test_blank_lines_are_dropped_without_becoming_skips(self):
        rows, _ = parse_csv_text("name,description\nAcme,Does things well enough\n,\n")
        assert len(rows) == 1

    def test_short_rows_do_not_raise(self):
        # Ragged CSVs are normal; a missing trailing cell must not IndexError.
        rows, mapping = parse_csv_text("name,description,url\nAcme,Does things\n")
        assert build_row(1, ["Acme", "Does things"], mapping)["url"] == ""

    def test_empty_file_yields_nothing(self):
        rows, mapping = parse_csv_text("")
        assert rows == [] and mapping.columns == {}
