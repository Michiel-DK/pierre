"""`make demo` path: the BYO-data demo runs offline on the bundled fictional CSV.

Stdlib + pierre.byodata only (CI-strip safe): the hashing embedder
stands in for Gemini, the judge is off.
"""
import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "byodata_demo", REPO_ROOT / "scripts" / "demo" / "byodata_demo.py"
)
demo = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(demo)


def test_hashing_embedder_is_deterministic_and_normalised():
    emb = demo.HashingEmbedder()
    a, b = emb.embed_query("soil carbon"), emb.embed_query("soil carbon")
    assert a == b
    assert abs(sum(v * v for v in a) - 1.0) < 1e-9


def test_offline_demo_ranks_sample_and_skips_thin_row(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy")
    result = demo.run(demo.DEFAULT_FILE, demo.DEFAULT_CRITERIA,
                      offline=False, use_judge=True, limit=5)
    assert result.total_rows == 20
    assert result.ranked_rows == 19
    assert [s.name for s in result.skipped] == ["Dunvrel Ops"]
    assert result.judged is False
    assert len(result.results) == 5
    assert result.results[0].sector == "climate"


def test_live_embedder_only_with_a_real_key(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy")
    assert demo.use_live_embedder(offline=False) is False
    monkeypatch.setenv("GOOGLE_API_KEY", "real-looking-key")
    assert demo.use_live_embedder(offline=False) is True
    assert demo.use_live_embedder(offline=True) is False


def test_main_falls_back_offline_when_live_call_fails(capsys, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "your-key-here")
    real_rank = demo.rank_upload

    def fake_rank(*args, embedder=None, **kwargs):
        if embedder is None:
            raise RuntimeError("API key not valid")
        return real_rank(*args, embedder=embedder, **kwargs)

    monkeypatch.setattr(demo, "rank_upload", fake_rank)
    assert demo.main(["--limit", "3"]) == 0
    captured = capsys.readouterr()
    assert "falling back to the offline embedder" in captured.err
    assert "offline hashing" in captured.out


def test_main_prints_table(capsys, monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert demo.main(["--limit", "3"]) == 0
    out = capsys.readouterr().out
    assert "offline hashing" in out
    assert "skipped row 20" in out
