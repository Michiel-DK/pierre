"""Unit tests for the BYO-data orchestrator + judge — no network, no LLM.

Both the embedder and the judge are injected fakes. What these prove:

- the two-stage shape actually holds (LLM sees top-K only, not every row),
- ``skipped_rows`` reaches the caller instead of being swallowed,
- a failing judge degrades to similarity order and SAYS it degraded,
- nothing is written to disk.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from pierre.byodata.rank import NoRankableRows, RankResult, rank_upload
from pierre.byodata.schema import UserRow

pytest.importorskip("google.genai", reason="rank imports the embedding service lazily")


class FakeEmbedder:
    """Deterministic 2-d embeddings: row text containing 'logistics' points one
    way, everything else the other. Records what it was asked to embed."""

    def __init__(self):
        self.documents: list[str] = []
        self.queries: list[str] = []

    def _vec(self, text: str):
        return [1.0, 0.0] if "logistics" in text.lower() else [0.0, 1.0]

    def embed_documents(self, texts):
        self.documents.extend(texts)
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        self.queries.append(text)
        return self._vec(text)


def _csv(rows: int = 4, *, thin: int = 0) -> bytes:
    lines = ["Company,Blurb,Country"]
    for i in range(rows):
        topic = "logistics freight forwarding across Europe" if i % 2 == 0 else "consumer mobile games studio"
        lines.append(f"Company {i},{topic} for enterprise customers,NL")
    for j in range(thin):
        lines.append(f"Thin {j},x,NL")
    return ("\n".join(lines) + "\n").encode()


CRITERIA = "European logistics and freight software companies"


class TestRankUpload:
    def test_ranks_by_similarity_when_judge_disabled(self):
        result = rank_upload("book.csv", _csv(4), CRITERIA, use_judge=False, embedder=FakeEmbedder())
        assert isinstance(result, RankResult)
        assert result.ranked_rows == 4
        assert result.judged is False
        # The two logistics rows must outrank the two games rows.
        assert [r.name for r in result.results[:2]] == ["Company 0", "Company 2"]
        assert result.results[0].grade is None

    def test_skipped_rows_are_counted_and_returned(self):
        result = rank_upload(
            "book.csv", _csv(4, thin=3), CRITERIA, use_judge=False, embedder=FakeEmbedder()
        )
        assert result.ranked_rows == 4
        assert result.skipped_rows == 3
        assert {s.reason for s in result.skipped} == {"description too short (<25 chars)"}
        payload = result.to_dict()
        assert payload["skipped_rows"] == 3 and len(payload["skipped"]) == 3

    def test_thin_rows_never_appear_in_results(self):
        result = rank_upload(
            "book.csv", _csv(2, thin=5), CRITERIA, use_judge=False, embedder=FakeEmbedder()
        )
        assert all(not r.name.startswith("Thin") for r in result.results)

    def test_only_top_k_rows_reach_the_judge(self):
        seen = {}

        def fake_judge(criteria, rows):
            seen["n"] = len(rows)
            return list(rows), {}, 0.0, True

        rank_upload(
            "book.csv", _csv(20), CRITERIA, top_k=5, use_judge=True,
            embedder=FakeEmbedder(), judge=fake_judge,
        )
        # 20 rows embedded, only 5 graded — the cost-flattening property.
        assert seen["n"] == 5

    def test_judge_grades_are_surfaced_per_row(self):
        from pierre.matching.reranker import RerankGrade

        def fake_judge(criteria, rows):
            grades = {rows[0].row_id: RerankGrade(grade=3, reason="core fit")}
            return list(rows), grades, 0.0021, False

        result = rank_upload(
            "book.csv", _csv(4), CRITERIA, use_judge=True,
            embedder=FakeEmbedder(), judge=fake_judge,
        )
        assert result.judged is True and result.judge_degraded is False
        assert result.results[0].grade == 3
        assert result.results[0].reason == "core fit"
        assert result.cost_usd == pytest.approx(0.0021)
        # Ungraded rows report None, not a fabricated 0.
        assert result.results[1].grade is None

    def test_judge_failure_degrades_to_similarity_order_and_says_so(self):
        def failing_judge(criteria, rows):
            return list(rows), {}, 0.0, True

        result = rank_upload(
            "book.csv", _csv(4), CRITERIA, use_judge=True,
            embedder=FakeEmbedder(), judge=failing_judge,
        )
        # judged=False because no grade came back — see
        # test_totally_failed_judge_reports_not_judged for why that matters.
        assert result.judged is False
        assert result.judge_degraded is True
        assert [r.name for r in result.results[:2]] == ["Company 0", "Company 2"]

    def test_limit_caps_returned_rows_but_not_ranked_count(self):
        result = rank_upload(
            "book.csv", _csv(10), CRITERIA, limit=3, use_judge=False, embedder=FakeEmbedder()
        )
        assert len(result.results) == 3
        assert result.ranked_rows == 10

    def test_criteria_is_what_gets_embedded_as_the_query(self):
        embedder = FakeEmbedder()
        rank_upload("book.csv", _csv(2), CRITERIA, use_judge=False, embedder=embedder)
        assert embedder.queries == [CRITERIA]

    def test_missing_description_column_raises_with_diagnosis(self):
        data = b"Company,Owner\nAcme,Jane\n"
        with pytest.raises(NoRankableRows) as exc:
            rank_upload("book.csv", data, CRITERIA, embedder=FakeEmbedder())
        assert "description" in str(exc.value)
        assert exc.value.mapping.missing_required == ["description"]

    def test_all_thin_rows_raises_the_other_diagnosis(self):
        data = b"Company,Blurb\nAcme,x\nBeta,y\n"
        with pytest.raises(NoRankableRows) as exc:
            rank_upload("book.csv", data, CRITERIA, embedder=FakeEmbedder())
        assert "skipped" in str(exc.value)
        assert len(exc.value.skipped) == 2

    def test_rows_past_the_cap_are_reported_not_silently_dropped(self, monkeypatch):
        """A capped file must not read as fully ranked.

        Without ``truncated_rows`` the response said "5000 rows ranked, 0
        skipped" for a 6000-row book — the user is told their whole list was
        covered. That is the same silent-drop failure ``skipped_rows`` exists
        to prevent, and worse, because it is invisible from the output.
        """
        monkeypatch.setattr("pierre.byodata.parse.MAX_ROWS", 5)
        result = rank_upload(
            "book.csv", _csv(8), CRITERIA, use_judge=False, embedder=FakeEmbedder()
        )
        assert result.ranked_rows == 5
        assert result.truncated_rows == 3
        # total_rows counts the file as SENT, not what survived the cap.
        assert result.total_rows == 8
        assert result.to_dict()["truncated_rows"] == 3

    def test_no_truncation_reported_for_a_normal_file(self):
        result = rank_upload(
            "book.csv", _csv(6), CRITERIA, use_judge=False, embedder=FakeEmbedder()
        )
        assert result.truncated_rows == 0
        assert result.total_rows == 6

    def test_csv_and_xlsx_count_the_cap_identically(self, monkeypatch):
        """The cap must apply to DATA rows in both formats — the xlsx reader
        used to count source rows (blank check after the cap), so the same
        file saved both ways gave different totals."""
        openpyxl = pytest.importorskip("openpyxl")
        import io

        monkeypatch.setattr("pierre.byodata.parse.MAX_ROWS", 4)

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Company", "Blurb"])
        for i in range(6):
            ws.append([f"Company {i}", "logistics freight forwarding across Europe"])
            ws.append([None, None])  # interleaved blank rows
        buf = io.BytesIO()
        wb.save(buf)

        xlsx = rank_upload("b.xlsx", buf.getvalue(), CRITERIA, use_judge=False, embedder=FakeEmbedder())
        assert (xlsx.ranked_rows, xlsx.truncated_rows) == (4, 2)

    def test_short_embedding_response_fails_loud(self):
        """zip() would silently shorten the ranking; rows would vanish from the
        answer while ranked_rows still counted them."""
        from pierre.byodata.rank import EmbeddingCountMismatch

        class ShortEmbedder(FakeEmbedder):
            def embed_documents(self, texts):
                return super().embed_documents(texts)[:-1]

        with pytest.raises(EmbeddingCountMismatch):
            rank_upload("book.csv", _csv(4), CRITERIA, use_judge=False, embedder=ShortEmbedder())

    def test_totally_failed_judge_reports_not_judged(self):
        """judged=True with zero grades renders as 'partial' when the honest
        word is 'none' — every row would show a blank fit score."""
        def dead_judge(criteria, rows):
            return list(rows), {}, 0.0, True

        result = rank_upload(
            "book.csv", _csv(4), CRITERIA, use_judge=True,
            embedder=FakeEmbedder(), judge=dead_judge,
        )
        assert result.judged is False
        assert result.judge_degraded is True

    def test_nothing_is_written_to_disk(self):
        before = set(os.listdir(tempfile.gettempdir()))
        rank_upload("book.csv", _csv(6), CRITERIA, use_judge=False, embedder=FakeEmbedder())
        after = set(os.listdir(tempfile.gettempdir()))
        assert after - before == set()


class TestXlsxPath:
    def test_xlsx_parses_through_the_same_pipeline(self):
        openpyxl = pytest.importorskip("openpyxl")
        import io

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Organisation", "What They Do", "HQ"])
        ws.append(["Acme", "logistics freight forwarding across Europe daily", "NL"])
        ws.append(["Beta", "consumer mobile games studio for casual players", "DE"])
        ws.append(["Thin", "x", "FR"])
        buf = io.BytesIO()
        wb.save(buf)

        result = rank_upload(
            "book.xlsx", buf.getvalue(), CRITERIA, use_judge=False, embedder=FakeEmbedder()
        )
        assert result.ranked_rows == 2
        assert result.skipped_rows == 1
        assert result.results[0].name == "Acme"

    def test_unsupported_extension_is_rejected(self):
        from pierre.byodata.parse import UnsupportedFileType

        with pytest.raises(UnsupportedFileType):
            rank_upload("book.xls", b"whatever", CRITERIA, embedder=FakeEmbedder())


class TestJudgePromptShape:
    """The judge's pure builders — company semantics, not firm semantics."""

    def test_company_text_carries_row_id_and_omits_empty_tags(self):
        from pierre.byodata._judge import company_text

        text = company_text(UserRow(7, "Acme", "Ships pallets", sector="Logistics"))
        assert "row_id=r7" in text and "sector=Logistics" in text
        assert "stage=" not in text and "location=" not in text

    def test_batch_prompt_states_the_criteria_and_asks_for_row_id(self):
        from pierre.byodata._judge import build_batch_prompt

        prompt = build_batch_prompt(CRITERIA, [UserRow(1, "Acme", "Ships pallets")])
        assert CRITERIA in prompt
        assert "row_id" in prompt
        # It must NOT inherit the reranker's VC framing.
        assert "STARTUP PITCH" not in prompt and "FIRM" not in prompt

    def test_grade_rows_degrades_when_client_cannot_be_built(self):
        from pierre.byodata._judge import grade_rows

        rows = [UserRow(1, "Acme", "Ships pallets"), UserRow(2, "Beta", "Games")]

        def boom():
            raise RuntimeError("no key")

        ordered, grades, cost, degraded = grade_rows(CRITERIA, rows, client_factory=boom)
        assert [r.row_id for r in ordered] == ["r1", "r2"]  # incoming order kept
        assert grades == {} and cost == 0.0 and degraded is True

    def test_grade_rows_empty_input_is_not_an_error(self):
        from pierre.byodata._judge import grade_rows

        assert grade_rows(CRITERIA, []) == ([], {}, 0.0, False)
