"""The four canary alert rules, on fixture series (no model call, no key). Stdlib only."""
import json
import random
import subprocess
import sys
from pathlib import Path

import pytest

from pierre.agent import alerts

ROOT = Path(__file__).resolve().parents[2]
THRESHOLDS = alerts.load_thresholds(ROOT / "canary" / "thresholds.json")
CASES = ["a", "b", "c", "d", "e"]


def row(i, passed=4, cost=0.10, causes=None, model="m1", sdk="1.0", case_ids=None):
    return {
        "date": f"2026-12-{i:02d}", "pass_count": passed, "cases_graded": 5, "turns": 8,
        "cost_per_turn_usd_p50": cost, "model": model, "sdk": sdk,
        "case_ids": case_ids or CASES, "termination_causes": causes or {}, "alerts": [],
    }


def series(n, **kw):
    return [row(i + 1, **kw) for i in range(n)]


def check(rows):
    return alerts.check(rows, THRESHOLDS)


# --- thresholds file -----------------------------------------------------------------------------------------------------


def test_thresholds_file_holds_the_four_rules():
    assert THRESHOLDS["baseline_rows"] == 7
    assert THRESHOLDS["pass_drop"] == {"drop": 2, "consecutive_runs": 2}
    assert THRESHOLDS["cost"]["factor"] == 1.5
    assert THRESHOLDS["terminations"]["causes"] == ["timeout", "max_tokens"]
    assert THRESHOLDS["change"]["exit_code"] == 3


THRESHOLDS_ADDED = json.loads((ROOT / "canary" / "thresholds_added.json").read_text(encoding="utf-8"))


def _metric_rows():
    p = ROOT / "canary" / "metrics.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_no_row_is_dated_before_the_thresholds_record():
    # Only dates are checked: the record holds the date of the commit that added thresholds.json, and every row must be
    # dated on or after it. A row of the same date passes, so this does not show the order within that day. No row at
    # all also holds.
    added = THRESHOLDS_ADDED["date"]
    early = [r["date"] for r in _metric_rows() if r["date"] < added]
    assert early == [], f"rows dated before the thresholds were committed ({added}): {early}"


def test_the_metric_rows_are_in_date_order_one_per_date():
    dates = [r["date"] for r in _metric_rows()]
    assert dates == sorted(set(dates))


def test_the_readme_states_the_thresholds_date_from_the_record_and_no_more():
    # A published copy has no history of this repository, so the record holds a date and no commit id, and the README
    # claims only that date plus the date check above; the order within the day is stated as private.
    assert set(THRESHOLDS_ADDED) == {"date", "file", "note"}
    assert (ROOT / THRESHOLDS_ADDED["file"]).exists()
    readme = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
    assert f"thresholds in `canary/thresholds.json`, dated {THRESHOLDS_ADDED['date']} in `canary/thresholds_added.json`, the date of the first row" in readme
    assert "committed before the first row existed" not in readme


# --- no rule fires / short series ----------------------------------------------------------------------------------------


def test_a_healthy_series_fires_nothing():
    r = check(series(10))
    assert r.alerts == [] and r.exit_code == 0


def test_fewer_than_seven_rows_skips_the_median_rules_and_says_so():
    rows = series(6) + [row(7, passed=0, cost=9.0)]  # 6 previous rows: a collapse in pass and cost
    r = check(rows)
    assert r.alerts == [] and r.exit_code == 0
    assert any("pass_drop" in n and "not evaluated" in n and "6 of 7" in n for n in r.notes)
    assert any("cost" in n and "not evaluated" in n for n in r.notes)


def test_fewer_than_seven_rows_still_runs_the_termination_rule():
    r = check(series(2) + [row(3, causes={"timeout": 1})])
    assert r.exit_code == 1 and r.alerts[0].startswith("termination")


def test_fewer_than_seven_rows_still_runs_the_change_rule():
    r = check([row(1), row(2, model="m2")])
    assert r.exit_code == 3 and r.alerts[0].startswith("changed")


def test_a_single_row_fires_nothing():
    assert check([row(1)]).exit_code == 0


def test_no_rows_is_exit_zero():
    r = check([])
    assert r.exit_code == 0 and r.alerts == []


# --- rule 1: pass count --------------------------------------------------------------------------------------------------


def test_pass_drop_of_two_on_two_runs_in_a_row_fires():
    r = check(series(7) + [row(8, passed=2), row(9, passed=2)])  # median 4, a drop of exactly 2, twice
    assert r.exit_code == 1 and any(a.startswith("pass_drop") for a in r.alerts)


