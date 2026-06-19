#!/bin/sh
# Reset failed queue jobs to pending (no sqlite3 CLI required).
set -e
cd "$(dirname "$0")/.."
docker compose exec -T orchestrator python -c "
import sqlite3
from pathlib import Path
db = Path('/data/state/queue.db')
conn = sqlite3.connect(db)
cur = conn.execute(
    \"UPDATE files SET status='pending', attempts=0, last_error=NULL WHERE status='failed' AND is_part=0\"
)
conn.execute(\"UPDATE queue_control SET state='running', pause_reason='' WHERE id=1\")
conn.commit()
print(f'Reset {cur.rowcount} failed job(s) to pending')
"
