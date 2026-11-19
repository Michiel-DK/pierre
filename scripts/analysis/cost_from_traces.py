"""Re-derive the cost of the 13 Nov live eval from the committed traces. No model call, no network.

    python scripts/analysis/cost_from_traces.py            # print a summary
    python scripts/analysis/cost_from_traces.py --json     # the same as JSON
    python scripts/analysis/cost_from_traces.py --render-md  # write docs/observability/cost-of-the-13-nov-run.md

Tokens come from each turn's ``meta.usage`` (agent calls, priced at the turn's model) and ``meta.auditor_usage``
(the claim auditor, whose model is fixed at ``DEFAULT_AUDIT_MODEL`` whatever model the agent runs on: the traces do not
record it per call, so that is an assumption of this script). Prices: ``pierre/agent/prices.json``.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from pierre.agent import cost
from pierre.agents._claim_auditor import DEFAULT_AUDIT_MODEL

ROOT = Path(__file__).resolve().parents[2]
TRACES = ROOT / "data" / "agent_eval" / "traces"
SUMMARY = ROOT / "data" / "agent_eval" / "run_summary.json"
OUT = ROOT / "docs" / "observability" / "cost-of-the-13-nov-run.md"
KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def derive() -> dict:
    by = defaultdict(lambda: {"sessions": 0, "turns": 0, "agent_calls": 0, "auditor_calls": 0, "agent_usd": Decimal(0), "auditor_usd": Decimal(0), **{f"agent_{k}": 0 for k in KEYS}, **{f"auditor_{k}": 0 for k in KEYS}})
    for model_dir in sorted(TRACES.iterdir()):
        for path in sorted(model_dir.glob("*.trace.jsonl")):
            by[model_dir.name]["sessions"] += 1
            for line in path.read_text(encoding="utf-8").splitlines():
                rec = json.loads(line)
                row, meta = by[model_dir.name], rec["meta"]
                row["turns"] += 1
                for who, usages, model in (("agent", meta["usage"], rec["model"]), ("auditor", meta["auditor_usage"], DEFAULT_AUDIT_MODEL)):
                    for u in usages:
                        row[f"{who}_calls"] += 1
                        for k in KEYS:
                            row[f"{who}_{k}"] += int(u.get(k) or 0)
                        usd = cost._usd(model, u)
                        if usd is None:
                            raise SystemExit(f"no price row for {model}")
                        row[f"{who}_usd"] += usd
    total = sum((r["agent_usd"] + r["auditor_usd"] for r in by.values()), Decimal(0))
    ledger = json.loads(SUMMARY.read_text())["metered_ledger"]
    out_by = {m: {k: (round(float(v), 4) if isinstance(v, Decimal) else v) for k, v in r.items()} for m, r in by.items()}
    for m, r in out_by.items():
        r["total_usd"] = round(r["agent_usd"] + r["auditor_usd"], 4)
    return {
        "derived_from": "committed traces under data/agent_eval/traces (no model call)",
        "prices_as_of": cost.prices()["as_of"],
        "auditor_model_assumed": DEFAULT_AUDIT_MODEL,
        "turns": sum(r["turns"] for r in by.values()),
        "by_agent_model": out_by,
        "total_usd": round(float(total), 4),
        "model_calls_in_traces": sum(r["agent_calls"] + r["auditor_calls"] for r in by.values()),
        "model_calls_in_metered_ledger": ledger["calls"],
    }


def render(d: dict) -> str:
    L = [
        "# Cost of the 13 Nov live eval, re-derived from the committed traces",
        "",
        f"*derived from committed traces, prices as of {d['prices_as_of']}.* No model call was made to produce this page:",
        "`python scripts/analysis/cost_from_traces.py --render-md` reads `meta.usage` and `meta.auditor_usage` from",
        "`data/agent_eval/traces/` and prices the tokens with `pierre/agent/prices.json` (list price, standard tier).",
        f"The claim auditor is priced as `{d['auditor_model_assumed']}`, the fixed judge model; the traces do not record the judge per call.",
        "pierre has no users: this run was the fixed eval set (21 cases), not organic traffic.",
        "",
        "| agent model | sessions | turns | agent calls | auditor calls | agent USD | auditor USD | total USD |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for m, r in d["by_agent_model"].items():
        L.append(f"| {m} | {r['sessions']} | {r['turns']} | {r['agent_calls']} | {r['auditor_calls']} | {r['agent_usd']:.2f} | {r['auditor_usd']:.2f} | {r['total_usd']:.2f} |")
    L += [
        f"| **all** | | {d['turns']} | | | | | **{d['total_usd']:.2f}** |",
        "",
        f"Model calls counted in the traces: {d['model_calls_in_traces']}. The metered ledger in `run_summary.json` counts "
        f"{d['model_calls_in_metered_ledger']}; the {d['model_calls_in_traces'] - d['model_calls_in_metered_ledger']:+d} difference is not explained by the files in this repo.",
    ]
    return "\n".join(L) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--render-md", action="store_true")
    args = ap.parse_args()
    d = derive()
    if args.json:
        print(json.dumps(d, indent=1, sort_keys=True))
    elif args.render_md:
        OUT.write_text(render(d), encoding="utf-8")
        print(f"wrote {OUT.relative_to(ROOT)}")
    else:
        print(render(d))


if __name__ == "__main__":
    main()
