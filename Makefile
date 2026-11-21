VENV := .venv
PY := $(VENV)/bin/python
PYTHON := $(shell command -v python3.12 2>/dev/null || (command -v uv >/dev/null 2>&1 && uv python find 3.12 2>/dev/null) || echo python3.12)
SEARXNG_URL ?= http://localhost:49731
RELOAD ?= --reload
APP_HOST ?= 127.0.0.1
APP_PORT ?= 8000
BROWSER_PROFILE_DIR ?= data/browser-profile
TRACE_DB_PATH ?= data/traces.sqlite3
API_KEYS_FILE ?= data/api_keys.txt
LLM_ENV_FILE ?= data/llm.env
STT_ENV_FILE ?= data/stt.env

.PHONY: install start stop searxng api-key llm-config stt-config test browser-profile

$(VENV):
	$(PYTHON) -m venv $(VENV)

install: $(VENV)
	$(PY) -m pip install -e ".[dev]"

api-key:
	@mkdir -p "$(dir $(API_KEYS_FILE))"
	@if [ ! -s "$(API_KEYS_FILE)" ]; then \
		key="$$($(PYTHON) -c 'import secrets; print(secrets.token_urlsafe(32))')"; \
		printf '%s\n' "$$key" > "$(API_KEYS_FILE)"; \
		chmod 600 "$(API_KEYS_FILE)"; \
		echo "Created $(API_KEYS_FILE). Bearer key: $$key"; \
	fi

searxng:
	@command -v docker >/dev/null 2>&1 || { echo "docker is required to run SearXNG"; exit 1; }
	@docker compose up -d --wait searxng

start: install api-key searxng llm-config stt-config
	@command -v ffmpeg >/dev/null 2>&1 || echo "warning: ffmpeg not found; media transcription will return descriptions only"
	@set -a; if [ -f "$(LLM_ENV_FILE)" ]; then . "$(LLM_ENV_FILE)"; fi; if [ -f "$(STT_ENV_FILE)" ]; then . "$(STT_ENV_FILE)"; fi; set +a; \
		SEARXNG_URL=$(SEARXNG_URL) TRACE_DB_PATH=$(TRACE_DB_PATH) API_KEYS_FILE=$(API_KEYS_FILE) \
		$(VENV)/bin/uvicorn app.main:app --host $(APP_HOST) --port $(APP_PORT) $(RELOAD)

llm-config: install
	@LLM_ENV_FILE="$(LLM_ENV_FILE)" $(PY) scripts/configure_llm.py

stt-config: install
	@STT_ENV_FILE="$(STT_ENV_FILE)" $(PY) scripts/configure_stt.py

stop:
	-@pkill -f "uvicorn app.main:app"
	-@if command -v docker >/dev/null 2>&1; then docker compose down; fi

test: install
	./scripts/test.sh

browser-profile: install
	BROWSER_PROFILE_DIR="$(BROWSER_PROFILE_DIR)" $(PY) scripts/open_browser_profile.py
