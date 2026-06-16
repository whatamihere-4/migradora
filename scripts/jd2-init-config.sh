#!/bin/sh
# Seed JD2 Deprecated API config on first run (idempotent).
set -e
TEMPLATE_DIR="/templates"
TARGET_DIR="${JD2_CONFIG_DIR:-/data/jd2/config}/cfg"
mkdir -p "$TARGET_DIR"
if [ -f "$TEMPLATE_DIR/org.jdownloader.api.RemoteAPIConfig.json" ]; then
  cp -n "$TEMPLATE_DIR/org.jdownloader.api.RemoteAPIConfig.json" \
    "$TARGET_DIR/org.jdownloader.api.RemoteAPIConfig.json" 2>/dev/null || true
fi
