"""Split oversized media into watchable parts via ffmpeg stream-copy (one part at a time).

Parts are named ``<name>.PART1.<ext>``, ``<name>.PART2.<ext>`` … and each is
independently playable. Rejoin losslessly with the concat demuxer::

    ffmpeg -f concat -safe 0 -i list.txt -c copy movie.mkv
"""

from __future__ import annotations

import logging
import math
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

logger = logging.getLogger("migradora.ffmpeg_splitter")

_TARGET_FACTORS = (0.90, 0.75, 0.60)


class SplitError(RuntimeError):
    pass


def probe_duration(path: str | Path, *, ffprobe_bin: str = "ffprobe") -> float:
    proc = subprocess.run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    raw = (proc.stdout or "").strip()
    try:
        dur = float(raw)
    except ValueError:
        dur = 0.0
    if dur <= 0:
        raise SplitError(
            f"Could not determine media duration via ffprobe (got {raw!r}); "
            f"cannot split {Path(path).name}"
        )
    return dur


def _copy_stream_maps() -> list[str]:
    """Video + audio only (skip timecode/data tracks that break MP4 segment muxing)."""
    return ["-map", "0:v", "-map", "0:a?"]


def _ffmpeg_line_for_log(line: str) -> str | None:
    s = line.strip()
    if not s:
        return None
    lower = s.lower()
    if lower.startswith("ffmpeg version") or lower.startswith("configuration:"):
        return None
    if "libav" in lower and ("copyright" in lower or "built with" in lower):
        return None
    if "input #" in lower and "from '" in lower:
        return None
    if "output #" in lower and "to '" in lower:
        return s
    if "opening '" in lower or "stream mapping" in lower:
        return s
    if "error" in lower or "failed" in lower or "warning" in lower:
        return s
    if "time=" in s and ("frame=" in s or "size=" in s or "bitrate=" in s):
        return s
    if s.startswith("frame="):
        return s
    return None


def _run_ffmpeg_logged(
    cmd: list[str],
    *,
    timeout: int,
    skip_check: Callable[[], None] | None = None,
) -> None:
    if "-stats_period" not in cmd:
        insert_at = 1 if len(cmd) > 1 and cmd[1] == "-hide_banner" else 1
        cmd = cmd[:insert_at] + ["-stats_period", "1"] + cmd[insert_at:]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    stderr_buf: list[str] = []
    last_progress = [0.0]

    def _reader() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_buf.append(line)
            picked = _ffmpeg_line_for_log(line)
            if not picked:
                continue
            now = time.time()
            if "time=" in picked and (now - last_progress[0]) < 1.0:
                continue
            if "time=" in picked:
                last_progress[0] = now
            logger.info("[ffmpeg] %s", picked)

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    deadline = time.time() + timeout
    rc = None
    try:
        while True:
            if skip_check:
                skip_check()
            rc = proc.poll()
            if rc is not None:
                break
            if time.time() > deadline:
                proc.kill()
                proc.wait()
                raise SplitError(f"ffmpeg timed out after {timeout}s")
            time.sleep(0.25)
    finally:
        if rc is None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        reader.join(timeout=3)

    if rc != 0:
        tail = "".join(stderr_buf)[-600:]
        raise SplitError(f"ffmpeg failed (exit {rc}): {tail}")


def _extract_single_segment(
    path: str | Path,
    output_path: str | Path,
    start_sec: float,
    duration_sec: float,
    *,
    ffmpeg_bin: str,
    timeout: int,
    skip_check: Callable[[], None] | None = None,
) -> None:
    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-y",
        "-ss",
        str(max(0.0, start_sec)),
        "-i",
        str(path),
        "-t",
        str(max(0.001, duration_sec)),
        *_copy_stream_maps(),
        "-c",
        "copy",
        "-reset_timestamps",
        "1",
        str(output_path),
    ]
    logger.info(
        "ffmpeg slice %s @ %.1fs for %.1fs",
        Path(output_path).name,
        start_sec,
        duration_sec,
    )
    _run_ffmpeg_logged(cmd, timeout=timeout, skip_check=skip_check)


