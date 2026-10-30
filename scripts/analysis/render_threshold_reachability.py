"""Re-render docs/calibration/threshold_reachability_report.md from its committed snapshot."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from scripts.analysis._threshold_reachability_helpers import render_markdown  # noqa: E402

SNAPSHOT = ROOT / "tests" / "golden" / "threshold_reachability_snapshot.json"
REPORT = ROOT / "docs" / "calibration" / "threshold_reachability_report.md"


def main() -> int:
    snap = json.loads(SNAPSHOT.read_text())
    REPORT.write_text(render_markdown(snap["rows"], captured_at=snap.get("captured_at")))
    print(f"wrote {REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
