#!/bin/sh
# Nuclear option: clear entire JD2 linkgrabber + downloads lists.
set -e
cd "$(dirname "$0")/.."
API="${JD2_API_URL:-http://localhost:3128}"

echo "Clearing JD2 linkgrabber..."
curl -sf -X POST "$API/linkgrabberv2/clearList" -H 'Content-Type: application/json' -d '[]' \
  || echo "(linkgrabber clearList failed — use web UI)"

echo "Clearing migradora packages from downloads..."
./scripts/jd2-clear-migradora.sh 2>/dev/null || true

echo "Done. Delete any remaining packages in JD2 web UI if needed."
