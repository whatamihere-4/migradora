#!/bin/sh
# Probe Real-Debrid downloads + torrents for filename match against migradora queue.
# Requires REAL_DEBRID_API_TOKEN in .env or environment.
set -e
cd "$(dirname "$0")/.."

if [ -z "${REAL_DEBRID_API_TOKEN:-}" ] && [ -f .env ]; then
  set -a
  # shellcheck source=/dev/null
  . ./.env
  set +a
fi

if docker compose ps orchestrator 2>/dev/null | grep -q Up; then
  docker compose exec -T \
    -e REAL_DEBRID_API_TOKEN="${REAL_DEBRID_API_TOKEN:-}" \
    -e REAL_DEBRID_API_BASE="${REAL_DEBRID_API_BASE:-}" \
  orchestrator python /app/scripts/probe-realdebrid-match.py "$@"
else
  if ! python3 -c "import httpx" 2>/dev/null; then
    echo "Install httpx: pip install httpx"
    exit 1
  fi
  export DB_PATH="${DB_PATH:-./data/state/queue.db}"
  python3 scripts/probe-realdebrid-match.py "$@"
fi
