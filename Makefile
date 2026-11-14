VENV := .venv
PY := $(VENV)/bin/python
PYTHON := python3.12
SEARXNG_URL ?= http://localhost:49731
RELOAD ?= --reload
BROWSER_PROFILE_DIR ?= data/browser-profile

.PHONY: install start stop test browser-profile

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

browser-profile: install
	BROWSER_PROFILE_DIR="$(BROWSER_PROFILE_DIR)" $(PY) scripts/open_browser_profile.py
