#!/bin/sh
# One-shot upload speed vs Filester API pressure report.
set -e
cd "$(dirname "$0")/.."
exec docker compose exec -T orchestrator python -m migradora diag "$@"
