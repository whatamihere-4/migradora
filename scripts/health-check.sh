#!/bin/sh
# Quick health check — run on the VPS from repo root.
set -e
cd "$(dirname "$0")/.."

COMPOSE="docker compose"
if [ -f docker-compose.vpn.yml ] && docker compose -f docker-compose.yml -f docker-compose.vpn.yml --profile vpn ps -q gluetun 2>/dev/null | grep -q .; then
  COMPOSE="docker compose -f docker-compose.yml -f docker-compose.vpn.yml --profile vpn"
fi

echo "=== Containers ==="
$COMPOSE ps -a || docker compose ps -a

echo
echo "=== JD2 API ==="
curl -sf http://localhost:3128/help >/dev/null && echo "OK (port 3128)" || echo "FAIL — run ./scripts/jd2-enable-api.sh && restart jdownloader"

echo
echo "=== Orchestrator dashboard ==="
PORT="${DASHBOARD_PORT:-8080}"
if [ -f .env ]; then
  # shellcheck disable=SC1091
  . ./.env 2>/dev/null || true
  PORT="${DASHBOARD_PORT:-8080}"
fi
curl -sf "http://localhost:${PORT}/health" && echo " OK (port ${PORT})" || echo " FAIL (port ${PORT})"

echo
echo "=== Queue (if orchestrator up) ==="
$COMPOSE exec -T orchestrator python -m migradora status 2>/dev/null || echo "(orchestrator not running)"

echo
echo "=== Downloads on disk ==="
du -sh data/downloads 2>/dev/null || echo "(no data/downloads)"
find data/downloads -type f ! -name '*.part' 2>/dev/null | head -10 || true

echo
echo "=== Recent orchestrator logs ==="
$COMPOSE logs orchestrator --tail 15 2>/dev/null || true
