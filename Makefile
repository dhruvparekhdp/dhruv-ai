# One-command setup and checks. CI calls the same targets.
#
#   make setup       create .venv and install requirements-dev.txt
#   make lint        ruff, against scripts/baselines/ruff.txt
#   make typecheck   mypy --strict, against scripts/baselines/mypy.txt
#   make test        pytest
#   make security    pip-audit, gitleaks, and npm audit when a package.json exists
#   make check       all four checks
#   make baseline    rewrite the lint and type baselines from the current tree
#   make format      ruff format (not part of make check; the tree is not formatted)
#
# PY is expanded when a recipe runs, so `make setup lint` sees the venv that
# setup just created. A missing venv falls back to PATH, which is how CI runs.

PYTHON ?= python3
PY = $(if $(wildcard .venv/bin/python),.venv/bin/python,$(PYTHON))

.PHONY: setup lint typecheck test security check baseline format

setup:
	$(PYTHON) -m venv .venv
	.venv/bin/python -m pip install -r requirements-dev.txt

lint:
	$(PY) -m scripts.baseline check-ruff

typecheck:
	$(PY) -m scripts.baseline check-mypy

test:
	$(PY) -m pytest -q

security:
	scripts/install-gitleaks.sh
	$(PY) -m scripts.pip_audit
	.tools/gitleaks detect --source . --redact --no-banner
	@if [ -f package.json ]; then \
		npm audit --audit-level=high; \
	else \
		echo "No package.json. The PWA is vanilla JS under app/static, so npm audit does not apply."; \
	fi

check: lint typecheck test security

baseline:
	$(PY) -m scripts.baseline update

format:
	$(if $(wildcard .venv/bin/ruff),.venv/bin/ruff,ruff) format .
