#!/bin/sh
# Measure raw VPS → Filester upload throughput (same httpx client as migradora).
#
# Usage:
#   ./scripts/test-filester-upload-speed.sh
#   ./scripts/test-filester-upload-speed.sh --mib 200
#   ./scripts/test-filester-upload-speed.sh --mib 50 --folder-id YOUR_FOLDER_ID
set -e
cd "$(dirname "$0")/.."

SAMPLE_MIB=100
FOLDER_ID=""

while [ $# -gt 0 ]; do
  case "$1" in
    --mib)
      SAMPLE_MIB="${2:-100}"
      shift 2
      ;;
    --folder-id)
      FOLDER_ID="${2:-}"
      shift 2
      ;;
    -*)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
    *)
      FOLDER_ID="$1"
      shift
      ;;
  esac
done

docker compose exec -T orchestrator python - "$SAMPLE_MIB" "$FOLDER_ID" <<'PY'
import os
import sys
import tempfile
import time
from pathlib import Path

from migradora.config import Settings
from migradora.filester_client import FilesterClient
from migradora.transfer_stats import format_speed

sample_mib = int(sys.argv[1])
folder_id = (sys.argv[2] if len(sys.argv) > 2 else "").strip()
settings = Settings.load()

if not settings.filester_api_key:
    print("FILESTER_API_KEY is not set", file=sys.stderr)
    sys.exit(1)

folder_id = folder_id or settings.filester_root_folder_id
size_bytes = sample_mib * 1024 * 1024
chunk = b"\0" * (1024 * 1024)

with tempfile.NamedTemporaryFile(
    prefix="migradora-upload-bench-",
    suffix=".bin",
    dir=settings.download_dir,
    delete=False,
) as tmp:
    path = Path(tmp.name)
    written = 0
    while written < size_bytes:
        block = min(len(chunk), size_bytes - written)
        tmp.write(chunk[:block])
        written += block

print(f"Uploading {sample_mib} MiB to {settings.filester_api_base} ...")
if folder_id:
    print(f"  folder: {folder_id}")
else:
    print("  folder: (account root — pass --folder-id or set FILESTER_ROOT_FOLDER_ID)")

last = {"t": time.time(), "done": 0}
peak_bps = 0.0

def on_progress(done: int, total: int) -> None:
  global peak_bps
  now = time.time()
  dt = now - last["t"]
  if dt >= 0.5:
      delta = done - last["done"]
      if delta > 0 and dt > 0:
          bps = delta / dt
          peak_bps = max(peak_bps, bps)
      last["t"] = now
      last["done"] = done

started = time.time()
with FilesterClient(
    settings.filester_api_key,
    settings.filester_api_base,
    upload_chunk_bytes=settings.filester_upload_chunk_bytes,
    upload_write_timeout_sec=settings.filester_upload_write_timeout_sec,
) as client:
    result = client.upload_file(
        path,
        folder_id=folder_id or None,
        on_progress=on_progress,
    )
elapsed = time.time() - started
avg_bps = size_bytes / elapsed if elapsed > 0 else 0

slug = result.get("slug") or FilesterClient.file_identifier_from_response(result)
print(f"\nDone in {elapsed:.1f}s")
print(f"  Average: {format_speed(avg_bps)}")
print(f"  Peak (sampled): {format_speed(peak_bps)}")
if slug:
    print(f"  Uploaded: https://filester.me/d/{slug}")
    print(f"  (delete the test file on Filester if you do not need it)")

path.unlink(missing_ok=True)
PY
