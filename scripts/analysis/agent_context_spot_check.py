"""A 12-claim spot check of the claim auditor on contexts built from agent tool results.

The 57-claim calibration measures the auditor on single-firm report contexts. The agent hands it a different
kind of context, built from tool results (pierre/agent/_audit_context.py). This script runs 12 claims over such
contexts: 6 true by the planted data and 6 false by it. The labels come from the data in data/agent/firms.json,
not from a model. The result is reported beside the 57-claim number and is not merged with it.

    python scripts/analysis/agent_context_spot_check.py             # run the auditor (needs a key), write the snapshot
    python scripts/analysis/agent_context_spot_check.py --render-md  # re-render the report from the snapshot
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pierre.agent._audit_context import audit_context_from_turn  # noqa: E402
from pierre.agent.backend import InMemoryFirmStore  # noqa: E402
from pierre.agent.registry import ToolContext, bind_tools  # noqa: E402
from pierre.agent.tools import ToolDispatcher  # noqa: E402
from pierre.agents._claim_auditor import DEFAULT_AUDIT_MODEL, audit_report_claims  # noqa: E402
from pierre.agents._claim_auditor_calibration import effective_verdict  # noqa: E402

SNAPSHOT = ROOT / "tests" / "golden" / "agent_context_spot_check_snapshot.json"
REPORT = ROOT / "docs" / "calibration" / "agent_context_spot_check_report.md"

CONTEXTS = {
    "quillmoor": [("lookup_firm", {"firm_name": "Quillmoor Capital"})],
    "brask_gravick": [("lookup_firm", {"firm_name": "Brask Ventures"}), ("compare_firms", {"firm_ids": ["brask-ventures", "gravick-partners"]})],
    "merrowfield_2024": [
        ("filter_portfolio_by_year", {"investment_year": 2024, "firm_ids": ["merrowfield-ventures"]}),
        ("find_recent_investments", {"sector": "climate"}),
    ],
    "raise_fit": [("check_raise_fit", {"stage": "Seed", "sector": "fintech", "geography": "UK", "amount_min": 4000000, "amount_max": 5000000})],
    "shortlist": [("add_to_shortlist", {"firm_slug": "brask-ventures"}), ("list_shortlist", {})],
    "ferrin_error": [("get_portfolio_stats", {"firm_ids": ["ferrin-lane-capital"]}), ("lookup_firm", {"firm_name": "Ferrin Lane Capital"})],
}

# (id, context, label, claim). A "true" label means the planted data supports the claim; "false" means it contradicts it.
CLAIMS = [
    ("t1_hq_cheque", "quillmoor", "true", "Quillmoor Capital is headquartered in London and writes cheques between EUR 500,000 and EUR 3,000,000."),
    ("t2_portfolio_count", "quillmoor", "true", "Quillmoor Capital has six portfolio companies on record."),
    ("t3_shared_stage", "brask_gravick", "true", "Brask Ventures and Gravick Partners both invest at Series A."),
    ("t4_2024_companies", "merrowfield_2024", "true", "Merrowfield Ventures invested in Umbrevia Bio and Bexmirra Materials in 2024."),
    ("t5_shortlist_added", "shortlist", "true", "Brask Ventures has been added to the shortlist."),
    ("t6_error_reported", "ferrin_error", "true", "The portfolio statistics call for Ferrin Lane Capital failed, so no statistics are available."),
    ("f1_wrong_hq", "quillmoor", "false", "Quillmoor Capital is headquartered in Paris."),
    ("f2_wrong_founded", "quillmoor", "false", "Quillmoor Capital was founded in 2009."),
    ("f3_wrong_cheque", "brask_gravick", "false", "Gravick Partners writes cheques of up to EUR 40 million."),
    ("f4_wrong_company_year", "merrowfield_2024", "false", "Merrowfield Ventures invested in Saltmere Energy in 2024."),
    ("f5_wrong_fit", "raise_fit", "false", "A EUR 4,000,000 to EUR 5,000,000 raise falls inside Quillmoor Capital's cheque range."),
    ("f6_wrong_shortlist", "shortlist", "false", "Orrindale Fund has been added to the shortlist."),
]


def build_context(name: str) -> dict:
    dispatcher = ToolDispatcher(bind_tools(ToolContext(InMemoryFirmStore())))
    calls = [{"tool": t, "args": a} for t, a in CONTEXTS[name]]
    return audit_context_from_turn(calls, dispatcher.execute_parallel(CONTEXTS[name]))


def preflight() -> None:
    from pierre.agents._claim_auditor import _build_audit_prompt, _summarize_context_for_prompt

    tokens = sum(len(_build_audit_prompt(c[3], _summarize_context_for_prompt(build_context(c[1])))) // 4 for c in CLAIMS)
    print(f"call list: {len(CLAIMS)} model calls; estimated input tokens (chars / 4): {tokens}")


def run(client) -> dict:
    rows = []
    for cid, ctx_name, label, claim in CLAIMS:
        res = audit_report_claims(claim, build_context(ctx_name), client)
        verdicts = [a.verdict for a in res.audits]
        rows.append({"id": cid, "label": label, "claim": claim, "sub_verdicts": verdicts,
                     "effective_verdict": effective_verdict(verdicts), "error": res.error})
    true_rows = [r for r in rows if r["label"] == "true"]
    false_rows = [r for r in rows if r["label"] == "false"]
    return {
        "n_claims": len(rows),
        "model": DEFAULT_AUDIT_MODEL,
        "captured_at": date.today().isoformat(),
        "true_flagged_fabricated": [r["id"] for r in true_rows if r["effective_verdict"] == "fabricated"],
        "true_verdict_counts": {v: sum(r["effective_verdict"] == v for r in true_rows) for v in ("verified", "unverifiable", "fabricated", "no_claim")},
        "false_caught": [r["id"] for r in false_rows if r["effective_verdict"] == "fabricated"],
        "false_verdict_counts": {v: sum(r["effective_verdict"] == v for r in false_rows) for v in ("verified", "unverifiable", "fabricated", "no_claim")},
        "per_claim": rows,
    }


def render(snap: dict) -> str:
    t, f = snap["true_verdict_counts"], snap["false_verdict_counts"]
    lines = [
        "# Claim auditor on agent-built contexts: 12-claim spot check",
        "",
        "> **Auto-generated** by `scripts/analysis/agent_context_spot_check.py --render-md` from "
        "`tests/golden/agent_context_spot_check_snapshot.json`. Do not hand-edit.",
        f"> Snapshot captured: **{snap['captured_at']}** with `{snap['model']}`.",
        "",
        "The 57-claim calibration (`claim_auditor_calibration_report.md`) measures the auditor on single-firm report "
        "contexts. The agent gives it a context built from tool results, which is a different distribution. This "
        "spot check runs 12 claims over such contexts: 6 true by the planted data and 6 false by it. The labels come "
        "from `data/agent/firms.json`, not from a model. It is reported beside the 57-claim number and is not merged with it.",
        "",
        "| | claims | `verified` | `unverifiable` | `fabricated` | no claim extracted |",
        "|---|---|---|---|---|---|",
        f"| true by the data | 6 | {t['verified']} | {t['unverifiable']} | {t['fabricated']} | {t['no_claim']} |",
        f"| false by the data | 6 | {f['verified']} | {f['unverifiable']} | {f['fabricated']} | {f['no_claim']} |",
        "",
        f"True claims flagged `fabricated`: **{len(snap['true_flagged_fabricated'])} of 6**. "
        f"False claims caught as `fabricated`: **{len(snap['false_caught'])} of 6**.",
        "",
        "Twelve claims cannot bound a rate. The counts show whether the auditor works on this kind of context at all; "
        "they are not a calibrated error rate, and they do not replace the 57-claim bound.",
        "",
        "| id | label | effective verdict | claim |",
        "|---|---|---|---|",
    ]
    lines += [f"| {r['id']} | {r['label']} | {r['effective_verdict']} | {r['claim']} |" for r in snap["per_claim"]]
    lines += ["", "Re-run with a key: `python scripts/analysis/agent_context_spot_check.py`.", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--render-md", action="store_true")
    args = ap.parse_args()
    if args.render_md:
        REPORT.write_text(render(json.loads(SNAPSHOT.read_text())))
        return 0
    from pierre.agent.budget import client_from_env

    preflight()
    snap = run(client_from_env())
    if any(r["error"] or r["effective_verdict"] == "no_claim" for r in snap["per_claim"]):
        print("WARNING: some claims errored or returned no claim; see the snapshot.", file=sys.stderr)
    SNAPSHOT.write_text(json.dumps(snap, indent=2, ensure_ascii=False) + "\n")
    REPORT.write_text(render(snap))
    print(f"true flagged fabricated: {snap['true_flagged_fabricated']}; false caught: {snap['false_caught']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
