.PHONY: help venv install test serve clean

PY ?= python3
VENV ?= venv
BIN := $(VENV)/bin
HOST ?= 0.0.0.0
PORT ?= 8000

help:
	@echo "Mad — available targets:"
	@echo "  make venv      Create the $(VENV)/ virtualenv"
	@echo "  make install   Install the mad package (editable) + dev deps"
	@echo "  make test      Run the pytest suite"
	@echo "  make serve     Run uvicorn on $(HOST):$(PORT) (override with HOST=/PORT=)"
	@echo "  make clean     Remove caches, build artifacts, and sessions/"

venv:
	$(PY) -m venv $(VENV)

install: venv
	$(BIN)/pip install -U pip
	$(BIN)/pip install -e '.[dev]'

test:
	$(BIN)/pytest -q

serve:
	$(BIN)/uvicorn mad.api.app:create_app --factory --host $(HOST) --port $(PORT)

clean:
	rm -rf .pytest_cache **/__pycache__ build dist *.egg-info sessions
