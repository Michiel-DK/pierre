"""Round-trip check for the local Langfuse dashboard (`make langfuse-check`; the stack must be up).

This replays one committed turn (no model call) with both span exporters at once (`jsonl,langfuse`), so both receive
the same spans with the same ids. It then reads the trace back through Langfuse's public API
(GET /api/public/v2/observations) and compares, for each span:
- the name,
- the parent,
- the token counts,
- whether the token counts are numbers (shown in the table, not failed on by itself: a non-number count fails the
  token comparison).

It prints the comparison as a markdown table and exits non-zero on any mismatch. pierre has no users: in normal use,
what Langfuse shows is a small scheduled canary of held-out cases. No credential is printed.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pierre.agent import obs  # noqa: E402
from pierre.agent.factory import RECORDINGS_DIR, replay_case  # noqa: E402

TOKEN_KEYS = ("gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens")


def read_env_file(path: Path) -> dict:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def fetch_observations(host: str, auth: str, trace_id: str) -> list:
    query = urllib.parse.urlencode({"traceId": trace_id, "fields": "core,basic,usage,model", "limit": 100})
    req = urllib.request.Request(f"{host}/api/public/v2/observations?{query}", headers={"Authorization": f"Basic {auth}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)["data"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--env-file", type=Path, required=True)
    ap.add_argument("--case", default="hq-and-ticket-quillmoor", help="a committed single-turn recording")
    ap.add_argument("--timeout", type=float, default=180.0, help="seconds to wait for the trace to be readable")
    ap.add_argument("--dump", type=Path, help="write the JSONL spans and the API readback here (outside the repo)")
    args = ap.parse_args()

    env = read_env_file(args.env_file)
    host = env.get("LANGFUSE_HOST", obs.LANGFUSE_DEFAULT_HOST).rstrip("/")
    spans_file = Path(tempfile.mkdtemp()) / "spans.jsonl"
    os.environ.update(
        PIERRE_OTEL_EXPORTER="jsonl,langfuse",
        PIERRE_OTEL_FILE=str(spans_file),
        LANGFUSE_HOST=host,
        LANGFUSE_PUBLIC_KEY=env["LANGFUSE_PUBLIC_KEY"],
        LANGFUSE_SECRET_KEY=env["LANGFUSE_SECRET_KEY"],
    )
    obs.reset()
    _, trace_path = replay_case(args.case)
    obs.reset()  # shut the provider down: every span has been exported
    byte_equal = trace_path.read_bytes() == (RECORDINGS_DIR / f"{args.case}.trace.jsonl").read_bytes()

    spans = [json.loads(x) for x in spans_file.read_text(encoding="utf-8").splitlines() if x.strip()]
    (trace_id,) = {s["trace_id"] for s in spans}
    auth = base64.b64encode(f"{env['LANGFUSE_PUBLIC_KEY']}:{env['LANGFUSE_SECRET_KEY']}".encode()).decode()

    started, observations = time.monotonic(), []
    while time.monotonic() - started < args.timeout:
        observations = fetch_observations(host, auth, trace_id)
        if len(observations) >= len(spans):
            break
        time.sleep(2)
    waited = time.monotonic() - started
    by_id = {o["id"]: o for o in observations}

    if args.dump:
        args.dump.mkdir(parents=True, exist_ok=True)
        (args.dump / "spans.jsonl").write_text(spans_file.read_text(encoding="utf-8"), encoding="utf-8")
        (args.dump / "observations.json").write_text(json.dumps(observations, indent=2, sort_keys=True), encoding="utf-8")

    print(f"case `{args.case}`: {len(spans)} spans in the JSONL file, {len(observations)} observations read back "
          f"from Langfuse after {waited:.1f}s; trace id {trace_id}")
    print(f"committed trace byte-equal after the replay: {byte_equal}\n")
    print("| span (JSONL) | Langfuse name | span_id = observation id | parent matches | Langfuse type | "
          "JSONL tokens in / out | Langfuse usageDetails | numbers | tokens match |")
    print("|---|---|---|---|---|---|---|---|---|")
    failures = [] if byte_equal else ["committed trace not byte-equal"]
    for s in sorted(spans, key=lambda x: (x["parent_span_id"] is not None, x["start_time_unix_nano"])):
        o = by_id.get(s["span_id"])
        tokens = tuple(s["attributes"].get(k) for k in TOKEN_KEYS)
        if o is None:
            failures.append(f"{s['name']}: no observation with id {s['span_id']}")
            print(f"| `{s['name']}` | missing | | | | {tokens[0]} / {tokens[1]} | | | |")
            continue
        parent_ok = (o.get("parentObservationId") or None) == s["parent_span_id"]
        usage = o.get("usageDetails") or {}
        numbers = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in usage.values()) if usage else None
        has_tokens = tokens[0] is not None
        tokens_ok = (usage.get("input"), usage.get("output")) == tokens if has_tokens and o.get("type") == "GENERATION" else None
        # Langfuse names a tool observation after gen_ai.tool.name rather than the span name (observed on 4.80.0).
        expected_names = {s["name"], s["attributes"].get("gen_ai.tool.name")} - {None}
        if o.get("name") not in expected_names:
            failures.append(f"{s['name']}: Langfuse name {o.get('name')!r}")
        if not parent_ok:
            failures.append(f"{s['name']}: parent {o.get('parentObservationId')!r} != {s['parent_span_id']!r}")
        if has_tokens and o.get("type") == "GENERATION" and not tokens_ok:
            failures.append(f"{s['name']}: usageDetails {usage} != {tokens}")
        print(f"| `{s['name']}` | `{o.get('name')}` | {o['id'] == s['span_id']} | {parent_ok} | {o.get('type')} | "
              f"{tokens[0]} / {tokens[1]} | `{json.dumps(usage, sort_keys=True)}` | {numbers} | {tokens_ok} |")
    extra = set(by_id) - {s["span_id"] for s in spans}
    if extra:
        failures.append(f"{len(extra)} observations with no matching span")
    roots = [o for o in observations if o.get("isRootObservation")]
    print(f"\nroot observation session id: {roots[0].get('sessionId') if roots else None!r} "
          f"(the spans' gen_ai.conversation.id: {next((s['attributes'].get('gen_ai.conversation.id') for s in spans if s['parent_span_id'] is None), None)!r})")
    totals = {o["id"]: (o.get("totalCost"), o.get("model")) for o in observations if o.get("totalCost")}
    print(f"observations with a cost from Langfuse's own price table: {len(totals)} {totals if totals else ''}")
    if failures:
        print("\nFAIL:\n- " + "\n- ".join(failures))
        return 1
    print("\nOK: every span arrived, with the same id, parent, name and token counts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
