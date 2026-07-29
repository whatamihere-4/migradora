#!/usr/bin/env bash
# Validate fast ffmpeg_slice inside the migradora orchestrator container.
#
# Usage (on VPS or dev machine with migradora running):
#   ./scripts/test-ffmpeg-slice.sh /data/downloads/test/test.mp4
#   ./scripts/test-ffmpeg-slice.sh /data/downloads/test/test.mp4 2147483648
#
# Host path equivalent:
#   ./scripts/test-ffmpeg-slice.sh ./data/downloads/test/test.mp4
set -euo pipefail

CONTAINER="${MIGRADORA_CONTAINER:-migradora-orchestrator}"
INPUT="${1:-/data/downloads/test/test.mp4}"
PART_LIMIT="${2:-10200547328}" # ~9.5 GiB default (under Filester's 10 GB cap)
OUT_DIR="/data/downloads/test/slice-test-$$"

if [[ ! -f "$INPUT" && -f "./data/downloads/${INPUT#./data/downloads/}" ]]; then
  INPUT="./data/downloads/${INPUT#./data/downloads/}"
fi

if [[ -f "$INPUT" ]]; then
  HOST_INPUT="$INPUT"
  case "$INPUT" in
    /data/downloads/*) CONTAINER_INPUT="$INPUT" ;;
    ./data/downloads/*|data/downloads/*)
      rel="${INPUT#./}"
      rel="${rel#data/downloads/}"
      CONTAINER_INPUT="/data/downloads/$rel"
      ;;
    *)
      echo "Put test files under ./data/downloads/ or pass a /data/downloads/... container path." >&2
      exit 1
      ;;
  esac
else
  CONTAINER_INPUT="$INPUT"
  HOST_INPUT=""
fi

if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "Container $CONTAINER is not running." >&2
  exit 1
fi

echo "==> Container: $CONTAINER"
echo "==> Input:     $CONTAINER_INPUT"
echo "==> Part cap:  $PART_LIMIT bytes"
echo "==> Output:    $OUT_DIR"
echo

docker exec "$CONTAINER" mkdir -p "$OUT_DIR"

echo "==> 1) Sparse keyframe plan (timed)"
docker exec -i "$CONTAINER" python3 - "$CONTAINER_INPUT" "$PART_LIMIT" <<'PY'
import sys
import time
from pathlib import Path

from migradora.ffmpeg_splitter import (
    plan_sparse_keyframe_part_starts,
    probe_duration,
)

path = Path(sys.argv[1])
part_limit = int(sys.argv[2])
size = path.stat().st_size
duration = probe_duration(path)
target_segment_time = max(1, int((part_limit * 0.90) / (size / duration)))

t0 = time.perf_counter()
starts = plan_sparse_keyframe_part_starts(path, duration, target_segment_time)
elapsed = time.perf_counter() - t0

print(f"duration_sec={duration:.3f}")
print(f"target_segment_sec={target_segment_time}")
print(f"planned_parts={len(starts)}")
print(f"part_starts={', '.join(f'{s:.3f}' for s in starts)}")
print(f"sparse_plan_sec={elapsed:.2f}")
PY

echo
echo "==> 2) Split into playable parts (timed, source kept)"
docker exec -i "$CONTAINER" python3 - "$CONTAINER_INPUT" "$OUT_DIR" "$PART_LIMIT" <<'PY'
import sys
import time
from pathlib import Path

from migradora.splitter import iter_upload_parts

path = Path(sys.argv[1])
out_dir = Path(sys.argv[2])
part_limit = int(sys.argv[3])

t0 = time.perf_counter()
parts = list(
    iter_upload_parts(
        path,
        out_dir,
        part_limit,
        split_mode="ffmpeg_slice",
        delete_source=False,
    )
)
elapsed = time.perf_counter() - t0

print(f"parts={len(parts)}")
for part in parts:
    p = Path(part["path"])
    print(f"  {part['filename']}: {p.stat().st_size:,} bytes")
print(f"split_total_sec={elapsed:.2f}")
PY

echo
echo "==> 3) Boundary sanity (last frame of PART1 vs first of PART2)"
docker exec -i "$CONTAINER" python3 - "$OUT_DIR" <<'PY'
import subprocess
import sys
from pathlib import Path

out_dir = Path(sys.argv[1])
parts = sorted(out_dir.glob("*.PART*.mp4"))
if len(parts) < 2:
    print("only one part; boundary check skipped")
    raise SystemExit(0)

p1, p2 = parts[0], parts[1]

def frame_count(path: Path) -> int:
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-count_packets", "-show_entries", "stream=nb_read_packets",
            "-of", "csv=p=0", str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(proc.stdout.strip())

def last_pts(path: Path) -> float:
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_frames", "-show_entries", "frame=pts_time",
            "-of", "csv=p=0", str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    return float(lines[-1])

def first_pts(path: Path) -> float:
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_frames", "-show_entries", "frame=pts_time",
            "-read_intervals", "%+0.5",
            "-of", "csv=p=0", str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    return float(lines[0])

n1 = frame_count(p1)
n2 = frame_count(p2)
l1 = last_pts(p1)
f2 = first_pts(p2)
print(f"PART1 frames={n1} last_pts={l1:.6f}")
print(f"PART2 frames={n2} first_pts={f2:.6f}")
print(f"pts_gap={f2 - l1:.6f} (expect small positive step, not duplicate overlap)")
PY

echo
echo "==> Done. Parts left in $OUT_DIR (remove manually when finished)."
if [[ -n "$HOST_INPUT" ]]; then
  host_out="./data/downloads/${OUT_DIR#/data/downloads/}"
  echo "Host path: $host_out"
fi
