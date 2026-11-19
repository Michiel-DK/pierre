# pierre showcase. Python 3.11+.
#
#   make install    venv with the package and test deps
#   make test       unit tests (offline: no network, no database, no API key)
#   make demo       rank the bundled fictional CSV against a criteria paragraph
#   make demo-agent replay two recorded agent sessions (offline, no key)
#   make record-agent  record the demo sessions from real model calls (needs ANTHROPIC_API_KEY, spends tokens)
#   make install-eval  the pinned wilco commit as an editable checkout (the grader; never vendored)
#   make agent-eval    the live agentic eval: run (needs ANTHROPIC_API_KEY, spends tokens, capped), grade with wilco, render the report
#   make anchor-eval MODEL=<id> ARM=<none|confirm-on-mutate|spotlight>  one defence arm of the public anchor (AgentDojo banking) through the agent loop (needs ANCHOR_ENV=<file with an ANTHROPIC_API_KEY line>, spends tokens; MODEL=gemma4:e2b also needs Ollama serving it). `--dry-run` on the script prints every pair's estimate, no call
#   make langfuse-up     start the local Langfuse dashboard (Docker; credentials generated into a git-ignored env file)
#   make langfuse-check  replay one committed turn into it (no model call) and compare the readback with the JSONL spans
#   make langfuse-down   stop it and remove its containers, volumes and env file
#   make canary      the scheduled canary: 5 fixed held-out cases, live (needs ANTHROPIC_API_KEY, spends tokens, capped), graded by wilco, one row appended to canary/metrics.jsonl
#   make canary FULL=1  the same on all 21 cases (when the model id or the SDK version changed)
#   make canary-check  judge the newest canary row against the series: the four alert rules, reasons written into its `alerts` (exit 1 a rule fired, 3 model/SDK changed: run make canary FULL=1; make itself reports any non-zero as 2)
#   make reports    re-render the calibration reports, the agent-eval report and the anchor report from their committed data; fail if any changes

PYTHON ?= python3
VENV   ?= .venv
PY     := $(VENV)/bin/python

.PHONY: canary-check install install-eval test demo demo-agent record-agent agent-eval anchor-eval canary reports langfuse-up langfuse-down langfuse-check

$(PY):
	$(PYTHON) -m venv $(VENV)

install: $(PY)
	$(PY) -m pip install -q -e ".[test,anchor,obs,langfuse]"

# Same 40-character commit as the [eval] extra in pyproject.toml. The wilco package is a shim over its checkout, so a plain
# install of the extra cannot grade; an editable VCS install keeps the checkout in $(VENV)/src. scripts/analysis/agent_eval.py
# refuses to grade when the checkout is not at the pinned commit.
WILCO_SHA := $(shell sed -n 's/.*wilco-exams @ git+https:\/\/github.com\/Michiel-DK\/wilco@\([0-9a-f]\{40\}\).*/\1/p' pyproject.toml)

install-eval: install
	$(PY) -m pip install -q -e "git+https://github.com/Michiel-DK/wilco@$(WILCO_SHA)#egg=wilco-exams"

test:
	GOOGLE_API_KEY= env -u ANTHROPIC_API_KEY -u PIERRE_OTEL_EXPORTER -u PIERRE_OTEL_FILE -u LANGFUSE_HOST -u LANGFUSE_PUBLIC_KEY -u LANGFUSE_SECRET_KEY $(PY) -m pytest -q

demo:
	$(PY) scripts/demo/byodata_demo.py

demo-agent:
	env -u ANTHROPIC_API_KEY $(PY) scripts/demo/agent_demo.py

record-agent:
	$(PY) scripts/demo/agent_record.py

anchor-eval:
	@test -n "$(ANCHOR_ENV)" || { echo "set ANCHOR_ENV=<env file with an ANTHROPIC_API_KEY line>"; exit 2; }
	$(PY) scripts/analysis/public_anchor_agentdojo.py --env-file $(ANCHOR_ENV) $(if $(MODEL),--model $(MODEL)) $(if $(ARM),--arm $(ARM))

agent-eval: install-eval
	$(PY) scripts/analysis/agent_eval.py run
	$(PY) scripts/analysis/agent_eval.py grade
	$(PY) scripts/analysis/agent_eval.py render-md

reports:
	$(PY) scripts/analysis/claim_auditor_calibration_harness.py --render-md
	$(PY) scripts/analysis/competitor_threshold_calibration_harness.py --render-md
	$(PY) scripts/analysis/render_threshold_reachability.py
	$(PY) scripts/analysis/agent_context_spot_check.py --render-md
	$(PY) scripts/analysis/public_anchor_agentdojo.py --render-md
	$(PY) scripts/analysis/agent_eval.py render-md
	git diff --exit-code -- docs/calibration/

canary: install-eval
	$(PY) scripts/analysis/canary.py run $(if $(FULL),--full)

canary-check:
	$(PY) scripts/analysis/canary_check.py

# The local Langfuse stack is optional: `make test` and CI never need it (docs/observability/langfuse.md).
langfuse-up:
	@scripts/observability/langfuse_stack.sh up

langfuse-down:
	@scripts/observability/langfuse_stack.sh down

langfuse-check:
	@PY=$(PY) scripts/observability/langfuse_stack.sh check $(LANGFUSE_CHECK_ARGS)
