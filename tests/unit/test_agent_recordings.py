"""The committed recordings: provenance, hygiene, and offline replay that reproduces the committed traces byte for byte."""
from __future__ import annotations

import hashlib
import json

import pytest

from pierre.agent.factory import RECORDINGS_DIR, load_cases, replay_case

RECORDINGS = sorted(p for p in RECORDINGS_DIR.glob("*.jsonl") if not p.name.endswith((".trace.jsonl", "record_log.jsonl")))
IDS = [p.stem for p in RECORDINGS]


def _lines(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def test_there_are_recordings_for_train_cases_only():
    cases = load_cases()
    assert len(RECORDINGS) >= 8
    assert {cases[i]["split"] for i in IDS} == {"train"}


@pytest.mark.parametrize("path", RECORDINGS, ids=IDS)
def test_recording_provenance_and_hygiene(path):
    header, *calls = _lines(path)
    assert header["recorded"] is True and header["case_id"] == path.stem
    assert header["model"] == "claude-opus-5-5" and header["effort"] == "medium"
    assert calls and all(c["response"]["id"].startswith("msg_") for c in calls)
    assert {c["role"] for c in calls} <= {"agent", "auditor"}
    text = path.read_text(encoding="utf-8")
    for needle in ("req_", "request-id", "request_id", "sk-ant", "x-api-key", "/Users/", "organization", "api_key"):
        assert needle not in text, needle


def test_every_recording_has_a_log_line_with_its_hash():
    log = {x["session"]: x for x in _lines(RECORDINGS_DIR / "record_log.jsonl")}
    for path in RECORDINGS:
        entry = log[path.stem]
        assert entry["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest(), f"{path.name} changed after it was recorded"
        assert entry["calls"] == len(_lines(path)) - 1


@pytest.mark.parametrize("case_id", IDS)
def test_replay_is_byte_stable_and_matches_the_committed_trace(case_id):
    results, first = replay_case(case_id)
    _, second = replay_case(case_id)
    committed = (RECORDINGS_DIR / f"{case_id}.trace.jsonl").read_bytes()
    assert first.read_bytes() == second.read_bytes() == committed
    assert all(r.steps <= 16 and r.auditor_calls <= 3 for r in results)