def test_one_bad_run_is_noise():
    r = check(series(7) + [row(8, passed=0)])
    assert r.alerts == [] and r.exit_code == 0


def test_a_bad_run_followed_by_a_good_one_does_not_fire():
    assert check(series(7) + [row(8, passed=1), row(9, passed=4)]).exit_code == 0


def test_a_drop_of_one_twice_does_not_fire():
    assert check(series(7) + [row(8, passed=3), row(9, passed=3)]).exit_code == 0


def test_pass_drop_uses_the_median_of_the_previous_seven_not_the_mean():
    prev = [row(i + 1, passed=p) for i, p in enumerate([5, 5, 5, 5, 4, 0, 0])]  # median 5, mean 3.4
    assert check(prev + [row(8, passed=4), row(9, passed=4)]).exit_code == 0
    # row 8 (3) is bad against median 5; row 9 (2) against the median of rows 2 to 8, which row 8 moved to 4
    assert check(prev + [row(8, passed=3), row(9, passed=2)]).exit_code == 1


def test_pass_drop_looks_back_seven_rows_only():
    old = [row(i + 1, passed=5) for i in range(5)]
    recent = [row(i + 6, passed=3) for i in range(7)]  # the last 7 are the baseline: median 3
    assert check(old + recent + [row(13, passed=1), row(14, passed=1)]).exit_code == 1
    assert check(old + recent + [row(13, passed=2), row(14, passed=2)]).exit_code == 0


def test_a_full_run_row_is_not_a_baseline_for_the_canary_rows():
    full = row(1, passed=15, case_ids=[f"x{i}" for i in range(21)])
    rows = [row(i + 2, passed=4) for i in range(7)]
    rows.insert(3, full)
    # the canary baseline is the seven 5-case rows (median 4), the 21-case row is skipped
    assert check(rows + [row(20, passed=4), row(21, passed=4)]).exit_code == 0


def test_a_full_run_row_has_no_baseline_and_fires_no_median_rule():
    full = row(8, passed=0, cost=9.0, case_ids=[f"x{i}" for i in range(21)])
    r = check(series(7) + [full])
    assert r.alerts == []
    assert any("not evaluated" in n for n in r.notes)


# --- rule 2: cost --------------------------------------------------------------------------------------------------------


def test_cost_above_one_and_a_half_times_the_median_fires():
    r = check(series(7, cost=0.10) + [row(8, cost=0.151)])
    assert r.exit_code == 1 and any(a.startswith("cost") for a in r.alerts)


def test_cost_at_exactly_one_and_a_half_times_does_not_fire():
    assert check(series(7, cost=0.10) + [row(8, cost=0.15)]).exit_code == 0


def test_cost_uses_the_median_of_the_previous_seven_rows():
    prev = [row(i + 1, cost=c) for i, c in enumerate([0.10, 0.10, 0.10, 0.10, 0.10, 1.0, 1.0])]  # median 0.10
    assert check(prev + [row(8, cost=0.16)]).exit_code == 1
    assert check(prev + [row(8, cost=0.14)]).exit_code == 0


def test_a_null_cost_skips_the_cost_rule_and_says_so():
    r = check(series(7) + [row(8, cost=None)])
    assert r.exit_code == 0 and any("cost" in n and "null" in n for n in r.notes)


# --- rule 3: terminations ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("cause", ["timeout", "max_tokens"])
def test_a_timeout_or_max_tokens_termination_fires(cause):
    r = check(series(8) + [row(9, causes={cause: 1})])
    assert r.exit_code == 1 and any(a.startswith("termination") and cause in a for a in r.alerts)


def test_other_termination_causes_do_not_fire():
    assert check(series(8) + [row(9, causes={"iteration_cap": 1, "refusal": 2})]).exit_code == 0


def test_a_termination_in_an_older_row_does_not_fire_again():
    assert check([row(1, causes={"timeout": 1})] + series(8)[1:]).exit_code == 0


def test_a_termination_count_without_causes_fires_as_unknown_cause():
    bad = row(9)
    del bad["termination_causes"]
    bad["termination_rate"] = 0.125
    r = check(series(8) + [bad])
    assert r.exit_code == 1 and any("cause unknown" in a for a in r.alerts)


# --- rule 4: model id / SDK version --------------------------------------------------------------------------------------


def test_a_changed_model_id_is_exit_three_not_a_failure():
    r = check(series(8) + [row(9, model="m2")])
    assert r.exit_code == 3 and any(a.startswith("changed") and "model" in a for a in r.alerts)


