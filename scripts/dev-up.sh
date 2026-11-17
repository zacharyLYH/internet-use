#!/usr/bin/env bash
set -euo pipefail

cleanup() {
    make stop >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

make start
