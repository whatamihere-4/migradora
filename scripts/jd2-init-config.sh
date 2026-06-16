#!/bin/sh
# Internal helper (orchestrator image). Host users should run ./scripts/jd2-enable-api.sh
set -e
CFG_DIR="${JD2_CONFIG_DIR:-/jd2-config}/cfg"
GUI="$CFG_DIR/org.jdownloader.settings.GraphicalUserInterfaceSettings.json"
API="$CFG_DIR/org.jdownloader.api.RemoteAPIConfig.json"
TEMPLATE="/templates/org.jdownloader.api.RemoteAPIConfig.json"

if [ ! -f "$GUI" ]; then
  echo "jd2-init-config: JD2 not initialized; skipping"
  exit 0
fi

mkdir -p "$CFG_DIR"
if [ -f "$TEMPLATE" ]; then
  cp "$TEMPLATE" "$API"
elif [ ! -f "$API" ]; then
  printf '%s\n' '{"deprecatedapienabled":true,"deprecatedapilocalhostonly":false,"port":3128}' > "$API"
fi
