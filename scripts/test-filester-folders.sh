#!/bin/sh
# Probe Filester folder API: list folders, test create variants, run production create_folder().
set -e
cd "$(dirname "$0")/.."

NAME="${1:-CzechVR}"
shift || true

echo "=== Filester folder probe (search/create: $NAME) ==="
docker compose exec -T orchestrator python -m migradora filester-probe --search "$NAME" "$@"
