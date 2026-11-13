#!/usr/bin/env bash
set -euo pipefail

make stop || true
docker compose down
