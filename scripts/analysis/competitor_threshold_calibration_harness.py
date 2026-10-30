"""Calibration harness for the pitch-to-company semantic-competitor threshold.

The bug
-------
A similarity threshold flags a fund as "backs a direct competitor" when one of
its portfolio companies is close to the pitch. It was calibrated on
company-to-company embedding pairs (both embedded as documents). In production
the pitch is embedded as a *query* and compared with stored *document* vectors.
Mixed task types sit on a measurably lower distribution, and the threshold
never fired on the production corpus.

What this harness measured
--------------------------
Whether a correctly calibrated threshold exists for the pitch-to-company
distribution, with two tests:

1. **Hand-labeled separation.** For two generic sector pitches, real public
   portfolio companies known to be direct competitors (positives) against
   companies from unrelated verticals (negatives; "easy" = obviously different
   business, "hard" = same broad category, different vertical). Company vectors
   were the stored production embeddings, so the comparison matched production.
2. **Candidate-pool flood check** (the decisive test). The negatives above are
   drawn from the whole population, an easy discrimination. Production applies
   the threshold to the post-retrieval pool: firms retrieval already ranked as
   topically similar to the pitch. The check replayed the real matcher call
   chain for six evaluation pitches and recorded what fraction of
   already-retrieved firms would be flagged at each candidate threshold.

A threshold passes only if it is both reachable (fires on known competitors)
and selective (does not flag most of an already-relevant pool). No value
passed both, which is why the threshold was replaced by an LLM verification
step in the source system.

Capturing a new snapshot needs the original corpus and is not included here.
This script re-renders the report and re-checks the gates from the committed
snapshot, with no network or database:

    python scripts/analysis/competitor_threshold_calibration_harness.py --check   # exit 1 = no threshold passes (the finding)
    python scripts/analysis/competitor_threshold_calibration_harness.py --render-md
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_GOLDEN_DIR = _REPO_ROOT / "tests" / "golden"
_SNAPSHOT_PATH = _GOLDEN_DIR / "competitor_threshold_calibration_snapshot.json"
_REPORT_MD_PATH = _REPO_ROOT / "docs" / "calibration" / "competitor_threshold_calibration_report.md"

# ---------------------------------------------------------------------------
# Labeled calibration cases
# ---------------------------------------------------------------------------
#
# Each case: a generic pitch-style paragraph written for this harness
# + REAL portfolio-company names known to be direct competitors (positives)
# + REAL portfolio-company names from a different vertical (negatives, split
#   "easy" = obviously unrelated business, "hard" = same broad category but a
#   different specific vertical — the harder test for separation).
#
# Every company name was checked to have a stored embedding before it was added.

CALIBRATION_CASES: list[dict[str, Any]] = [
    {
        "pitch_id": "cross_border_b2b_payments",
        "pitch_text": (
            "API-first cross-border B2B payments rail enabling businesses to "
            "send and receive international payments without correspondent "
            "banking friction. We provide multi-currency accounts, FX "
            "conversion, and payment-orchestration APIs for platforms and "
            "marketplaces that need to move money across borders at lower cost "
            "and faster settlement than traditional wire transfers."
        ),
        "positive_companies": [
            "Currencycloud", "Wise", "Airwallex", "Payoneer", "Railsr",
        ],
        "negative_companies_easy": [
            "Mosa Meat", "Bond Pet Foods", "Novel Farms", "BIOCRAFT",
        ],
        "negative_companies_hard": [
            "Getsafe", "GetSafe", "WeFox", "wefox",
        ],
    },
    {
        "pitch_id": "embedded_insurance",
        "pitch_text": (
            "We are a digital-first embedded insurance platform offering "
            "renters, household, and liability coverage through API "
            "integrations with partner apps and marketplaces. Customers get "
            "instant underwriting decisions, fully digital claims handling, "
            "and usage-based pricing — replacing legacy broker-distributed "
            "insurance products with a mobile-native, API-embeddable "
            "experience."
        ),
        "positive_companies": [
            "Getsafe", "GetSafe", "WeFox", "wefox",
        ],
        "negative_companies_easy": [
            "Mosa Meat", "Bond Pet Foods", "Novel Farms", "BIOCRAFT",
        ],
        "negative_companies_hard": [
            "Currencycloud", "Wise", "Airwallex", "Payoneer", "Railsr",
        ],
    },
]


def _load_snapshot() -> dict:
    if not _SNAPSHOT_PATH.exists():
        raise FileNotFoundError(
            f"{_SNAPSHOT_PATH} not found — the snapshot ships with the repo."
        )
    return json.loads(_SNAPSHOT_PATH.read_text())


def render_md(snapshot: dict | None = None) -> str:
    snap = snapshot or _load_snapshot()
    lines = [
        "# Competitor-threshold pitch↔company calibration",
        "",
        f"_Auto-generated by `scripts/analysis/competitor_threshold_calibration_harness.py`. "
        f"Captured: {snap.get('captured_at', 'unknown')}._",
        "",
        "> Measured on the source system's private corpus (stored production embeddings of real, public "
        "portfolio companies). The committed snapshot is the record; it cannot be re-captured from this repo.",
        "",
        "## Per-case similarity samples",
        "",
    ]
    for case in snap["cases"]:
        lines.append(f"### {case['pitch_id']}")
        lines.append("")
        lines.append(f"- Positives (known competitors), n={case['positive_rows_found']}:")
        for m in case["positive_matches"]:
            lines.append(f"  - {m['company_name']} ({m['firm_slug']}): **{m['similarity']}**")
        lines.append(f"- Negatives (easy, unrelated vertical), n={case['negative_easy_rows_found']}:")
        for m in case["negative_easy_matches"]:
            lines.append(f"  - {m['company_name']} ({m['firm_slug']}): {m['similarity']}")
        lines.append(f"- Negatives (hard, same broad category), n={case['negative_hard_rows_found']}:")
        for m in case["negative_hard_matches"]:
            lines.append(f"  - {m['company_name']} ({m['firm_slug']}): {m['similarity']}")
        zfp = case["zero_fp_floor_this_case"]
        lines.append(
            f"- **Zero-FP floor this case**: max negative = {zfp['floor']}, "
            f"→ threshold ≥ {round(zfp['threshold'], 4) if zfp['threshold'] is not None else None} "
            f"retains {zfp['positives_retained']}/{zfp['n_positives']} positives "
            f"(recall={round(zfp['recall'], 3) if zfp['recall'] is not None else None})"
        )
        lines.append("")

    for label, key in [
        ("ALL negatives (easy+hard combined)", "overall_separation_vs_all_negatives"),
        ("HARD negatives only", "separation_vs_hard_negatives_only"),
        ("EASY negatives only", "separation_vs_easy_negatives_only"),
    ]:
        sep = snap[key]
        lines.append(f"## Separation vs {label}")
        lines.append("")
        ps, ns = sep["positive_stats"], sep["negative_stats"]
        lines.append(f"- Positive distribution: n={ps['n']}, mean={ps['mean']:.4f}, "
                      f"min={ps['min']:.4f}, max={ps['max']:.4f}")
        lines.append(f"- Negative distribution: n={ns['n']}, mean={ns['mean']:.4f}, "
                      f"min={ns['min']:.4f}, max={ns['max']:.4f}")
        lines.append(f"- **Clean separation: {sep['clean_separation']}** "
                      f"(gap={sep['gap']})")
        lines.append(f"- Best achievable threshold (balanced accuracy / Youden's J): {sep['candidate_threshold']}")
        lines.append(f"  → false positives at that threshold: {sep['false_positives_at_candidate']}, "
                      f"false negatives: {sep['false_negatives_at_candidate']}")
        lines.append(f"- {sep['reason']}")
        zfp = sep["zero_fp"]
        lines.append(
            f"- **Precision-first (zero-FP) floor**: max negative = {zfp['floor']}, "
            f"→ threshold ≥ {round(zfp['threshold'], 4) if zfp['threshold'] is not None else None} "
            f"retains {zfp['positives_retained']}/{zfp['n_positives']} positives "
            f"(recall={round(zfp['recall'], 3) if zfp['recall'] is not None else None}) with **0 false positives**"
        )
        lines.append("")

    flood = snap.get("candidate_pool_flood_check")
    if flood:
        lines.append("## Candidate-pool flood check (the decisive test)")
        lines.append("")
        lines.append(
            f"Replays the source system's matcher call chain (firm retrieval → "
            f"per-portfolio-company semantic competitor scoring → self-match "
            f"filter → competitor tiering) against "
            f"the 6 golden pitches, n_results={flood['n_results']}. Reports "
            f"what fraction of the matcher's OWN already-retrieved candidate "
            f"firms get flagged CONFLICT at each swept threshold."
        )
        lines.append("")
        header = "| pitch | n_candidates | " + " | ".join(f"@{t}" for t in flood["thresholds_swept"]) + " |"
        sep_row = "|---" * (2 + len(flood["thresholds_swept"])) + "|"
        lines.append(header)
        lines.append(sep_row)
        for pp in flood["per_pitch"]:
            cells = []
            for t in flood["thresholds_swept"]:
                bt = pp["by_threshold"][str(t)]
                pct = bt["conflict_flagged_pct"]
                cells.append(f"{bt['conflict_flagged_firms']}/{pp['n_candidates']} ({pct:.0%})" if pct is not None else "n/a")
            lines.append(f"| {pp['pitch_id']} | {pp['n_candidates']} | " + " | ".join(cells) + " |")
        lines.append("")

    md = "\n".join(lines) + "\n"
    _REPORT_MD_PATH.parent.mkdir(parents=True, exist_ok=True)
    _REPORT_MD_PATH.write_text(md)
    return md


def check(
    snapshot: dict | None = None,
    *,
    min_recall: float = 0.5,
    max_flood_pct: float = 0.25,
) -> bool:
    """Print both tests and return True iff a threshold exists that clears
    BOTH gates simultaneously:

    1. **Reachable** — retains ≥ ``min_recall`` of the hand-labeled known
       competitors (``overall_separation_vs_all_negatives`` positives).
    2. **Selective** — the candidate-pool flood check
       (``candidate_pool_flood_check``) never flags more than
       ``max_flood_pct`` of any golden pitch's already-retrieved candidate
       firms at that same threshold.

    This is the real decision gate (see the module docstring: two tests, not
    one): the hand-labeled separation alone is NOT
    sufficient — it can look clean while still flooding the post-retrieval
    pool the threshold actually operates on in production. Defaults
    (``min_recall=0.5``, ``max_flood_pct=0.25``) are permissive sanity bars,
    not a proposal — interpret the actual numbers, not just pass/fail.
    """
    snap = snapshot or _load_snapshot()
    sep = snap["overall_separation_vs_all_negatives"]
    zfp = sep["zero_fp"]
    print(f"clean_separation(balanced, hand-labeled)={sep['clean_separation']} gap={sep['gap']}")
    print(f"hand-labeled positives: n={sep['positive_stats']['n']} min={sep['positive_stats']['min']} "
          f"max={sep['positive_stats']['max']}")
    print(f"zero-FP floor (hand-labeled): threshold>={zfp['threshold']} retains "
          f"{zfp['positives_retained']}/{zfp['n_positives']} positives (recall={zfp['recall']})")
    for case in snap["cases"]:
        czfp = case["zero_fp_floor_this_case"]
        print(f"  per-case zero-FP floor [{case['pitch_id']}]: threshold>={czfp['threshold']} "
              f"recall={czfp['recall']}")

    all_positive_sims = [m["similarity"] for c in snap["cases"] for m in c["positive_matches"]]
    flood = snap.get("candidate_pool_flood_check")
    safe_and_reachable: list[float] = []
    if flood and all_positive_sims:
        for t in flood["thresholds_swept"]:
            recall_at_t = sum(1 for v in all_positive_sims if v >= t) / len(all_positive_sims)
            max_flood_at_t = max(
                pp["by_threshold"][str(t)]["conflict_flagged_pct"] or 0.0
                for pp in flood["per_pitch"]
            )
            reachable = recall_at_t >= min_recall
            selective = max_flood_at_t <= max_flood_pct
            print(f"  threshold={t}: hand-labeled recall={recall_at_t:.2%} "
                  f"(reachable={reachable}), max candidate-pool flood={max_flood_at_t:.0%} "
                  f"(selective={selective})")
            if reachable and selective:
                safe_and_reachable.append(t)

    ok = bool(safe_and_reachable)
    print(f"\nthresholds clearing BOTH gates: {safe_and_reachable or 'NONE'}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--render-md", action="store_true",
                        help="Re-render the markdown report from the committed snapshot.")
    parser.add_argument("--check", action="store_true",
                        help="Check the snapshot for a threshold clearing both gates (default). Exits 1 when none does, which is the recorded finding.")
    args = parser.parse_args()

    if args.render_md:
        render_md()
        print(f"wrote {_REPORT_MD_PATH}")
        return 0
    return 0 if check() else 1


if __name__ == "__main__":
    raise SystemExit(main())