def iter_upload_parts_sliced(
    source: str | Path,
    output_dir: str | Path,
    part_size_bytes: int,
    *,
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
    ffmpeg_timeout: int = 7200,
    skip_check: Callable[[], None] | None = None,
    delete_source: bool = True,
) -> Iterator[dict]:
    """Yield one ffmpeg stream-copy part at a time (~source + one part on disk)."""
    source = Path(source)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not source.exists():
        raise FileNotFoundError(f"Source file not found: {source}")

    size = source.stat().st_size
    if size <= part_size_bytes:
        yield {
            "path": str(source),
            "filename": source.name,
            "size_bytes": size,
            "part_index": 0,
            "part_count": 1,
            "is_source": True,
            "original_basename": source.name,
            "split_mode": "ffmpeg_slice",
        }
        return

    stem = source.stem
    ext = source.suffix
    duration = probe_duration(source, ffprobe_bin=ffprobe_bin)
    bytes_per_sec = size / duration

    segment_time = None
    num_parts = None
    last_err = None
    for factor in _TARGET_FACTORS:
        if skip_check:
            skip_check()

        target_bytes = int(part_size_bytes * factor)
        trial_segment_time = max(1, int(target_bytes / bytes_per_sec))
        trial_num_parts = max(1, math.ceil(duration / trial_segment_time))
        probe_path = output_dir / f"{stem}.PART1{ext}"
        probe_path.unlink(missing_ok=True)

        _extract_single_segment(
            source,
            probe_path,
            0,
            trial_segment_time,
            ffmpeg_bin=ffmpeg_bin,
            timeout=ffmpeg_timeout,
            skip_check=skip_check,
        )
        probe_size = probe_path.stat().st_size
        probe_path.unlink(missing_ok=True)

        if probe_size > part_size_bytes:
            last_err = (
                f"first slice exceeded limit at factor {factor} "
                f"({probe_size:,} > {part_size_bytes:,} bytes)"
            )
            logger.warning(last_err)
            continue

        segment_time = trial_segment_time
        num_parts = trial_num_parts
        logger.info(
            "ffmpeg per-part slice: %d part(s), ~%ds each (factor %s)",
            num_parts,
            segment_time,
            factor,
        )
        break

    if segment_time is None or num_parts is None:
        raise SplitError(
            f"Unable to slice {source.name} under {part_size_bytes:,} bytes. Last: {last_err}"
        )

    original = source.name
    for idx in range(num_parts):
        if skip_check:
            skip_check()

        start = idx * segment_time
        seg_dur = min(segment_time, duration - start)
        if seg_dur <= 0:
            break

        part_name = f"{stem}.PART{idx + 1}{ext}"
        part_path = output_dir / part_name
        _extract_single_segment(
            source,
            part_path,
            start,
            seg_dur,
            ffmpeg_bin=ffmpeg_bin,
            timeout=ffmpeg_timeout,
            skip_check=skip_check,
        )
        part_size = part_path.stat().st_size
        if part_size > part_size_bytes:
            part_path.unlink(missing_ok=True)
            raise SplitError(
                f"Part {idx + 1} ({part_name}) is {part_size:,} bytes "
                f"(> {part_size_bytes:,}); try bytes mode or a smaller FILESTER_MAX_FILE_BYTES"
            )

        yield {
            "path": str(part_path),
            "filename": part_name,
            "size_bytes": part_size,
            "part_index": idx + 1,
            "part_count": num_parts,
            "is_source": False,
            "original_basename": original,
            "split_mode": "ffmpeg_slice",
        }

    if delete_source:
        source.unlink(missing_ok=True)
        logger.info("Removed source after splitting: %s", source.name)
