# pierre showcase. Python 3.11+.
#
#   make install    venv with the package and test deps
#   make test       unit tests (offline: no network, no database, no API key)
#   make demo       rank the bundled fictional CSV against a criteria paragraph
#   make reports    re-render the three calibration reports from their snapshots; fail if any changes

PYTHON ?= python3
VENV   ?= .venv
PY     := $(VENV)/bin/python

.PHONY: install test demo reports

$(PY):
	$(PYTHON) -m venv $(VENV)

install: $(PY)
	$(PY) -m pip install -q -e ".[test]"

test:
	GOOGLE_API_KEY= $(PY) -m pytest -q

demo:
	$(PY) scripts/demo/byodata_demo.py

reports:
	$(PY) scripts/analysis/claim_auditor_calibration_harness.py --render-md
	$(PY) scripts/analysis/competitor_threshold_calibration_harness.py --render-md
	$(PY) scripts/analysis/render_threshold_reachability.py
	git diff --exit-code -- docs/calibration/
