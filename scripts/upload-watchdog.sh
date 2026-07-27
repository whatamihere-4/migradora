#!/bin/sh
# Monitor upload speed and restart orchestrator when it stays below threshold.
#
# Configure in .env:
#   UPLOAD_WATCHDOG_ENABLED=true
#   UPLOAD_WATCHDOG_MIN_MBPS=5
#   UPLOAD_WATCHDOG_SUSTAIN_SEC=60
#   UPLOAD_WATCHDOG_POLL_SEC=10
#   UPLOAD_WATCHDOG_COOLDOWN_SEC=300
#
# Run on the VPS (repo root), ideally under systemd or tmux:
#   ./scripts/upload-watchdog.sh
#   ./scripts/upload-watchdog.sh --dry-run
#
# One-shot check (e.g. cron):
#   ./scripts/upload-watchdog.sh --once
set -e
cd "$(dirname "$0")/.."

DRY_RUN=0
ONCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --once)
      ONCE=1
      shift
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

if [ -f .env ]; then
  # shellcheck disable=SC1091
  set -a
  . ./.env
  set +a
fi

enabled="${UPLOAD_WATCHDOG_ENABLED:-false}"
case "$enabled" in
  true|1|yes|on|TRUE|YES|ON) ;;
  *)
    echo "Upload watchdog disabled (UPLOAD_WATCHDOG_ENABLED=$enabled)"
    exit 0
    ;;
esac

POLL="${UPLOAD_WATCHDOG_POLL_SEC:-10}"
EXTRA_ARGS=""
[ "$DRY_RUN" -eq 1 ] && EXTRA_ARGS="$EXTRA_ARGS --dry-run"

run_check() {
  set +e
  docker compose exec -T orchestrator python -m migradora upload-watchdog --once $EXTRA_ARGS
  code=$?
  set -e
  return "$code"
}

if [ "$ONCE" -eq 1 ]; then
  run_check
  code=$?
  if [ "$code" -eq 2 ] && [ "$DRY_RUN" -eq 0 ]; then
    echo "Restarting orchestrator ..."
    docker compose restart orchestrator
  fi
  exit "$code"
fi

echo "Upload watchdog running (poll every ${POLL}s, min ${UPLOAD_WATCHDOG_MIN_MBPS:-5} MB/s)"
while true; do
  if run_check; then
    :
  else
    code=$?
    if [ "$code" -eq 2 ] && [ "$DRY_RUN" -eq 0 ]; then
      echo "$(date -Is) Upload watchdog: restarting orchestrator ..."
      docker compose restart orchestrator
      echo "$(date -Is) Waiting for container to come back ..."
      sleep 30
    elif [ "$code" -ne 2 ]; then
      echo "$(date -Is) Upload watchdog check failed (exit $code)" >&2
    fi
  fi
  sleep "$POLL"
done
