#!/bin/sh
# Check web UI inside the container and on caddy_net — run from repo root.
set -e
cd "$(dirname "$0")/.."

if [ -f .env ]; then
  # shellcheck disable=SC1091
  . ./.env
fi
PORT="${WEBUI_PORT:-8080}"

echo "=== .env WEBUI_PORT ==="
echo "WEBUI_PORT=${PORT} (internal only — not published on host)"

echo
echo "=== Docker compose ==="
docker compose ps -a 2>/dev/null || true

echo
echo "=== Host listeners (should be empty for migradora) ==="
ss -tlnp 2>/dev/null | grep ":${PORT} " || netstat -tlnp 2>/dev/null | grep ":${PORT} " || echo "Nothing listening on host :${PORT} (expected)"

echo
echo "=== Inside container ==="
docker compose exec -T orchestrator sh -c \
  'echo WEBUI_PORT=$WEBUI_PORT; curl -sf http://127.0.0.1:${WEBUI_PORT:-8080}/health && echo health OK' 2>/dev/null \
  || echo "(container not running)"

echo
echo "=== From caddy_net (Caddy upstream) ==="
if docker network inspect caddy_net >/dev/null 2>&1; then
  if docker run --rm --network caddy_net curlimages/curl:8.5.0 -sf --max-time 5 \
    "http://migradora:${PORT}/health" >/dev/null 2>&1; then
    echo "http://migradora:${PORT}/health OK"
  else
    echo "FAIL — Caddy cannot reach migradora:${PORT}"
    echo "Run: ./scripts/check-caddy-upstream.sh"
  fi
else
  echo "caddy_net not found — run: docker network create caddy_net"
fi

echo
echo "Dashboard URL is via Caddy (not host :${PORT}). See docs/CADDY.md."
