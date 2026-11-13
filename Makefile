# pierre showcase. Python 3.11+.
#
#   make install    venv with the package and test deps
#   make test       unit tests (offline: no network, no database, no API key)
#   make demo       rank the bundled fictional CSV against a criteria paragraph
#   make demo-agent replay two recorded agent sessions (offline, no key)
#   make record-agent  record the demo sessions from real model calls (needs ANTHROPIC_API_KEY, spends tokens)
#   make install-eval  the pinned wilco commit as an editable checkout (the grader; never vendored)
#   make agent-eval    the live agentic eval: run (needs ANTHROPIC_API_KEY, spends tokens, capped), grade with wilco, render the report
#   make anchor-eval  the public anchor: an AgentDojo banking slice through the agent loop (needs ANCHOR_ENV=<file with an ANTHROPIC_API_KEY line>, spends tokens)
#   make reports    re-render the calibration reports, the agent-eval report and the anchor report from their committed data; fail if any changes

PYTHON ?= python3
VENV   ?= .venv
PY     := $(VENV)/bin/python

.PHONY: install install-eval test demo demo-agent record-agent agent-eval anchor-eval reports

$(PY):
	$(PYTHON) -m venv $(VENV)

install: $(PY)
	$(PY) -m pip install -q -e ".[test,anchor]"

# Same 40-character commit as the [eval] extra in pyproject.toml. The wilco package is a shim over its checkout, so a plain
# install of the extra cannot grade; an editable VCS install keeps the checkout in $(VENV)/src. scripts/analysis/agent_eval.py
# refuses to grade when the checkout is not at the pinned commit.
WILCO_SHA := $(shell sed -n 's/.*wilco-exams @ git+https:\/\/github.com\/Michiel-DK\/wilco@\([0-9a-f]\{40\}\).*/\1/p' pyproject.toml)

install-eval: install
	$(PY) -m pip install -q -e "git+https://github.com/Michiel-DK/wilco@$(WILCO_SHA)#egg=wilco-exams"

test:
	GOOGLE_API_KEY= env -u ANTHROPIC_API_KEY $(PY) -m pytest -q

demo:
	$(PY) scripts/demo/byodata_demo.py

demo-agent:
	env -u ANTHROPIC_API_KEY $(PY) scripts/demo/agent_demo.py

record-agent:
	$(PY) scripts/demo/agent_record.py

anchor-eval:
	@test -n "$(ANCHOR_ENV)" || { echo "set ANCHOR_ENV=<env file with an ANTHROPIC_API_KEY line>"; exit 2; }
	$(PY) scripts/analysis/public_anchor_agentdojo.py --env-file $(ANCHOR_ENV)

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
