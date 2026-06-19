#!/bin/sh
# Fix data/downloads ownership for jlesage/jdownloader-2 (default UID 1000).
set -e
cd "$(dirname "$0")/.."

JD2_UID="${JD2_UID:-1000}"
JD2_GID="${JD2_GID:-1000}"

echo "Fixing ownership of data/downloads for UID ${JD2_UID}:${JD2_GID}..."
if [ "$(id -u)" -eq 0 ]; then
  chown -R "${JD2_UID}:${JD2_GID}" data/downloads
  chmod -R u+rwX data/downloads
else
  sudo chown -R "${JD2_UID}:${JD2_GID}" data/downloads
  sudo chmod -R u+rwX data/downloads
fi

echo "Done. Remove stale JD2 packages in web UI, then:"
echo "  ./scripts/reset-failed-jobs.sh   # if needed"
echo "  docker compose exec orchestrator python -m migradora resume"
