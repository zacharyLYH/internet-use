#!/usr/bin/env bash
set -euo pipefail

cleanup() {
    docker compose down
}
trap cleanup EXIT INT TERM

if ! docker compose ps --status running --quiet searxng | grep -q .; then
    docker compose up -d --wait searxng
fi

.venv/bin/mypy app tests
.venv/bin/pytest -q
