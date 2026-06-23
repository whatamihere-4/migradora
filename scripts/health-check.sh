#!/bin/sh
# Quick health check — run on the VPS from repo root.
set -e
cd "$(dirname "$0")/.."

echo "=== Containers ==="
docker compose ps -a

echo
echo "=== Orchestrator dashboard ==="
PORT="${WEBUI_PORT:-${DASHBOARD_PORT:-8080}}"
if [ -f .env ]; then
  # shellcheck disable=SC1091
  . ./.env 2>/dev/null || true
  PORT="${WEBUI_PORT:-${DASHBOARD_PORT:-8080}}"
fi
curl -sf "http://localhost:${PORT}/health" | head -c 500 && echo || echo " FAIL (port ${PORT})"

echo
echo "=== Queue ==="
docker compose exec -T orchestrator python -m migradora status 2>/dev/null || echo "(orchestrator not running)"

echo
echo "=== Downloads on disk ==="
du -sh data/downloads 2>/dev/null || echo "(no data/downloads)"

echo
echo "=== Recent logs ==="
docker compose logs orchestrator --tail 15 2>/dev/null || true
