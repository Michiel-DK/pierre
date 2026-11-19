"""Judge the newest canary row against the series: the four alert rules (pierre/agent/alerts.py, canary/thresholds.json).

    python scripts/analysis/canary_check.py      # or: make canary-check

Writes the reasons into the newest row's `alerts` (replacing what was there, so a re-run is idempotent). Exit code 0: no rule
fired (or no rows yet); 1: a failure rule fired; 3: only the model / SDK change rule fired, meaning "run `make canary FULL=1`".
`make` itself reports any non-zero recipe as exit 2; the script's own code is printed on the last line.
pierre has no users: the series is a scheduled canary, not organic traffic.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pierre.agent import alerts  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--metrics", type=Path, default=ROOT / "canary" / "metrics.jsonl")
    ap.add_argument("--thresholds", type=Path, default=ROOT / "canary" / "thresholds.json")
    args = ap.parse_args()

    rows = alerts.read_rows(args.metrics)
    if not rows:
        print(f"no rows in {args.metrics.name}: nothing to check (exit code 0)")
        return alerts.EXIT_OK
    res = alerts.check(rows, alerts.load_thresholds(args.thresholds))
    lines = args.metrics.read_text(encoding="utf-8").splitlines()
    rows[-1]["alerts"] = res.alerts
    lines[max(i for i, x in enumerate(lines) if x.strip())] = json.dumps(rows[-1], sort_keys=True, ensure_ascii=False)
    args.metrics.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"checked the newest of {len(rows)} rows ({rows[-1]['date']})")
    for n in res.notes:
        print(f"  note: {n}")
    for a in res.alerts:
        print(f"  ALERT: {a}")
    if not res.alerts:
        print("  no rule fired")
    print(f"exit code {res.exit_code}")
    return res.exit_code


if __name__ == "__main__":
    sys.exit(main())
