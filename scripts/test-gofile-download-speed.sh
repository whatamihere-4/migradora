#!/bin/sh
# Resolve a Gofile file URL to the CDN link and measure raw download speed.
# Runs entirely inside the orchestrator container (python sqlite + curl).
#
# Usage:
#   ./scripts/test-gofile-download-speed.sh
#   ./scripts/test-gofile-download-speed.sh 'https://gofile.io/d/...#file=...'
#   ./scripts/test-gofile-download-speed.sh --seconds 30
set -e
cd "$(dirname "$0")/.."

GOFILE_URL="${1:-}"
SECONDS=120
if [ "$GOFILE_URL" = "--seconds" ]; then
  SECONDS="${2:-120}"
  GOFILE_URL="${3:-}"
fi

docker compose exec -T orchestrator python - "$GOFILE_URL" "$SECONDS" <<'PY'
import sqlite3
import subprocess
import sys
import time

from migradora.config import Settings
from migradora.gofile_client import GofileClient

url = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 120

if not url:
    row = sqlite3.connect("/data/state/queue.db").execute(
        """
        SELECT gofile_url, filename, size_bytes
        FROM files
        WHERE gofile_url IS NOT NULL AND gofile_url LIKE '%#file=%'
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        print(
            "No queued file with gofile_url containing #file=.\n"
            "Usage: ./scripts/test-gofile-download-speed.sh "
            "'https://gofile.io/d/FOLDER#file=FILE_ID'",
            file=sys.stderr,
        )
        sys.exit(1)
    url, filename, size_bytes = row
    print(f"From queue: {filename!r} ({size_bytes or '?'} bytes)", file=sys.stderr)
else:
    print(f"Using URL: {url}", file=sys.stderr)

settings = Settings.load()
if not settings.gofile_token:
    print("GOFILE_TOKEN is not set", file=sys.stderr)
    sys.exit(1)

with GofileClient(token=settings.gofile_token, password=settings.gofile_password) as client:
    link = client.resolve_direct_link(url)

print(f"CDN: {link}", file=sys.stderr)
print(link)

# Speed test with curl (same as a manual wget/curl download).
proc = subprocess.run(
    [
        "curl",
        "-L",
        "--max-time",
        str(seconds),
        "-o",
        "/dev/null",
        "-w",
        "%{http_code} %{size_download} %{time_total} %{speed_download}",
        link,
    ],
    capture_output=True,
    text=True,
)
line = (proc.stdout or "").strip()
if proc.returncode != 0:
    print(f"curl failed ({proc.returncode}): {proc.stderr or line}", file=sys.stderr)
    sys.exit(proc.returncode)

parts = line.split()
if len(parts) != 4:
    print(f"Unexpected curl output: {line!r}", file=sys.stderr)
    sys.exit(1)

http_code, size_download, time_total, speed_bps = parts
size_download = int(float(size_download))
speed_bps = float(speed_bps)
time_total = float(time_total)
mb_s = speed_bps / 1024 / 1024

print(file=sys.stderr)
print(f"HTTP {http_code}", file=sys.stderr)
print(f"Downloaded {size_download / (1024**2):.1f} MiB in {time_total:.1f}s", file=sys.stderr)
print(f"Average speed: {mb_s:.1f} MiB/s ({speed_bps / 1_000_000:.1f} MB/s)", file=sys.stderr)
if size_download < 1024 * 1024:
    print(
        "(Short sample — try a larger file or increase --seconds for a better reading.)",
        file=sys.stderr,
    )
PY
