#!/bin/sh
# Probe Filester move/reparent API variants (safe on a test folder only).
set -e
cd "$(dirname "$0")/.."

FOLDER_ID="${1:?usage: $0 <folder-identifier> [parent-identifier]}"
PARENT_ID="${2:-${FILESTER_ROOT_FOLDER_ID:-558b65a42fdad1f6}}"

echo "=== Move probe: folder=$FOLDER_ID -> parent=$PARENT_ID ==="
docker compose exec -T orchestrator python -m migradora filester-probe \
  --probe-move \
  --folder-identifier "$FOLDER_ID" \
  --parent-identifier "$PARENT_ID" \
  --dry-run

echo ""
echo "Re-run without --dry-run to POST/PATCH (use a disposable test folder):" 
echo "  docker compose exec orchestrator python -m migradora filester-probe \\"
echo "    --probe-move --folder-identifier $FOLDER_ID --parent-identifier $PARENT_ID"
