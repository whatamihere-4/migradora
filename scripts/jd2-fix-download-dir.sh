#!/bin/sh
# Fix JD2 default download folder + permissions on shared download volume.
set -e
cd "$(dirname "$0")/.."

CFG="data/jd2/config/cfg/org.jdownloader.settings.GeneralSettings.json"
mkdir -p data/jd2/config/cfg data/downloads

if [ -f jd2/config-templates/org.jdownloader.settings.GeneralSettings.json ]; then
  cp jd2/config-templates/org.jdownloader.settings.GeneralSettings.json "$CFG"
else
  cat > "$CFG" <<'EOF'
{
  "defaultdownloadfolder": "/output",
  "maxsimultaneousdownloads": 1,
  "maxsimultanedownloadsperhost": 1
}
EOF
fi

chmod -R a+rwX data/downloads 2>/dev/null || true

echo "Patched $CFG (defaultdownloadfolder=/output)"
echo "Restart jdownloader, remove failed packages in web UI, then:"
echo "  docker compose exec orchestrator python -m migradora resume"
