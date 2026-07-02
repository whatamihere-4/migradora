#!/bin/sh
# Quick health check — run on the VPS from repo root.
set -e
cd "$(dirname "$0")/.."

echo "=== Containers ==="
docker compose ps -a

echo
echo "=== Orchestrator dashboard (inside container) ==="
if docker compose exec -T orchestrator sh -c \
  'curl -sf http://127.0.0.1:${WEBUI_PORT:-8080}/health' 2>/dev/null | head -c 500; then
  echo
else
  echo " FAIL (container health)"
fi

echo
echo "=== Caddy upstream (caddy_net) ==="
PORT="${WEBUI_PORT:-8080}"
if [ -f .env ]; then
  # shellcheck disable=SC1091
  . ./.env 2>/dev/null || true
  PORT="${WEBUI_PORT:-8080}"
fi
if docker network inspect caddy_net >/dev/null 2>&1; then
  if docker run --rm --network caddy_net curlimages/curl:8.5.0 -sf --max-time 5 \
    "http://migradora:${PORT}/health" >/dev/null 2>&1; then
    echo "http://migradora:${PORT}/health OK"
  else
    echo "FAIL — run ./scripts/check-caddy-upstream.sh"
  fi
else
  echo "caddy_net missing"
fi

echo
echo "=== Queue ==="
docker compose exec -T orchestrator python -m migradora status 2>/dev/null || echo "(orchestrator not running)"

echo
echo "=== Downloads on disk ==="
du -sh data/downloads 2>/dev/null || echo "(no data/downloads)"

echo
echo "=== Recent logs ==="
docker compose logs orchestrator --tail 15 2>/dev/null || true