def test_a_changed_sdk_version_is_exit_three():
    r = check(series(8) + [row(9, sdk="1.1")])
    assert r.exit_code == 3 and any("sdk" in a for a in r.alerts)


def test_a_failure_rule_takes_precedence_over_the_change_notice():
    r = check(series(8) + [row(9, model="m2", causes={"timeout": 1})])
    assert r.exit_code == 1 and len(r.alerts) == 2


def test_the_change_rule_compares_with_the_previous_row_only():
    assert check([row(1, model="m0")] + series(9)[1:]).exit_code == 0


# --- writing the reasons into the row, and the command -------------------------------------------------------------------


def _write(path, rows):
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8")


def _cli(metrics):
    return subprocess.run(
        [sys.executable, "-I", str(ROOT / "scripts" / "analysis" / "canary_check.py"), "--metrics", str(metrics),
         "--thresholds", str(ROOT / "canary" / "thresholds.json")],
        capture_output=True, text=True,
    )


def test_cli_writes_the_reasons_into_the_newest_row_and_exits_one(tmp_path):
    p = tmp_path / "metrics.jsonl"
    _write(p, series(8) + [row(9, causes={"max_tokens": 2})])
    r = _cli(p)
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
    assert r.returncode == 1 and len(rows) == 9
    assert rows[-1]["alerts"] and rows[-1]["alerts"][0].startswith("termination")
    assert all(x["alerts"] == [] for x in rows[:-1])


def test_cli_rewrites_the_alerts_each_time_it_runs(tmp_path):
    p = tmp_path / "metrics.jsonl"
    _write(p, series(8) + [row(9, causes={"timeout": 1})])
    _cli(p)
    _cli(p)
    last = json.loads(p.read_text(encoding="utf-8").splitlines()[-1])
    assert len(last["alerts"]) == 1


def test_cli_exit_codes_zero_and_three(tmp_path):
    p = tmp_path / "metrics.jsonl"
    _write(p, series(9))
    assert _cli(p).returncode == 0
    _write(p, series(8) + [row(9, sdk="2.0")])
    r = _cli(p)
    assert r.returncode == 3 and "make canary FULL=1" in r.stdout
    assert json.loads(p.read_text(encoding="utf-8").splitlines()[-1])["alerts"][0].startswith("changed")


def test_cli_with_no_metrics_file_or_an_empty_one_is_exit_zero(tmp_path):
    p = tmp_path / "metrics.jsonl"
    r = _cli(p)
    assert r.returncode == 0 and "no rows" in r.stdout
    p.write_text("", encoding="utf-8")
    assert _cli(p).returncode == 0


def test_a_healthy_simulated_series_rarely_fires_the_pass_rule():
    rng = random.Random(0)
    n, fired = 3000, 0
    rows = []
    for i in range(n):
        rows.append(row(1, passed=sum(rng.random() < 0.8 for _ in range(5))))
        rows[-1]["date"] = f"d{i}"
        if len(rows) >= 9 and any(a.startswith("pass_drop") for a in check(rows[-9:]).alerts):
            fired += 1
    assert fired / (n - 8) < 0.06


# --- the row records the cause, and the README names the rules ------------------------------------------------------------


def test_the_makefile_has_the_canary_check_target():
    mk = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "canary-check:" in mk and "canary_check.py" in mk


def test_readme_has_the_watching_it_run_section_with_the_thresholds_and_the_scheduled_canary_sentence():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "## Watching it run" in text
    sec = text.split("## Watching it run", 1)[1].split("\n## ", 1)[0]
    assert "scheduled canary" in sec and "not users" in sec
    for needle in ("median of the previous 7", "1.5", "timeout", "max_tokens", "exit code 3", "14 rows"):
        assert needle in sec, needle


def readme_rows_sentence(rows):
    """The README's line about the series, rendered from metrics.jsonl so it cannot go stale."""
    if not rows:
        return "No row exists yet: nothing here has been observed running."
    n = len(rows)
    return f"{n} row{'s' if n != 1 else ''} exist{'' if n != 1 else 's'} in `canary/metrics.jsonl`, the first dated {rows[0]['date']}."


def test_readme_states_the_row_count_and_the_first_date():
    sec = (ROOT / "README.md").read_text(encoding="utf-8").split("## Watching it run", 1)[1].split("\n## ", 1)[0]
    want = readme_rows_sentence(_metric_rows())
    assert want in " ".join(sec.split()), (
        "README 'Watching it run' is stale: replace its row-count sentence with exactly:\n" + want)
