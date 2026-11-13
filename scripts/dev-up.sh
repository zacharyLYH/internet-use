#!/usr/bin/env bash
set -euo pipefail

cleanup() {
    make stop >/dev/null 2>&1 || true
    docker compose down
}
trap cleanup EXIT INT TERM

docker compose up -d --wait searxng
make start
