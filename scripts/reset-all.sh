#!/bin/sh
# Wipe migradora queue state and re-discover from Gofile (fresh start).
set -e
cd "$(dirname "$0")/.."

echo "=== Stopping orchestrator ==="
docker compose stop orchestrator

echo "=== Resetting queue, folder mappings, and local downloads ==="
docker compose run --rm -T orchestrator python -m migradora reset --yes --discover

echo "=== Starting orchestrator ==="
docker compose up -d orchestrator

echo "=== Done. Check dashboard or: docker compose exec orchestrator python -m migradora status ==="
