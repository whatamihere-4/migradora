"""Split oversized media into watchable parts via mkvmerge + ffmpeg remux (one part at a time).

Each part is extracted with ``mkvmerge --split parts:…`` (clean time boundaries on MP4
input), streamed through a named pipe into ``ffmpeg -c copy`` (no temp file on disk).
Parts are named ``<name>.PART1.<ext>`` … and rejoin losslessly with the concat demuxer.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

logger = logging.getLogger("migradora.ffmpeg_splitter")

_TARGET_FACTORS = (0.90, 0.75, 0.60)
_KEYFRAME_EPS = 0.001
_SPARSE_INITIAL_WINDOW_SEC = 180.0
_SPARSE_MAX_WINDOW_SEC = 300.0
_SPARSE_LOOKBACK_SEC = 30.0
_SPARSE_FORWARD_STEP_SEC = 150.0


def _emit_log(on_log: Callable[[str], None] | None, message: str) -> None:
    logger.info("%s", message)
    if on_log:
        on_log(message)


class SplitError(RuntimeError):
    pass


class _KeyframeCache:
    """Accumulate keyframe PTS values across sparse probes for one file."""

    def __init__(self) -> None:
        self._times: set[float] = {0.0}

    def add(self, times: list[float]) -> None:
        self._times.update(times)

    def at_or_after(self, target_sec: float) -> float | None:
        return _select_keyframe_at_or_after(sorted(self._times), target_sec)


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


def _format_mkvmerge_time(sec: float) -> str:
    sec = max(0.0, sec)
    hours = int(sec // 3600)
    minutes = int((sec % 3600) // 60)
    seconds = sec % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:06.3f}"


def _mkvmerge_split_spec(start_sec: float, end_sec: float, duration: float) -> str:
    if end_sec >= duration - _KEYFRAME_EPS:
        return f"parts:{_format_mkvmerge_time(start_sec)}-"
    return f"parts:{_format_mkvmerge_time(start_sec)}-{_format_mkvmerge_time(end_sec)}"


def _format_read_interval(start_sec: float, end_sec: float, duration: float) -> str:
    """Build an ffprobe ``-read_intervals`` spec as a second-based window."""
    if duration <= 0:
        raise SplitError("Cannot build read interval for zero-duration media")
    start_sec = max(0.0, min(start_sec, duration))
    end_sec = max(start_sec, min(end_sec, duration))
    if end_sec <= start_sec + _KEYFRAME_EPS:
        end_sec = min(duration, start_sec + 1.0)
    return f"{start_sec:.3f}%{end_sec:.3f}"


def _parse_keyframe_times(stdout: str) -> list[float]:
    times: list[float] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            t = float(line)
        except ValueError:
            continue
        if t >= -_KEYFRAME_EPS:
            times.append(t)
    return times


def _keyframes_from_packets_in_interval(
    path: str | Path,
    *,
    start_sec: float,
    end_sec: float,
    ffprobe_bin: str,
) -> list[float]:
    proc = subprocess.run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-read_intervals",
            f"{start_sec:.6f}%{end_sec:.6f}",
            "-select_streams",
            "v:0",
            "-show_entries",
            "packet=pts_time,flags",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        return []

    times: list[float] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(",", 1)
        if len(parts) != 2 or "K" not in parts[1]:
            continue
        try:
            t = float(parts[0])
        except ValueError:
            continue
        if start_sec - _KEYFRAME_EPS <= t <= end_sec + _KEYFRAME_EPS:
            times.append(t)
    return times


def _probe_keyframes_in_interval(
    path: str | Path,
    *,
    start_sec: float,
    end_sec: float,
    duration: float,
    ffprobe_bin: str = "ffprobe",
) -> list[float]:
    """Return keyframe PTS values found in ``[start_sec, end_sec]`` without scanning the whole file."""
    if duration <= 0:
        return []
    start_sec = max(0.0, start_sec)
    end_sec = min(duration, end_sec)
    if end_sec <= start_sec + _KEYFRAME_EPS:
        return []

    interval = _format_read_interval(start_sec, end_sec, duration)
    proc = subprocess.run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-read_intervals",
            interval,
            "-skip_frame",
            "nokey",
            "-select_streams",
            "v:0",
            "-show_entries",
            "frame=pts_time",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-400:]
        raise SplitError(
            f"ffprobe keyframe window failed for {Path(path).name} ({interval}): {tail}"
        )

    times = _parse_keyframe_times(proc.stdout or "")
    if not times:
        times = _keyframes_from_packets_in_interval(
            path,
            start_sec=start_sec,
            end_sec=end_sec,
            ffprobe_bin=ffprobe_bin,
        )
    return sorted(set(times))


def _select_keyframe_at_or_after(times: list[float], target_sec: float) -> float | None:
    candidates = [t for t in sorted(times) if t >= target_sec - _KEYFRAME_EPS]
    return candidates[0] if candidates else None


def find_keyframe_at_or_after(
    path: str | Path,
    target_sec: float,
    duration: float,
    target_segment_time: float,
    *,
    ffprobe_bin: str = "ffprobe",
    cache: _KeyframeCache | None = None,
) -> float | None:
    """Find the first keyframe at or after ``target_sec`` using capped ffprobe windows."""
    if duration <= 0:
        return None

    target_sec = max(0.0, min(target_sec, duration))
    if cache is not None:
        cached = cache.at_or_after(target_sec)
        if cached is not None:
            return cached

    window = min(
        _SPARSE_MAX_WINDOW_SEC,
        max(_SPARSE_INITIAL_WINDOW_SEC, min(target_segment_time * 0.15, 240.0)),
    )
    cursor = max(0.0, target_sec - _SPARSE_LOOKBACK_SEC)

    while cursor < duration - _KEYFRAME_EPS:
        end = min(duration, cursor + window)
        times = _probe_keyframes_in_interval(
            path,
            start_sec=cursor,
            end_sec=end,
            duration=duration,
            ffprobe_bin=ffprobe_bin,
        )
        if cache is not None:
            cache.add(times)

        picked = _select_keyframe_at_or_after(times, target_sec)
        if picked is not None:
            return picked

        if end >= duration - _KEYFRAME_EPS:
            break
        cursor = max(cursor + _SPARSE_FORWARD_STEP_SEC, end - _SPARSE_LOOKBACK_SEC)

    logger.warning(
        "Sparse keyframe lookup missed target %.3fs in %s; falling back to full scan",
        target_sec,
        Path(path).name,
    )
    full = probe_keyframe_times(path, ffprobe_bin=ffprobe_bin)
    if cache is not None:
        cache.add(full)
    return _select_keyframe_at_or_after(full, target_sec)


def _keyframes_from_packets(path: str | Path, *, ffprobe_bin: str) -> list[float]:
    proc = subprocess.run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "packet=pts_time,flags",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=3600,
    )
    if proc.returncode != 0:
        return []

    times: list[float] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(",", 1)
        if len(parts) != 2 or "K" not in parts[1]:
            continue
        try:
            t = float(parts[0])
        except ValueError:
            continue
        if t >= -_KEYFRAME_EPS:
            times.append(t)
    return times


def probe_keyframe_times(path: str | Path, *, ffprobe_bin: str = "ffprobe") -> list[float]:
    """Return sorted presentation timestamps of all video keyframes (full-file scan)."""
    proc = subprocess.run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-skip_frame",
            "nokey",
            "-select_streams",
            "v:0",
            "-show_entries",
            "frame=pts_time",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=3600,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-400:]
        raise SplitError(f"ffprobe keyframe scan failed for {Path(path).name}: {tail}")

    times = _parse_keyframe_times(proc.stdout or "")
    if not times:
        times = _keyframes_from_packets(path, ffprobe_bin=ffprobe_bin)

    if not times:
        raise SplitError(f"No video keyframes found in {Path(path).name}")

    times = sorted(set(times))
    if times[0] > _KEYFRAME_EPS:
        times = [0.0, *times]
    return times


def plan_keyframe_part_starts(
    keyframes: list[float],
    duration: float,
    target_segment_time: float,
) -> list[float]:
    """Return part start times ``[0, kf1, kf2, ...]`` on keyframe boundaries.

    Each part runs from one start time to the next (or ``duration`` for the last
    part). Every non-zero start is a keyframe so parts are playable on their own
    and concat with ``-c copy`` without overlap.
    """
    if duration <= 0:
        return [0.0]

    kf = sorted(set(keyframes))
    if kf[0] > _KEYFRAME_EPS:
        kf = [0.0, *kf]

    starts = [0.0]
    while starts[-1] < duration - _KEYFRAME_EPS:
        target = starts[-1] + max(1.0, target_segment_time)
        if target >= duration - _KEYFRAME_EPS:
            break

        candidates = [t for t in kf if t >= target - _KEYFRAME_EPS]
        if not candidates:
            break

        next_start = candidates[0]
        if next_start <= starts[-1] + _KEYFRAME_EPS:
            later = [t for t in kf if t > starts[-1] + _KEYFRAME_EPS]
            if not later:
                break
            next_start = later[0]

        if next_start >= duration - _KEYFRAME_EPS:
            break

        starts.append(next_start)

    return starts


def plan_sparse_keyframe_part_starts(
    path: str | Path,
    duration: float,
    target_segment_time: float,
    *,
    ffprobe_bin: str = "ffprobe",
) -> list[float]:
    """Plan part starts by probing only near each split boundary."""
    if duration <= 0:
        return [0.0]

    cache = _KeyframeCache()
    starts = [0.0]
    while starts[-1] < duration - _KEYFRAME_EPS:
        target = starts[-1] + max(1.0, target_segment_time)
        if target >= duration - _KEYFRAME_EPS:
            break

        next_start = find_keyframe_at_or_after(
            path,
            target,
            duration,
            target_segment_time,
            ffprobe_bin=ffprobe_bin,
            cache=cache,
        )
        if next_start is None:
            break
        if next_start <= starts[-1] + _KEYFRAME_EPS:
            next_start = find_keyframe_at_or_after(
                path,
                starts[-1] + _KEYFRAME_EPS,
                duration,
                target_segment_time,
                ffprobe_bin=ffprobe_bin,
                cache=cache,
            )
            if next_start is None or next_start <= starts[-1] + _KEYFRAME_EPS:
                break

        if next_start >= duration - _KEYFRAME_EPS:
            break

        starts.append(next_start)

    return starts


def _extract_single_segment(
    path: str | Path,
    output_path: str | Path,
    start_sec: float,
    end_sec: float,
    *,
    duration: float,
    ffmpeg_bin: str,
    mkvmerge_bin: str,
    timeout: int,
    skip_check: Callable[[], None] | None = None,
    on_log: Callable[[str], None] | None = None,
    on_split_progress: Callable[[int], None] | None = None,
) -> None:
    """Extract ``[start_sec, end_sec)`` via mkvmerge → fifo → ffmpeg (no temp file)."""
    if end_sec - start_sec <= _KEYFRAME_EPS:
        raise SplitError(
            f"Refusing zero-length segment for {Path(output_path).name} "
            f"({start_sec:.3f}s–{end_sec:.3f}s)"
        )

    spec = _mkvmerge_split_spec(start_sec, end_sec, duration)
    output_path = Path(output_path)
    fifo = output_path.with_suffix(output_path.suffix + ".fifo")
    fifo.unlink(missing_ok=True)
    os.mkfifo(fifo)

    ffmpeg_cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-y",
        "-i",
        str(fifo),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    _emit_log(on_log, f"mkvmerge→fifo→ffmpeg {output_path.name} {spec}")

    ff_proc: subprocess.Popen[str] | None = None
    stop_poll = threading.Event()

    def _poll_output_size() -> None:
        last_at = 0.0
        while not stop_poll.is_set():
            if on_split_progress and output_path.exists():
                now = time.time()
                if now - last_at >= 1.0:
                    last_at = now
                    on_split_progress(output_path.stat().st_size)
            stop_poll.wait(0.5)

    poll_thread = None
    if on_split_progress:
        poll_thread = threading.Thread(target=_poll_output_size, daemon=True)
        poll_thread.start()
    try:
        ff_proc = subprocess.Popen(
            ffmpeg_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        if skip_check:
            skip_check()

        mkv_cmd = [
            mkvmerge_bin,
            "-q",
            "-o",
            str(fifo),
            "--split",
            spec,
            str(path),
        ]
        deadline = time.time() + timeout
        try:
            mkv_proc = subprocess.run(
                mkv_cmd,
                capture_output=True,
                text=True,
                timeout=max(1, int(deadline - time.time())),
            )
        except subprocess.TimeoutExpired as exc:
            raise SplitError(f"mkvmerge timed out after {timeout}s") from exc

        assert ff_proc is not None
        try:
            ff_stderr = ff_proc.communicate(timeout=max(1, int(deadline - time.time())))[1]
        except subprocess.TimeoutExpired as exc:
            ff_proc.kill()
            ff_proc.communicate()
            raise SplitError(f"ffmpeg timed out after {timeout}s") from exc

        if ff_proc.returncode != 0:
            tail = (ff_stderr or "")[-600:]
            mkv_tail = (mkv_proc.stderr or mkv_proc.stdout or "")[-300:]
            raise SplitError(
                f"ffmpeg remux failed (exit {ff_proc.returncode}) for {output_path.name}: "
                f"{tail}; mkvmerge exit {mkv_proc.returncode}: {mkv_tail}"
            )

        # mkvmerge cannot seek on a fifo and exits 2 after the mux finishes; ffmpeg success is authoritative.
        if mkv_proc.returncode >= 2:
            logger.debug(
                "mkvmerge fifo exit %s for %s (ignored after successful ffmpeg remux)",
                mkv_proc.returncode,
                output_path.name,
            )
        elif mkv_proc.returncode == 1:
            tail = (mkv_proc.stderr or mkv_proc.stdout or "").strip()
            if tail:
                logger.warning("mkvmerge warnings for %s: %s", output_path.name, tail[-400:])

        if not output_path.exists() or output_path.stat().st_size <= 0:
            raise SplitError(f"ffmpeg produced no output for {output_path.name}")
    finally:
        stop_poll.set()
        if poll_thread is not None:
            poll_thread.join(timeout=1.5)
        if ff_proc is not None and ff_proc.poll() is None:
            ff_proc.kill()
            ff_proc.communicate()
        fifo.unlink(missing_ok=True)


def iter_upload_parts_sliced(
    source: str | Path,
    output_dir: str | Path,
    part_size_bytes: int,
    *,
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
    mkvmerge_bin: str = "mkvmerge",
    ffmpeg_timeout: int = 7200,
    skip_check: Callable[[], None] | None = None,
    delete_source: bool = True,
    skip_part_indices: frozenset[int] = frozenset(),
    reuse_existing_parts: bool = False,
    on_log: Callable[[str], None] | None = None,
    on_parts_planned: Callable[[int], None] | None = None,
    on_split_progress: Callable[[int, int, int, str, int], None] | None = None,
) -> Iterator[dict]:
    """Yield one mkvmerge/ffmpeg part at a time (~source + one part on disk)."""
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
    part_starts = None
    last_err = None
    for factor in _TARGET_FACTORS:
        if skip_check:
            skip_check()

        target_bytes = int(part_size_bytes * factor)
        trial_segment_time = max(1, int(target_bytes / bytes_per_sec))
        logger.info(
            "Planning sparse keyframe split for %s (~%ds target, factor %s)",
            source.name,
            trial_segment_time,
            factor,
        )
        _emit_log(
            on_log,
            f"Planning sparse keyframe split for {source.name} "
            f"(~{trial_segment_time}s target, factor {factor})",
        )
        trial_starts = plan_sparse_keyframe_part_starts(
            source,
            duration,
            trial_segment_time,
            ffprobe_bin=ffprobe_bin,
        )
        probe_path = output_dir / f"{stem}.PART1{ext}"
        probe_path.unlink(missing_ok=True)

        first_end = trial_starts[1] if len(trial_starts) > 1 else duration
        _extract_single_segment(
            source,
            probe_path,
            0,
            first_end,
            duration=duration,
            ffmpeg_bin=ffmpeg_bin,
            mkvmerge_bin=mkvmerge_bin,
            timeout=ffmpeg_timeout,
            skip_check=skip_check,
            on_log=on_log,
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
        part_starts = trial_starts
        logger.info(
            "ffmpeg keyframe-aligned slice: %d part(s), ~%ds target (factor %s)",
            len(trial_starts),
            segment_time,
            factor,
        )
        _emit_log(
            on_log,
            f"ffmpeg keyframe-aligned slice: {len(trial_starts)} part(s), "
            f"~{segment_time}s target (factor {factor})",
        )
        break

    if segment_time is None or part_starts is None:
        raise SplitError(
            f"Unable to slice {source.name} under {part_size_bytes:,} bytes. Last: {last_err}"
        )

    original = source.name
    num_parts = len(part_starts)
    if on_parts_planned:
        on_parts_planned(num_parts)
    for idx, start in enumerate(part_starts):
        if skip_check:
            skip_check()

        end = part_starts[idx + 1] if idx + 1 < num_parts else duration
        if end - start <= _KEYFRAME_EPS:
            continue

        part_name = f"{stem}.PART{idx + 1}{ext}"
        part_path = output_dir / part_name
        part_no = idx + 1
        if part_no in skip_part_indices:
            continue
        if reuse_existing_parts and part_path.is_file():
            part_size = part_path.stat().st_size
            if part_size > 0 and part_size <= part_size_bytes:
                _emit_log(on_log, f"Reusing existing part {part_no}/{num_parts}: {part_name}")
                yield {
                    "path": str(part_path),
                    "filename": part_name,
                    "size_bytes": part_size,
                    "part_index": part_no,
                    "part_count": num_parts,
                    "is_source": False,
                    "original_basename": original,
                    "split_mode": "ffmpeg_slice",
                    "reused_existing": True,
                }
                continue
        if on_split_progress:
            on_split_progress(part_no, 0, part_size_bytes, part_name, num_parts)
        _emit_log(on_log, f"Splitting part {part_no}/{num_parts}: {part_name}")

        def _report_size(done_bytes: int) -> None:
            if on_split_progress:
                on_split_progress(part_no, done_bytes, part_size_bytes, part_name, num_parts)

        _extract_single_segment(
            source,
            part_path,
            start,
            end,
            duration=duration,
            ffmpeg_bin=ffmpeg_bin,
            mkvmerge_bin=mkvmerge_bin,
            timeout=ffmpeg_timeout,
            skip_check=skip_check,
            on_log=on_log,
            on_split_progress=_report_size,
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

    if delete_source and not skip_part_indices:
        source.unlink(missing_ok=True)
        _emit_log(on_log, f"Removed source after splitting: {source.name}")
