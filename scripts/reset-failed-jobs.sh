#!/bin/sh
# Reset failed/stuck queue jobs to pending (no sqlite3 CLI required).
set -e
cd "$(dirname "$0")/.."

COMPOSE="docker compose"
if [ -f docker-compose.vpn.yml ] && docker compose -f docker-compose.yml -f docker-compose.vpn.yml --profile vpn ps -q gluetun 2>/dev/null | grep -q .; then
  COMPOSE="docker compose -f docker-compose.yml -f docker-compose.vpn.yml --profile vpn"
fi

$COMPOSE exec -T orchestrator python -c "
import sqlite3
from pathlib import Path
db = Path('/data/state/queue.db')
conn = sqlite3.connect(db)
cur = conn.execute(
    \"\"\"UPDATE files SET status='pending', attempts=0, last_error=NULL
       WHERE is_part=0 AND status IN ('failed', 'downloading')\"\"\"
)
conn.execute(\"UPDATE queue_control SET state='running', pause_reason='' WHERE id=1\")
conn.commit()
print(f'Reset {cur.rowcount} job(s) to pending')
"
