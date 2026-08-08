#!/bin/sh
# Probe Real-Debrid downloads + torrents for filename match against migradora queue.
# Requires REAL_DEBRID_API_TOKEN in .env or environment.
#
# Runs on the VPS HOST by default (container often can't reach api.real-debrid.com).
# To force container: PROBE_RD_IN_CONTAINER=1 ./scripts/probe-realdebrid-match.sh
set -e
cd "$(dirname "$0")/.."

if [ -z "${REAL_DEBRID_API_TOKEN:-}" ] && [ -f .env ]; then
  set -a
  # shellcheck source=/dev/null
  . ./.env
  set +a
fi

run_host() {
  if ! python3 -c "import httpx" 2>/dev/null; then
    echo "Install httpx on host: pip install httpx"
    exit 1
  fi
  export DB_PATH="${DB_PATH:-./data/state/queue.db}"
  python3 scripts/probe-realdebrid-match.py "$@"
}

run_container() {
  docker compose exec -T \
    -e REAL_DEBRID_API_TOKEN="${REAL_DEBRID_API_TOKEN:-}" \
    -e REAL_DEBRID_API_BASE="${REAL_DEBRID_API_BASE:-}" \
    orchestrator python /app/scripts/probe-realdebrid-match.py "$@"
}

if [ "${PROBE_RD_IN_CONTAINER:-}" = "1" ]; then
  run_container "$@"
elif docker compose ps orchestrator 2>/dev/null | grep -q Up; then
  # Default: host (reads ./data/state/queue.db, uses host network)
  run_host "$@"
else
  run_host "$@"
fi
