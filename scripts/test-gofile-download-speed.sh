#!/bin/sh
# Resolve a Gofile file URL to the CDN link and measure download speed using the
# same httpx client + auth headers as migradora (plain curl often gets 0 bytes).
#
# Usage:
#   ./scripts/test-gofile-download-speed.sh
#   ./scripts/test-gofile-download-speed.sh 'https://gofile.io/d/...#file=...'
#   ./scripts/test-gofile-download-speed.sh --seconds 30
#   ./scripts/test-gofile-download-speed.sh --mib 200
#   ./scripts/test-gofile-download-speed.sh --probe-servers
set -e
cd "$(dirname "$0")/.."

GOFILE_URL=""
SECONDS=30
SAMPLE_MIB=100
PROBE_SERVERS=0

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
    --probe-servers)
      PROBE_SERVERS=1
      shift
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

docker compose exec -T orchestrator python - "$GOFILE_URL" "$SECONDS" "$SAMPLE_MIB" "$PROBE_SERVERS" <<'PY'
import sqlite3
import sys
import time
from urllib.parse import urlparse

from migradora.config import Settings
from migradora.gofile_client import (
    GofileClient,
    _host_from_gofile_url,
    _order_server_hosts,
    _server_hosts_from_file_data,
    parse_gofile_url,
)

url = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 30
sample_mib = int(sys.argv[3]) if len(sys.argv) > 3 else 100
probe_servers = bool(int(sys.argv[4])) if len(sys.argv) > 4 else False
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

print(
    f"CDN settings: prefer={settings.gofile_cdn_prefer} "
    f"probe={settings.gofile_cdn_probe} "
    f"connections={settings.gofile_download_connections}",
    file=sys.stderr,
)
if settings.download_throttle_kbps > 0:
    print(
        f"Warning: DOWNLOAD_THROTTLE_KBPS={settings.download_throttle_kbps} "
        "(caps pipeline speed; speed test ignores throttle)",
        file=sys.stderr,
    )

_, file_id = parse_gofile_url(url)
if not file_id:
    print("URL must include #file=FILE_ID", file=sys.stderr)
    sys.exit(1)

with GofileClient(
    token=settings.gofile_token,
    password=settings.gofile_password,
    cdn_prefer=settings.gofile_cdn_prefer,
    cdn_probe=settings.gofile_cdn_probe or probe_servers,
    download_connections=settings.gofile_download_connections,
) as client:
    info = client.get_file_info(file_id)
    hosts = _server_hosts_from_file_data(info)
    ordered = _order_server_hosts(hosts, settings.gofile_cdn_prefer)
    print(f"API servers: {hosts}", file=sys.stderr)
    print(f"serverSelected: {info.get('serverSelected')!r}", file=sys.stderr)
    if ordered != hosts:
        print(f"After prefer={settings.gofile_cdn_prefer}: {ordered}", file=sys.stderr)

    candidates = client._candidate_download_urls(info, file_id)
    if probe_servers or settings.gofile_cdn_probe:
        print("\nProbing mirrors (2 MiB sample each):", file=sys.stderr)
        scored: list[tuple[float, str]] = []
        for candidate in candidates:
            host = _host_from_gofile_url(candidate)
            speed = client._probe_download_speed(candidate)
            mib_s = speed / (1024**2)
            scored.append((speed, candidate))
            print(f"  {host}: {mib_s:.1f} MiB/s ({mib_s * 8:.0f} Mbps)", file=sys.stderr)
        scored.sort(reverse=True)
        link = scored[0][1] if scored and scored[0][0] > 0 else client.resolve_direct_link(url)
    else:
        link = client.resolve_direct_link(url)

    host = urlparse(link).hostname or "?"
    print(f"\nCDN: {link}", file=sys.stderr)
    if host.startswith("store-na-"):
        print(
            f"Note: CDN node {host} is North America — a France VPS may be slow on this route.\n"
            "Try: GOFILE_CDN_PROBE=true and/or GOFILE_DOWNLOAD_CONNECTIONS=4 in .env",
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
