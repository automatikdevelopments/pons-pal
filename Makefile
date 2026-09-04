# SPDX-License-Identifier: MIT
# Pons Family - developer targets for pons.family
#
# `make check` is the local mirror of CI. Run it before every push: a red
# build should be found on the laptop, not in the pull request.

PYTHON ?= python3.11
VENV   ?= .venv
BIN    := $(VENV)/bin

.PHONY: venv install lint types test security check freeze run cycle state clean

venv:
	$(PYTHON) -m venv $(VENV)

install:
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -r requirements.txt
	$(BIN)/pip install -e ".[dev]"

lint:
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

types:
	$(BIN)/mypy --strict src

test:
	$(BIN)/pytest

security:
	$(BIN)/pip-audit -r requirements.txt
	$(BIN)/bandit -q -r src -c pyproject.toml

check: lint types test security

freeze:
	$(BIN)/pip freeze --exclude-editable > requirements.txt

run:
	$(BIN)/pons-pal run

cycle:
	$(BIN)/pons-pal cycle

state:
	$(BIN)/pons-pal state

clean:
	rm -rf .mypy_cache .ruff_cache .pytest_cache build dist src/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
