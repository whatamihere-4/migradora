#!/bin/sh
# Test whether Caddy can reach migradora on caddy_net (diagnose HTTPS 502).
set -e
cd "$(dirname "$0")/.."

if [ -f .env ]; then
  # shellcheck disable=SC1091
  . ./.env
fi

PORT="${WEBUI_PORT:-8080}"
HOST="${UPSTREAM_HOST:-migradora}"

echo "=== Migradora container networks ==="
if docker inspect migradora-orchestrator >/dev/null 2>&1; then
  docker inspect migradora-orchestrator --format '{{range $k,$v := .NetworkSettings.Networks}}  {{$k}}{{"\n"}}{{end}}'
  if docker inspect migradora-orchestrator --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' | grep -q caddy_net; then
    echo "caddy_net: attached"
  else
    echo "caddy_net: MISSING — recreate with:"
    echo "  docker compose -f docker-compose.yml -f docker-compose.caddy.yml up -d --force-recreate"
  fi
else
  echo "migradora-orchestrator container not found"
  exit 1
fi

echo
echo "=== Inside container (WEBUI_PORT) ==="
docker compose exec -T orchestrator sh -c \
  'echo WEBUI_PORT=${WEBUI_PORT:-8080}; curl -sf http://127.0.0.1:${WEBUI_PORT:-8080}/health && echo health OK' \
  || echo "FAIL — app not listening on container WEBUI_PORT"

echo
echo "=== From caddy_net (what Caddy uses) ==="
for try in "$HOST" migradora migradora-orchestrator orchestrator; do
  printf '  http://%s:%s/health ... ' "$try" "$PORT"
  if docker run --rm --network caddy_net curlimages/curl:8.5.0 -sf --max-time 5 \
    "http://${try}:${PORT}/health" >/dev/null 2>&1; then
    echo "OK"
  else
    echo "FAIL"
  fi
done

echo
echo "Caddy .env should have: UPSTREAM_3=${HOST}:${PORT}"
echo "Migradora .env should have: WEBUI_PORT=${PORT}"
