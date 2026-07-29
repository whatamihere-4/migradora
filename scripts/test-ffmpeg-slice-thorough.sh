#!/usr/bin/env bash
# Thorough ffmpeg_slice diagnostics — planning stress tests, merge, boundary hashes.
# Slower than test-ffmpeg-slice.sh; use when debugging split/merge quality.
#
# Usage:
#   ./scripts/test-ffmpeg-slice-thorough.sh /data/downloads/test/test.mp4
#   ./scripts/test-ffmpeg-slice-thorough.sh /data/downloads/test/test.mp4 400000000
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
echo "==> 1b) Sparse plan stress (dur/3 — multi-boundary timing)"
docker exec -i "$CONTAINER" python3 - "$CONTAINER_INPUT" <<'PY'
import sys
import time
from pathlib import Path

from migradora.ffmpeg_splitter import plan_sparse_keyframe_part_starts, probe_duration

path = Path(sys.argv[1])
duration = probe_duration(path)
target_segment_time = max(1, int(duration / 3))

t0 = time.perf_counter()
starts = plan_sparse_keyframe_part_starts(path, duration, target_segment_time)
elapsed = time.perf_counter() - t0

print(f"target_segment_sec={target_segment_time}")
print(f"planned_parts={len(starts)}")
print(f"part_starts={', '.join(f'{s:.3f}' for s in starts)}")
print(f"sparse_plan_stress_sec={elapsed:.2f}")
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
echo "==> 3) Boundary checks (source-aligned frame hashes; fast window reads only)"
docker exec -i "$CONTAINER" python3 - "$CONTAINER_INPUT" "$OUT_DIR" <<'PY'
import hashlib
import subprocess
import sys
from pathlib import Path

source = Path(sys.argv[1])
out_dir = Path(sys.argv[2])
parts = sorted(out_dir.glob(f"{source.stem}.PART*{source.suffix}"))
if len(parts) < 2:
    print("only one part; boundary checks skipped")
    raise SystemExit(0)

print("NOTE: per-part PTS are reset (-reset_timestamps); do not compare PARTn PTS to PARTn+1 PTS.")


def duration(path: Path) -> float:
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(proc.stdout.strip())


def frame_md5(path: Path, ss: float) -> str:
    raw = subprocess.check_output(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", f"{ss:.6f}", "-i", str(path),
            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ],
        stderr=subprocess.DEVNULL,
    )
    return hashlib.md5(raw).hexdigest()


def pts_in_window(path: Path, start: float, end: float) -> list[float]:
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-read_intervals", f"{start:.3f}%{end:.3f}",
            "-select_streams", "v:0",
            "-show_frames", "-show_entries", "frame=pts_time",
            "-of", "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return [float(x) for x in proc.stdout.splitlines() if x.strip()]


offsets: list[float] = []
cum = 0.0
for part in parts:
    offsets.append(cum)
    cum += duration(part)

issues = 0
for i in range(1, len(parts)):
    boundary = offsets[i]
    prev_part, cur_part = parts[i - 1], parts[i]
    prev_dur = duration(prev_part)

    prev_pts = pts_in_window(prev_part, max(0.0, prev_dur - 2.0), prev_dur)
    cur_pts = pts_in_window(cur_part, 0.0, min(2.0, duration(cur_part)))

    src_at = frame_md5(source, boundary)
    src_before = frame_md5(source, max(0.0, boundary - 0.05))
    prev_last = frame_md5(prev_part, max(0.0, prev_dur - 0.05))
    cur_first = frame_md5(cur_part, 0.0)

    print(f"boundary {i} @ source_t={boundary:.3f}s")
    print(f"  prev_tail_pts=[{prev_pts[-3:] if prev_pts else []}] cur_head_pts=[{cur_pts[:3] if cur_pts else []}]")
    print(f"  prev_last matches src_before: {prev_last == src_before}")
    print(f"  cur_first matches src_at:     {cur_first == src_at}")
    if prev_last == cur_first and prev_last != src_at:
        print("  WARN: prev tail frame == cur head frame but != source at boundary (overlap)")
        issues += 1
    if prev_last != src_before:
        print("  WARN: prev part tail != source before boundary")
        issues += 1
    if cur_first != src_at:
        print("  WARN: cur part head != source at boundary")
        issues += 1

if issues:
    print(f"FAIL: {issues} boundary warning(s)")
else:
    print("PASS: boundary frame hashes align with source")
PY

echo
echo "==> 4) Merge parts with ffmpeg concat (stream copy)"
docker exec -i "$CONTAINER" python3 - "$CONTAINER_INPUT" "$OUT_DIR" <<'PY'
import subprocess
import sys
from pathlib import Path

source = Path(sys.argv[1])
out_dir = Path(sys.argv[2])
parts = sorted(out_dir.glob(f"{source.stem}.PART*{source.suffix}"))
if len(parts) < 2:
    print("only one part; merge skipped")
    raise SystemExit(0)

list_path = out_dir / "parts.txt"
list_path.write_text("\n".join(f"file '{p}'" for p in parts) + "\n", encoding="utf-8")
merged = out_dir / f"{source.stem}.merged{source.suffix}"

proc = subprocess.run(
    [
        "ffmpeg", "-hide_banner", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(list_path),
        "-c", "copy",
        str(merged),
    ],
    capture_output=True,
    text=True,
)
if proc.returncode != 0:
    print(proc.stderr[-800:])
    raise SystemExit(proc.returncode)

print(f"merged={merged}")
for p in parts:
    print(f"  part {p.name}: {p.stat().st_size:,} bytes")
print(f"  merged size: {merged.stat().st_size:,} bytes")
PY

echo
echo "==> 5) Merge integrity (duration + frame counts)"
docker exec -i "$CONTAINER" python3 - "$CONTAINER_INPUT" "$OUT_DIR" <<'PY'
import subprocess
import sys
from pathlib import Path

source = Path(sys.argv[1])
out_dir = Path(sys.argv[2])
parts = sorted(out_dir.glob(f"{source.stem}.PART*{source.suffix}"))
merged = out_dir / f"{source.stem}.merged{source.suffix}"


def duration(path: Path) -> float:
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(proc.stdout.strip())


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

src_dur = duration(source)
src_frames = video_packets(source)
part_frames = sum(video_packets(p) for p in parts)
parts_dur = sum(duration(p) for p in parts)

print(f"source:  duration={src_dur:.3f}s frames={src_frames}")
print(f"parts:   duration_sum={parts_dur:.3f}s frames_sum={part_frames}")
if merged.exists():
    mrg_dur = duration(merged)
    mrg_frames = video_packets(merged)
    print(f"merged:  duration={mrg_dur:.3f}s frames={mrg_frames}")
    dur_delta = abs(mrg_dur - src_dur)
    frame_delta = abs(mrg_frames - src_frames)
    print(f"delta:   duration={dur_delta:.3f}s frames={frame_delta}")
    if frame_delta == 0 and dur_delta < 0.5:
        print("PASS: merged matches source")
    elif part_frames > src_frames:
        print(f"FAIL: {part_frames - src_frames} extra frame(s) in parts (overlap)")
    elif part_frames < src_frames:
        print(f"FAIL: {src_frames - part_frames} missing frame(s) in parts (gap)")
    else:
        print("WARN: small duration drift; inspect playback manually")
else:
    if part_frames == src_frames:
        print("PASS: part frame sum matches source")
    elif part_frames > src_frames:
        print(f"FAIL: {part_frames - src_frames} extra frame(s) in parts (overlap)")
    else:
        print(f"FAIL: {src_frames - part_frames} missing frame(s) in parts (gap)")
PY

slice_print_done
