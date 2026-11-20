"""Offline calibration harness for the report claim-auditor.

This is the report-quality counterpart to the source system's match-ranking
golden harness. It is a STANDING harness, not a one-shot measurement: capture a
snapshot, ``--check`` against it on every change, and let the CI-strip-safe
shape guard (``tests/unit/test_claim_auditor_calibration.py``) pin the artifact
shape.

What it measures
----------------
The offline calibration of the auditor itself. It never changes an
individual report; it scores the AUDITOR so a human can retune it:

  fixed labeled fixtures → run the auditor → reduce each fixture's sub-claim
  verdicts to one EFFECTIVE verdict (most-severe rule) → compare to the label
  → emit a confusion matrix + per-verdict precision/recall + the HEADLINE
  ``fabricated`` false-positive rate.

The headline FP-rate is the operator's standing concern as a number: of the
fixtures that SHOULD survive softening (label ∈ {verified, fair_inference}),
what fraction did the auditor effectively flag ``fabricated`` (would be
stripped)? An earlier live check found 4/5 ``fabricated`` flags were
false; this harness turns that gut feeling into a tracked metric.

Why context is FROZEN inline
----------------------------
A match-ranking harness re-runs against the live corpus *by design* — it tests
match-against-current-corpus. This harness tests the AUDITOR, which must be
isolated from corpus drift (the database gets repopulated), or the FP-rate
can't be attributed to auditor changes vs DB shift. So each fixture's
context is captured once and frozen in the committed JSON. Consequence: this
harness needs ONLY a Claude client, NOT a DB.

Determinism
-----------
The model is not fully deterministic (these models take no temperature setting), so
a borderline claim can flip verified↔unverifiable between runs. So ``--check``
gates on the AGGREGATE fabricated-FP-rate threshold (the headline metric), NOT
on exact per-fixture reproduction. Per-fixture verdict drift vs the snapshot is
printed as human drift-inspection info only — never a hard failure.

Usage
-----
    python scripts/analysis/claim_auditor_calibration_harness.py          # capture + write snapshot
    python scripts/analysis/claim_auditor_calibration_harness.py --check  # compare to threshold, exit 1 on regress
    python scripts/analysis/claim_auditor_calibration_harness.py --limit 5  # smoke a few fixtures
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure the repo root is importable when this is
# invoked as `python scripts/analysis/X.py` (sys.path[0] is scripts/analysis/,
# not the repo root). scripts/analysis/X.py → parent.parent.parent = repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from pierre.agents._claim_auditor_calibration import (  # noqa: E402
    confusion_matrix,
    effective_verdict,
    fabricated_false_positive_rate,
    fabricated_recall,
    per_verdict_precision_recall,
)
from pierre.agents._claim_auditor import DEFAULT_AUDIT_MODEL  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_GOLDEN_DIR = _REPO_ROOT / "tests" / "golden"
_FIXTURES_PATH = _GOLDEN_DIR / "claim_auditor_calibration_fixtures.json"
_SNAPSHOT_PATH = _GOLDEN_DIR / "claim_auditor_calibration_snapshot.json"
# Human-readable, client-presentable report — regenerated on every capture (and
# via --render-md without a model call) so the number a client sees never goes stale.
_REPORT_MD_PATH = _REPO_ROOT / "docs" / "calibration" / "claim_auditor_calibration_report.md"


def _build_client():
    """A live Claude client from ANTHROPIC_API_KEY (capped by the PIERRE_BUDGET_* variables when they are set)."""
    from pierre.agent.budget import client_from_env

    return client_from_env()


def _preflight(spec: dict, limit: int | None) -> None:
    """Print the size of the call list and the estimated input tokens (chars / 4); stop if it is over the cap."""
    from pierre.agents._claim_auditor import (
        _build_audit_prompt,
        _summarize_context_for_prompt,
    )

    fixtures = spec["fixtures"][:limit] if limit else spec["fixtures"]
    tokens = sum(
        len(_build_audit_prompt(fx["claim"], _summarize_context_for_prompt(spec["contexts"][fx["context_id"]]))) // 4
        for fx in fixtures
    )
    print(f"call list: {len(fixtures)} model calls; estimated input tokens (chars / 4): {tokens}")
    ledger, cap_calls, cap_tokens = (os.environ.get(k) for k in ("PIERRE_BUDGET_LEDGER", "PIERRE_BUDGET_MAX_CALLS", "PIERRE_BUDGET_MAX_INPUT_TOKENS"))
    if ledger and cap_calls and cap_tokens:
        used = json.loads(Path(ledger).read_text()) if Path(ledger).exists() else {"calls": 0, "est_input_tokens": 0}
        left_calls, left_tokens = int(cap_calls) - used["calls"], int(cap_tokens) - used["est_input_tokens"]
        print(f"budget left: {left_calls} calls / {left_tokens} estimated input tokens")
        if len(fixtures) > left_calls or tokens > left_tokens:
            raise SystemExit(f"FAIL: {len(fixtures)} calls / {tokens} tokens is over what is left of the cap.")


def _run_fixtures(spec: dict, client, limit: int | None) -> list:
    """Run the auditor over every fixture; return a list of result dicts."""
    from pierre.agents._claim_auditor import audit_report_claims

    contexts = spec["contexts"]
    fixtures = spec["fixtures"]
    if limit:
        fixtures = fixtures[:limit]

    results: list = []
    for i, fx in enumerate(fixtures):
        ctx = contexts[fx["context_id"]]
        res = audit_report_claims(fx["claim"], ctx, client)
        verdicts = [a.verdict for a in res.audits]
        eff = effective_verdict(verdicts)
        results.append(
            {
                "id": fx["id"],
                "label": fx["label"],
                "context_id": fx["context_id"],
                "claim": fx["claim"],
                "n_audits": len(res.audits),
                "sub_verdicts": verdicts,
                "effective_verdict": eff,
                "error": res.error,
            }
        )
        print(
            f"  [{i + 1}/{len(fixtures)}] {fx['id']:<28} "
            f"label={fx['label']:<14} effective={eff:<12} "
            f"(n_audits={len(res.audits)})"
        )
    return results


def _build_report(spec: dict, results: list) -> dict:
    """Compute the confusion matrix + metrics from per-fixture results."""
    pairs = [(r["label"], r["effective_verdict"]) for r in results]
    matrix = confusion_matrix(pairs)
    fp_rate = fabricated_false_positive_rate(pairs)
    recall = fabricated_recall(pairs)
    pr = per_verdict_precision_recall(pairs)

    # Surface the specific false-positive fixtures — these are the ones the
    # operator wants to eyeball (fair_inference / verified flagged fabricated).
    false_positives = [
        {"id": r["id"], "label": r["label"], "claim": r["claim"]}
        for r in results
        if r["label"] in ("verified", "fair_inference")
        and r["effective_verdict"] == "fabricated"
    ]
    no_claim = [r["id"] for r in results if r["effective_verdict"] == "no_claim"]

    return {
        "n_fixtures": len(results),
        "fp_rate_threshold": spec.get("fp_rate_threshold", 0.15),
        "recall_floor": spec.get("recall_floor", 0.8),
        "fabricated_false_positive_rate": fp_rate,
        "fabricated_recall": recall,
        "confusion_matrix": matrix,
        "per_verdict_precision_recall": pr,
        "false_positive_fixtures": false_positives,
        "no_claim_fixtures": no_claim,
        "errored_fixtures": [r["id"] for r in results if r.get("error")],
        "per_fixture": [
            {"id": r["id"], "label": r["label"], "effective_verdict": r["effective_verdict"]}
            for r in results
        ],
    }


def _print_report(report: dict) -> None:
    print("\n=== Claim-auditor calibration ===")
    print(f"fixtures: {report['n_fixtures']}")
    fp = report["fabricated_false_positive_rate"]
    rc = report["fabricated_recall"]
    print(
        f"HEADLINE fabricated FP-rate: "
        f"{'n/a' if fp is None else f'{fp:.3f}'} "
        f"(threshold ≤ {report['fp_rate_threshold']})"
    )
    print(f"fabricated recall: {'n/a' if rc is None else f'{rc:.3f}'}")
    print("\nconfusion matrix (rows=label, cols=effective verdict):")
    matrix = report["confusion_matrix"]
    cols = ["verified", "unverifiable", "fabricated", "no_claim"]
    print(f"  {'label':<16}" + "".join(f"{c:>14}" for c in cols))
    for label, row in matrix.items():
        print(f"  {label:<16}" + "".join(f"{row[c]:>14}" for c in cols))
    print("\nper-verdict precision / recall:")
    for verdict, m in report["per_verdict_precision_recall"].items():
        p = "n/a" if m["precision"] is None else f"{m['precision']:.2f}"
        r = "n/a" if m["recall"] is None else f"{m['recall']:.2f}"
        print(f"  {verdict:<14} precision={p:<6} recall={r:<6} (tp={m['tp']} fp={m['fp']} fn={m['fn']})")
    if report["false_positive_fixtures"]:
        print("\n⚠ false-positive fixtures (should survive, flagged fabricated):")
        for f in report["false_positive_fixtures"]:
            print(f"  - {f['id']} ({f['label']}): {f['claim']}")
    if report["no_claim_fixtures"]:
        print(f"\nno-claim fixtures (auditor extracted 0 claims): {report['no_claim_fixtures']}")


def _fmt(x, pct: bool = False) -> str:
    if x is None:
        return "n/a"
    return f"{x * 100:.1f}%" if pct else f"{x:.3f}"


def render_markdown(report: dict, *, captured_at: str | None = None) -> str:
    """Render a human-readable calibration report.

    Pure string assembly from the report/snapshot dict — no model call, no DB. The
    framing is meant to be shown to a client as trust validation:
    lead with the headline trust metric + an honest "this is a baseline, not a
    green-light" caveat, then the confusion matrix + the specific defects to
    retune. ``captured_at`` is threaded in (Date.now is unavailable in some
    runtimes; pass it from the caller / leave None).
    """
    fp = report.get("fabricated_false_positive_rate")
    recall = report.get("fabricated_recall")
    threshold = report.get("fp_rate_threshold")
    recall_floor = report.get("recall_floor")
    n = report.get("n_fixtures")
    matrix = report.get("confusion_matrix", {})
    pr = report.get("per_verdict_precision_recall", {})
    fps = report.get("false_positive_fixtures", [])

    # Label totals for the matrix header.
    def _row_total(lab):
        return sum(matrix.get(lab, {}).values())

    lines: list = []
    lines.append("# Claim-auditor calibration report")
    lines.append("")
    lines.append(
        "> **Auto-generated** by `scripts/analysis/claim_auditor_calibration_harness.py`. "
        "Do not hand-edit — re-run the harness (`--render-md` to refresh from the "
        "committed snapshot without a model call, or a full capture to re-measure)."
    )
    if captured_at:
        model = report.get("model")
        lines.append(">")
        lines.append(f"> Snapshot captured: **{captured_at}**" + (f" with `{model}`" if model else ""))
    lines.append("")
    lines.append("## What this measures")
    lines.append("")
    lines.append(
        "Our deep-report engine fact-checks every claim it writes against the "
        "structured evidence it was given, and labels each claim "
        "`verified` / `unverifiable` / `fabricated`. By default a `fabricated` claim is "
        "tagged `(fabricated)` and an `unverifiable` one `(unverified)` in the text; "
        "a stricter setting removes fabricated claims. This harness runs a **fixed set of test claims, written and labeled by Claude (not by hand)** "
        "through that auditor and scores how often it gets them right — the "
        "**groundedness calibration** of the engine."
    )
    lines.append("")
    lines.append(
        "The headline number is the **fabricated false-positive rate**: of the "
        "claims that are genuinely supported or fairly inferable (and so should "
        "survive), how many did the auditor wrongly flag as fabricated? Lower is "
        "better. Its counterweight is **recall**: of the genuinely fabricated "
        "claims, how many did it catch?"
    )
    lines.append("")
    lines.append("## Headline result")
    lines.append("")
    lines.append("| Metric | Result | Target | Meaning |")
    lines.append("|---|---|---|---|")
    lines.append(
        f"| **Fabricated false-positive rate** | **{_fmt(fp, pct=True)}** | ≤ {_fmt(threshold, pct=True)} | "
        f"share of legitimate claims wrongly flagged fabricated |"
    )
    lines.append(
        f"| **Fabricated recall** | **{_fmt(recall, pct=True)}** | ≥ {_fmt(recall_floor, pct=True)} | "
        f"share of real fabrications correctly caught |"
    )
    lines.append(f"| Test claims | {n} | — | fixtures written and labeled by Claude |")
    lines.append("")
    lines.append(
        "> **Read this as a baseline, not a green-light.** The target threshold is "
        "an internal bar, not an external standard, and a passing number here is "
        "the *precondition* for trusting the auditor in production — not proof it "
        "is fully calibrated. The defects below are the retune list."
    )
    n_legit = _row_total("verified") + _row_total("fair_inference")
    n_fab = _row_total("fabricated")
    if n_legit and n_fab and fp == 0.0 and recall == 1.0:
        # Rule of three: with zero errors in n trials, the 95% upper bound on the error rate is ~3/n.
        lines.append(">")
        lines.append(
            f"> **Sample size.** {n_legit} legitimate and {n_fab} fabricated claims, written and "
            f"labeled by the author. Zero errors on samples this small still allow a true "
            f"false-positive rate up to ~{3 / n_legit:.0%} and a true recall down to "
            f"~{1 - 3 / n_fab:.0%} (95%, rule of three)."
        )
        earlier_caught = [int(e["fabricated_caught"].split(" of ")[0]) for e in report.get("earlier_captures") or []]
        worst = min([n_fab] + earlier_caught)
        if worst < n_fab:
            z = 1.96
            low = (worst / n_fab + z * z / (2 * n_fab) - z * ((worst / n_fab) * (1 - worst / n_fab) / n_fab + z * z / (4 * n_fab**2)) ** 0.5) / (1 + z * z / n_fab)
            lines.append(">")
            lines.append(
                f"> **Run-to-run spread.** The worst capture listed below caught {worst} of {n_fab}; a Wilson 95% bound on "
                f"that result puts the true recall as low as ~{low:.0%}. Read the recall bound from the worst capture, not the best."
            )
    lines.append("")
    nc = report.get("no_claim_fixtures") or []
    if nc:
        lines.append("")
        lines.append(
            f"> **No claim extracted.** {', '.join(f'`{i}`' for i in nc)} returned no claim from the model "
            f"(no API error: `errored_fixtures` is {report.get('errored_fixtures')}). A fabricated fixture that returns "
            f"no claim counts as a miss; a legitimate one counts as not flagged."
        )
    earlier = report.get("earlier_captures") or []
    if earlier:
        lines.append("")
        lines.append("**Earlier captures** (kept so the run-to-run spread is visible; the model is not deterministic):")
        lines.append("")
        lines.append("| Captured | Model | False-positive rate | Fabricated caught | No-claim fixtures |")
        lines.append("|---|---|---|---|---|")
        for e in earlier:
            lines.append(
                f"| {e['captured_at']} | `{e['model']}` | {_fmt(e['fabricated_false_positive_rate'], pct=True)} | "
                f"{e['fabricated_caught']} | {', '.join(e['no_claim_fixtures']) or 'none'} |"
            )
    lines.append("")
    lines.append("## Confusion matrix")
    lines.append("")
    lines.append(
        "Rows = the ground-truth label (assigned by Claude, not by hand); columns = what the auditor "
        "actually decided. The diagonal-ish ideal: `verified`→verified, "
        "`fair_inference`→unverifiable (or verified — both survive), "
        "`fabricated`→fabricated."
    )
    lines.append("")
    lines.append("| Ground truth ↓ / Auditor → | verified | unverifiable | fabricated | no-claim |")
    lines.append("|---|---|---|---|---|")
    for lab in ("verified", "fair_inference", "fabricated"):
        row = matrix.get(lab, {})
        lines.append(
            f"| **{lab}** ({_row_total(lab)}) | {row.get('verified', 0)} | "
            f"{row.get('unverifiable', 0)} | {row.get('fabricated', 0)} | "
            f"{row.get('no_claim', 0)} |"
        )
    lines.append("")
    lines.append("Per-verdict precision / recall:")
    lines.append("")
    lines.append("| Verdict | Precision | Recall |")
    lines.append("|---|---|---|")
    for verdict in ("verified", "unverifiable", "fabricated"):
        m = pr.get(verdict, {})
        lines.append(f"| {verdict} | {_fmt(m.get('precision'))} | {_fmt(m.get('recall'))} |")
    lines.append("")
    lines.append("## Logged defects (the retune list)")
    lines.append("")
    if fps:
        lines.append(
            "Legitimate claims the auditor wrongly flagged `fabricated` (would be "
            "tagged or removed in a report). These are the targets to fix **before** "
            "enabling the production integrity gate:"
        )
        lines.append("")
        for f in fps:
            lines.append(f"- **{f['id']}** (`{f['label']}`): {f['claim']}")
    else:
        lines.append("_None on this run._")
    lines.append("")
    lines.append("## How to reproduce")
    lines.append("")
    lines.append("```bash")
    lines.append("# Full re-measure (needs an Anthropic key, no DB):")
    lines.append("python scripts/analysis/claim_auditor_calibration_harness.py")
    lines.append("# Gate before shipping an auditor change (needs an Anthropic key; exits 1 on regression):")
    lines.append("python scripts/analysis/claim_auditor_calibration_harness.py --check")
    lines.append("# Refresh THIS doc from the committed snapshot (no model call):")
    lines.append("python scripts/analysis/claim_auditor_calibration_harness.py --render-md")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def _write_markdown(report: dict, captured_at: str | None = None) -> None:
    _REPORT_MD_PATH.parent.mkdir(parents=True, exist_ok=True)
    _REPORT_MD_PATH.write_text(render_markdown(report, captured_at=captured_at) + "\n")
    rel = (
        _REPORT_MD_PATH.relative_to(Path.cwd())
        if _REPORT_MD_PATH.is_relative_to(Path.cwd())
        else _REPORT_MD_PATH
    )
    print(f"Wrote report → {rel}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--check",
        action="store_true",
        help="Compare to threshold, exit 1 if the fabricated FP-rate exceeds it. "
        "Does NOT require exact per-fixture reproduction (the model isn't fully "
        "deterministic at temp=0).",
    )
    ap.add_argument("--limit", type=int, default=None, help="Run only the first N fixtures (smoke).")
    ap.add_argument(
        "--render-md",
        action="store_true",
        help="Regenerate the human-readable markdown report from the committed "
        "snapshot JSON (no model call, no DB) and exit. Use to refresh the doc "
        "without re-measuring.",
    )
    args = ap.parse_args()

    # --render-md: no model call, no fixtures run — just re-render the doc from the
    # committed snapshot. Loud failure if the snapshot is missing.
    if args.render_md:
        if not _SNAPSHOT_PATH.exists():
            raise SystemExit(
                f"No snapshot at {_SNAPSHOT_PATH} — run a full capture first "
                f"(python scripts/analysis/claim_auditor_calibration_harness.py)."
            )
        snap = json.loads(_SNAPSHOT_PATH.read_text())
        _write_markdown(snap, captured_at=snap.get("captured_at"))
        return 0

    spec = json.loads(_FIXTURES_PATH.read_text())
    _preflight(spec, args.limit)
    client = _build_client()

    print(f"Running auditor over {len(spec['fixtures'])} fixtures "
          f"{'(limited to %d)' % args.limit if args.limit else ''}...")
    results = _run_fixtures(spec, client, args.limit)
    report = _build_report(spec, results)
    report["captured_at"] = datetime.now(timezone.utc).isoformat()
    report["model"] = DEFAULT_AUDIT_MODEL
    _print_report(report)

    fp = report["fabricated_false_positive_rate"]
    threshold = report["fp_rate_threshold"]
    recall = report["fabricated_recall"]
    recall_floor = report["recall_floor"]
    no_claim_ids = report["no_claim_fixtures"]

    if args.check:
        # Only treat --limit as a non-gating smoke run when it ACTUALLY limits
        # the set (limit < total). `--check --limit 42` on 42 fixtures is a full
        # run and must gate — otherwise it's a silent false-green footgun
        # (silent-failure-hunter follow-up).
        if args.limit and args.limit < len(spec["fixtures"]):
            print("\n--check with --limit is a smoke run; not gating.")
            return 0
        # Drift info (non-gating): compare per-fixture verdicts to snapshot.
        if _SNAPSHOT_PATH.exists():
            snap = json.loads(_SNAPSHOT_PATH.read_text())
            prev = {p["id"]: p["effective_verdict"] for p in snap.get("per_fixture", [])}
            drift = [
                (p["id"], prev.get(p["id"]), p["effective_verdict"])
                for p in report["per_fixture"]
                if p["id"] in prev and prev[p["id"]] != p["effective_verdict"]
            ]
            if drift:
                print(f"\nℹ per-fixture verdict drift vs snapshot ({len(drift)}) — info only:")
                for fid, was, now in drift:
                    print(f"    {fid}: {was} → {now}")

        # --- Run-VALIDITY gates (silent-failure-hunter findings #1 + #2) ---
        # A no_claim effective verdict means the auditor extracted ZERO claims
        # from a fixture — for THIS fixture set (every claim is non-trivial and
        # every context is non-empty) that can only be a model-call error / empty-
        # context short-circuit / unparseable-JSON, NOT a legitimate "no
        # checkable claim". Such an errored fixture is counted as a benign
        # result by the FP-rate denominator → errors would SILENTLY IMPROVE the
        # metric and let --check pass on a corrupted run. Fail the run instead.
        if no_claim_ids:
            print(
                f"\n❌ FAIL (invalid run): {len(no_claim_ids)} fixture(s) returned "
                f"no_claim — the auditor errored or extracted nothing. The FP-rate "
                f"is NOT trustworthy on this run (errors deflate it). Fix the "
                f"auditor error and re-run before gating. IDs: {no_claim_ids}"
            )
            return 1
        # A degenerate run with no verified/fair_inference fixtures processed
        # yields fp=None — that is NOT a pass, it's an absence of measurement.
        if fp is None:
            print(
                "\n❌ FAIL (degenerate run): fabricated FP-rate is None — no "
                "verified/fair_inference fixtures were scored. Cannot gate on an "
                "empty measurement."
            )
            return 1

        # --- Quality gates ---
        if fp > threshold:
            print(
                f"\n❌ FAIL: fabricated FP-rate {fp:.3f} > threshold {threshold}. "
                f"The auditor is stripping legitimate (verified/fair_inference) "
                f"claims. Retune the auditor prompt/thresholds (push borderline "
                f"cases fabricated → unverifiable) BEFORE enabling the prod gate."
            )
            return 1
        # Recall floor (finding #3): driving FP-rate down by loosening the
        # auditor must NOT silently tank its ability to catch real fabrications.
        # A blind auditor (flags nothing) would otherwise pass with FP-rate 0.
        if recall is None or recall < recall_floor:
            print(
                f"\n❌ FAIL: fabricated recall "
                f"{'n/a' if recall is None else f'{recall:.3f}'} < floor {recall_floor}. "
                f"The auditor is MISSING real fabrications — a low FP-rate here is "
                f"hollow. Tighten the auditor before trusting the FP-rate."
            )
            return 1
        # --- Per-fixture fabricated-class FN gate --------------------------
        # The aggregate recall floor (0.8) tolerates a SINGLE fabricated FN as
        # long as enough OTHER fabrications are caught — which is EXACTLY how
        # an earlier `p_fab_geo_absent_field` regression (fabricated →
        # unverifiable) shipped on a green run: recall fell 100% → 94.1%, still
        # above the 0.8 floor, so nothing gated. A missed fabrication
        # under-strips a real lie, so make it a HARD, per-fixture failure: ANY
        # fixture whose GROUND-TRUTH label is `fabricated` but whose effective
        # verdict is NOT `fabricated` fails --check: fail loudly, never
        # silently.
        #
        # Key off the SPEC's ground-truth label (NOT the stored snapshot's
        # effective_verdict). Reading the snapshot would let a bad re-capture
        # bake the regression into the baseline and keep the gate foolable —
        # which defeats the entire point of this gate.
        gt_labels = {fx["id"]: fx["label"] for fx in spec["fixtures"]}
        fabricated_fns = [
            (p["id"], p["effective_verdict"])
            for p in report["per_fixture"]
            if gt_labels.get(p["id"]) == "fabricated"
            and p["effective_verdict"] != "fabricated"
        ]
        if fabricated_fns:
            print(
                f"\n❌ FAIL (fabricated-class FN): {len(fabricated_fns)} fixture(s) "
                f"labeled `fabricated` did NOT score `fabricated` — a missed "
                f"fabrication under-strips a real lie. The aggregate recall floor "
                f"hides this (a single FN stays above the 0.8 floor); this "
                f"per-fixture gate does not. Re-tune the auditor so each is caught "
                f"as `fabricated` again before shipping:"
            )
            for fid, eff in fabricated_fns:
                print(f"    {fid}: expected fabricated, got {eff}")
            return 1
        print(
            f"\n✅ PASS: fabricated FP-rate {fp:.3f} ≤ {threshold}, "
            f"recall {recall:.3f} ≥ {recall_floor}, no errored fixtures."
        )
        return 0

    # Capture mode — warn loudly on no_claim (does NOT block the write so the
    # operator can still inspect the artifact), then write the snapshot.
    if no_claim_ids:
        print(
            f"\n⚠ WARNING: {len(no_claim_ids)} fixture(s) returned no_claim "
            f"(auditor errored or extracted nothing): {no_claim_ids}. The captured "
            f"FP-rate is deflated by these — re-run a clean capture before trusting "
            f"the snapshot. (--check would FAIL on this run.)"
        )
    if args.limit and args.limit < len(spec["fixtures"]):
        print("\n--limit smoke run: the snapshot and report are NOT written.")
        return 0
    if _SNAPSHOT_PATH.exists():
        # Carry the previous capture forward so the run-to-run spread stays visible in the report.
        prev = json.loads(_SNAPSHOT_PATH.read_text())
        caught = prev["confusion_matrix"]["fabricated"]
        report["earlier_captures"] = list(prev.get("earlier_captures", [])) + [
            {
                "captured_at": prev["captured_at"],
                "model": prev["model"],
                "fabricated_false_positive_rate": prev["fabricated_false_positive_rate"],
                "fabricated_recall": prev["fabricated_recall"],
                "fabricated_caught": f"{caught['fabricated']} of {sum(caught.values())}",
                "no_claim_fixtures": prev.get("no_claim_fixtures", []),
            }
        ]
    _SNAPSHOT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(f"\nWrote snapshot → {_SNAPSHOT_PATH.relative_to(Path.cwd()) if _SNAPSHOT_PATH.is_relative_to(Path.cwd()) else _SNAPSHOT_PATH}")
    # Also refresh the human-readable report so the doc
    # never lags the snapshot.
    _write_markdown(report, captured_at=report.get("captured_at"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
