#!/bin/sh
# Test Gofile API token + URL resolution from the orchestrator container.
set -e
cd "$(dirname "$0")/.."

URL="${1:-}"
if [ -z "$URL" ]; then
  URL=$(docker compose exec -T orchestrator python -c "
import sqlite3
row = sqlite3.connect('/data/state/queue.db').execute(
    'SELECT gofile_url FROM files WHERE id=1'
).fetchone()
print(row[0] if row else '')
" 2>/dev/null | tr -d '\r')
fi

if [ -z "$URL" ]; then
  echo "Usage: $0 [gofile-file-url]"
  echo "  URL must include #file=... or pass a folder URL to list files:"
  echo "  $0 https://gofile.io/d/FOLDER_ID"
  exit 1
fi

if echo "$URL" | grep -q '#file='; then
  docker compose exec -T orchestrator python -c "
from migradora.config import Settings
from migradora.gofile_client import GofileClient

s = Settings.load()
with GofileClient(token=s.gofile_token, password=s.gofile_password) as g:
    link = g.resolve_direct_link('$URL')
    print('OK:', link[:120])
"
else
  docker compose exec -T orchestrator python -c "
from migradora.config import Settings
from migradora.gofile_client import GofileClient

s = Settings.load()
with GofileClient(token=s.gofile_token, password=s.gofile_password) as g:
    files = list(g.iter_files('$URL'))
    print(f'OK: {len(files)} file(s)')
    for f in files[:20]:
        print(f'  {f.size_bytes:>12}  {f.path}')
    if len(files) > 20:
        print(f'  ... and {len(files) - 20} more')
"
fi
