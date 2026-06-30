#!/bin/sh
# Resolve a Gofile file URL to the CDN link and measure download speed using the
# same httpx client + auth headers as migradora (plain curl often gets 0 bytes).
#
# Usage:
#   ./scripts/test-gofile-download-speed.sh
#   ./scripts/test-gofile-download-speed.sh 'https://gofile.io/d/...#file=...'
#   ./scripts/test-gofile-download-speed.sh --seconds 30
#   ./scripts/test-gofile-download-speed.sh --mib 200
set -e
cd "$(dirname "$0")/.."

GOFILE_URL=""
SECONDS=30
SAMPLE_MIB=100

while [ $# -gt 0 ]; do
  case "$1" in
    --seconds)
      SECONDS="${2:-30}"
      shift 2
      ;;
    --mib)
      SAMPLE_MIB="${2:-100}"
      shift 2
      ;;
    -*)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
    *)
      GOFILE_URL="$1"
      shift
      ;;
  esac
done

docker compose exec -T orchestrator python - "$GOFILE_URL" "$SECONDS" "$SAMPLE_MIB" <<'PY'
import sqlite3
import sys
import time
from urllib.parse import urlparse

from migradora.config import Settings
from migradora.gofile_client import GofileClient

url = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 30
sample_mib = int(sys.argv[3]) if len(sys.argv) > 3 else 100
sample_bytes = sample_mib * 1024 * 1024

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
    host = urlparse(link).hostname or "?"
    print(f"CDN: {link}", file=sys.stderr)
    if host.startswith("store-na-"):
        print(
            f"Note: CDN node {host} is North America — a France VPS may be slow on this route.",
            file=sys.stderr,
        )
    elif host.startswith("store-eu-"):
        print(f"CDN node {host} looks like EU (good for a France VPS).", file=sys.stderr)

    downloaded = 0
    start = time.monotonic()
    deadline = start + seconds
    status = None
    content_length = None

    with client._client.stream("GET", link, follow_redirects=True) as resp:
        status = resp.status_code
        content_length = resp.headers.get("content-length")
        print(f"HTTP {status}", file=sys.stderr)
        if content_length:
            print(f"Content-Length: {int(content_length) / (1024**3):.2f} GiB", file=sys.stderr)
        resp.raise_for_status()
        for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
            if not chunk:
                continue
            downloaded += len(chunk)
            if downloaded >= sample_bytes or time.monotonic() >= deadline:
                break

elapsed = max(time.monotonic() - start, 0.001)
mb_s = (downloaded / (1024 * 1024)) / elapsed

print(file=sys.stderr)
print(f"Sampled {downloaded / (1024**2):.1f} MiB in {elapsed:.1f}s", file=sys.stderr)
print(f"Average speed: {mb_s:.1f} MiB/s ({mb_s * 8:.0f} Mbps)", file=sys.stderr)
if downloaded == 0:
    print(
        "Got 0 bytes — CDN may be rejecting the request or stalling. "
        "Check migradora logs for the same job.",
        file=sys.stderr,
    )
    sys.exit(1)
if downloaded < 5 * 1024 * 1024:
    print(
        f"(Small sample — try --seconds {seconds * 2} or --mib {sample_mib * 2})",
        file=sys.stderr,
    )
PY
