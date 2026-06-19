#!/bin/sh
# Test Gofile API token + URL resolution from the orchestrator container.
set -e
cd "$(dirname "$0")/.."

COMPOSE="docker compose"
if [ -f docker-compose.vpn.yml ] && docker compose -f docker-compose.yml -f docker-compose.vpn.yml --profile vpn ps -q gluetun 2>/dev/null | grep -q .; then
  COMPOSE="docker compose -f docker-compose.yml -f docker-compose.vpn.yml --profile vpn"
fi

URL="${1:-}"
if [ -z "$URL" ]; then
  URL=$($COMPOSE exec -T orchestrator python -c "
import sqlite3
row = sqlite3.connect('/data/state/queue.db').execute(
    'SELECT gofile_url FROM files WHERE id=1'
).fetchone()
print(row[0] if row else '')
" 2>/dev/null | tr -d '\r')
fi

if [ -z "$URL" ]; then
  echo "Usage: $0 [gofile-url]"
  exit 1
fi

echo "Testing Gofile resolve for: $URL"
$COMPOSE exec -T orchestrator python -c "
from migradora.config import Settings
from migradora.gofile_client import GofileClient

s = Settings.load()
print('token set:', bool(s.gofile_token))
with GofileClient(token=s.gofile_token, password=s.gofile_password) as g:
    link = g.resolve_direct_link('$URL')
    print('OK:', link[:120])
"
