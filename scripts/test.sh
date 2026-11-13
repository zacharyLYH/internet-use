#!/usr/bin/env bash
set -euo pipefail

started=0
if ! docker compose ps --status running --quiet searxng | grep -q .; then
    docker compose up -d --wait searxng
    started=1
fi

cleanup() {
    if [ "$started" = 1 ]; then docker compose down; fi
}
trap cleanup EXIT INT TERM

.venv/bin/mypy app tests
.venv/bin/pytest -q
