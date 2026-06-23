#!/bin/sh
# Check web UI port binding — run on the VPS from repo root.
set -e
cd "$(dirname "$0")/.."

if [ -f .env ]; then
  # shellcheck disable=SC1091
  . ./.env
fi
PORT="${WEBUI_PORT:-8080}"

echo "=== .env WEBUI_PORT ==="
echo "WEBUI_PORT=${PORT}"

echo
echo "=== Docker compose port map ==="
docker compose ps -a 2>/dev/null || true
docker compose port orchestrator "${PORT}" 2>/dev/null || echo "(orchestrator not publishing :${PORT})"

echo
echo "=== Host listeners ==="
ss -tlnp 2>/dev/null | grep ":${PORT} " || netstat -tlnp 2>/dev/null | grep ":${PORT} " || echo "Nothing listening on :${PORT}"

echo
echo "=== Local curl ==="
curl -sf "http://127.0.0.1:${PORT}/health" && echo " OK" || echo "FAIL — app not reachable on 127.0.0.1:${PORT}"

echo
echo "=== Container env + logs ==="
docker compose exec -T orchestrator sh -c 'echo WEBUI_PORT=$WEBUI_PORT; curl -sf http://127.0.0.1:${WEBUI_PORT:-8080}/health && echo health OK' 2>/dev/null \
  || echo "(container not running)"

echo
echo "=== Tailscale (if installed) ==="
if command -v tailscale >/dev/null 2>&1; then
  tailscale serve status 2>/dev/null || true
  echo "MagicDNS: http://$(tailscale ip -4 2>/dev/null):${PORT}/"
else
  echo "tailscale CLI not found"
fi

echo
echo "If local curl OK but Tailscale URL fails, update serve/funnel to port ${PORT}:"
echo "  tailscale serve --bg http://127.0.0.1:${PORT}"
echo "  # or funnel: tailscale funnel --bg http://127.0.0.1:${PORT}"
