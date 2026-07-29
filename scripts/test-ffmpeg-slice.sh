#!/usr/bin/env bash
# Quick ffmpeg_slice validation — production-like path only (plan + split + frame sum).
#
# Usage:
#   ./scripts/test-ffmpeg-slice.sh /data/downloads/test/test.mp4
#   ./scripts/test-ffmpeg-slice.sh /data/downloads/test/test.mp4 400000000
#
# For merge tests, boundary hashes, and stress planning see test-ffmpeg-slice-thorough.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/test-ffmpeg-slice-lib.sh
source "$SCRIPT_DIR/test-ffmpeg-slice-lib.sh"

slice_resolve_input "${1:-}" "${2:-}"

echo "==> 1) Sparse keyframe plan (production part cap)"
docker exec -i "$CONTAINER" python3 - "$CONTAINER_INPUT" "$PART_LIMIT" <<'PY'
import sys
import time
from pathlib import Path

from migradora.ffmpeg_splitter import plan_sparse_keyframe_part_starts, probe_duration

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
echo "==> 2) Split into playable parts (source kept)"
docker exec -i "$CONTAINER" python3 - "$CONTAINER_INPUT" "$OUT_DIR" "$SPLIT_LIMIT" <<'PY'
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
echo "==> 3) Part frame sum vs source (overlap => parts_sum > source)"
docker exec -i "$CONTAINER" python3 - "$CONTAINER_INPUT" "$OUT_DIR" <<'PY'
import subprocess
import sys
from pathlib import Path

source = Path(sys.argv[1])
out_dir = Path(sys.argv[2])
parts = sorted(out_dir.glob(f"{source.stem}.PART*{source.suffix}"))

def video_packets(path: Path) -> int:
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-count_packets",
            "-show_entries", "stream=nb_read_packets",
            "-of", "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(proc.stdout.strip())

if len(parts) < 2:
    print("only one part produced; boundary overlap check not applicable")
    raise SystemExit(0)

src_frames = video_packets(source)
part_frames = sum(video_packets(p) for p in parts)
print(f"source_frames={src_frames}")
print(f"parts_frames_sum={part_frames}")
if part_frames == src_frames:
    print("PASS: part frame sum matches source")
elif part_frames > src_frames:
    print(f"FAIL: {part_frames - src_frames} extra frame(s) — overlap at boundary(s)")
else:
    print(f"FAIL: {src_frames - part_frames} missing frame(s) — gap at boundary(s)")
PY

slice_print_done
