VENV := .venv
PY := $(VENV)/bin/python
PYTHON := python3.12
SEARXNG_URL ?= http://localhost:49731
RELOAD ?= --reload

.PHONY: install start stop test

$(VENV):
	$(PYTHON) -m venv $(VENV)

install: $(VENV)
	$(PY) -m pip install -e ".[dev]"

start: install
	SEARXNG_URL=$(SEARXNG_URL) $(VENV)/bin/uvicorn app.main:app $(RELOAD)

stop:
	-@pkill -f "uvicorn app.main:app"

test: install
	./scripts/test.sh
