#!/bin/sh
# Enable JD2 Deprecated API after first-run init completed.
# Usage: ./scripts/jd2-enable-api.sh
set -e
cd "$(dirname "$0")/.."

CFG_DIR="data/jd2/config/cfg"
GUI="$CFG_DIR/org.jdownloader.settings.GraphicalUserInterfaceSettings.json"
API="$CFG_DIR/org.jdownloader.api.RemoteAPIConfig.json"
TEMPLATE="jd2/config-templates/org.jdownloader.api.RemoteAPIConfig.json"

if [ ! -f "$GUI" ]; then
  echo "ERROR: JD2 has not finished first-run initialization."
  echo "  Missing: $GUI"
  echo ""
  echo "If you pre-created cfg/ or copied RemoteAPIConfig early, run:"
  echo "  ./scripts/jd2-reset-config.sh"
  echo "  docker compose up -d jdownloader"
  echo "  # wait for web UI at :5800, then run this script again"
  exit 1
fi

mkdir -p "$CFG_DIR"
if [ -f "$TEMPLATE" ]; then
  cp "$TEMPLATE" "$API"
else
  cat > "$API" <<'EOF'
{
  "deprecatedapienabled": true,
  "deprecatedapilocalhostonly": false,
  "port": 3128
}
EOF
fi

echo "Wrote $API"
echo "Restart jdownloader for API to listen on port 3128:"
echo "  docker compose restart jdownloader"
echo "Then verify:"
echo "  curl http://localhost:3128/help"
