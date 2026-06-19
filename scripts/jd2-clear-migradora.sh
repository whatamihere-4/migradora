#!/bin/sh
# Remove all migradora-* packages from JD2 linkgrabber and downloads.
set -e
cd "$(dirname "$0")/.."

API="${JD2_API_URL:-http://localhost:3128}"

remove_packages() {
  endpoint="$1"
  curl -sf -X POST "$API/$endpoint/queryPackages" \
    -H 'Content-Type: application/json' \
    -d '{"status":true}' \
    | jq -r '.data[] | select(.name|test("^migradora-")) | .uuid' \
    | while read -r id; do
        [ -n "$id" ] || continue
        echo "Removing $endpoint package $id"
        curl -sf -X POST "$API/$endpoint/removeLinks" \
          -H 'Content-Type: application/json' \
          -d "[[], [$id]]" \
          || curl -sf -X POST "$API/$endpoint/removeLinks" \
          -H 'Content-Type: application/json' \
          -d "[[0], [$id]]" \
          || echo "  (remove failed — delete manually in web UI)"
      done
}

echo "Clearing migradora packages from JD2..."
remove_packages linkgrabberv2
remove_packages downloadsV2
echo "Done. Run ./scripts/reset-failed-jobs.sh then watch orchestrator logs."
