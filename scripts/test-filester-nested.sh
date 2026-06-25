#!/bin/sh
# Test nested folder create under VR without creating a root folder first.
set -e
cd "$(dirname "$0")/.."

VR_ID="${FILESTER_ROOT_FOLDER_ID:-558b65a42fdad1f6}"
NAME="migradora-nested-$(date +%s)"

echo "=== Nested folder probe: $NAME under VR ($VR_ID) ==="
docker compose exec -T orchestrator python -m migradora filester-probe \
  --nested-only \
  --parent-identifier "$VR_ID" \
  --name "$NAME"
