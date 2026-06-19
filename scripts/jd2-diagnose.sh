#!/bin/sh
# JD2 + queue diagnostics — run from repo root on the VPS.
set -e
cd "$(dirname "$0")/.."

COMPOSE="docker compose"
if [ -f docker-compose.vpn.yml ] && docker compose -f docker-compose.yml -f docker-compose.vpn.yml --profile vpn ps -q gluetun 2>/dev/null | grep -q .; then
  COMPOSE="docker compose -f docker-compose.yml -f docker-compose.vpn.yml --profile vpn"
fi

echo "=== Download dir permissions ==="
ls -la data/downloads/ 2>/dev/null || echo "(missing)"
find data/downloads -maxdepth 2 -type d 2>/dev/null | while read -r d; do ls -ld "$d"; done

echo
echo "=== JD2 download controller ==="
STATE=$(curl -sf http://localhost:3128/downloadcontroller/getCurrentState 2>/dev/null || echo "unavailable")
SPEED=$(curl -sf http://localhost:3128/downloadcontroller/getSpeedInBps 2>/dev/null || echo "0")
echo "state: $STATE"
echo "speed_bps: $SPEED"

echo
echo "=== JD2 linkgrabber (migradora-*) ==="
curl -s -X POST http://localhost:3128/linkgrabberv2/queryPackages \
  -H 'Content-Type: application/json' \
  -d '{"status":true,"saveTo":true,"bytesTotal":true,"childCount":true}' \
  | jq '.data[] | select(.name|test("migradora"))' 2>/dev/null || echo "(none)"

echo
echo "=== JD2 downloads (migradora-*) ==="
curl -s -X POST http://localhost:3128/downloadsV2/queryPackages \
  -H 'Content-Type: application/json' \
  -d '{"status":true,"saveTo":true,"bytesTotal":true,"bytesLoaded":true,"running":true,"finished":true}' \
  | jq '.data[] | select(.name|test("migradora"))' 2>/dev/null || echo "(none)"

echo
echo "=== JD2 download links (gofile) ==="
curl -s -X POST http://localhost:3128/downloadsV2/queryLinks \
  -H 'Content-Type: application/json' \
  -d '{"url":true,"bytesTotal":true,"bytesLoaded":true,"finished":true,"status":true,"running":true}' \
  | jq '.data[] | select(.url|test("gofile")) | {status, running, bytesLoaded, bytesTotal, url}' 2>/dev/null || echo "(none)"

echo
echo "=== Queue DB ==="
$COMPOSE exec -T orchestrator python -c "
import sqlite3
c = sqlite3.connect('/data/state/queue.db')
for r in c.execute('SELECT id,status,attempts,substr(gofile_url,1,60),last_error FROM files'):
    print(r)
state = c.execute('SELECT state,pause_reason FROM queue_control WHERE id=1').fetchone()
print('queue_control:', state)
" 2>/dev/null || echo "(orchestrator down)"

echo
echo "=== Orchestrator status ==="
curl -sf http://localhost:8080/status | jq '{queue_state,pause_reason,pipeline,stats}' 2>/dev/null || true

echo
echo "=== Recent orchestrator logs ==="
$COMPOSE logs orchestrator --tail 20 2>/dev/null || true
