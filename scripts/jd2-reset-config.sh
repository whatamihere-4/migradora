#!/bin/sh
# Reset broken JD2 config (e.g. after pre-copying only RemoteAPIConfig.json).
# Usage: ./scripts/jd2-reset-config.sh
set -e
cd "$(dirname "$0")/.."
echo "Stopping jdownloader..."
docker compose stop jdownloader 2>/dev/null || true
echo "Removing data/jd2/config (JD2 will re-initialize on next start)..."
rm -rf data/jd2/config
mkdir -p data/jd2/config
echo "Done. Next steps:"
echo "  1. docker compose up -d jdownloader"
echo "  2. Wait ~2 min, open http://localhost:5800 (JD2 web UI loads)"
echo "  3. ./scripts/jd2-enable-api.sh"
echo "  4. docker compose restart jdownloader"
echo "  5. curl http://localhost:3128/help"
