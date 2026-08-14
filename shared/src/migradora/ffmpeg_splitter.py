"""Split oversized media into watchable parts via mkvmerge + ffmpeg remux (one part at a time).

Each part is extracted with ``mkvmerge --split parts:…`` (clean time boundaries on MP4
input), streamed through a named pipe into ``ffmpeg -c copy`` (no temp file on disk).
Parts are named ``<name>.PART1.<ext>`` … Users merge with a pasted ``mkvmerge`` one-liner
(see :func:`format_merge_oneliner_bash`).
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
_FULL_SCAN_MAX_BYTES = 15 * 1024**3
_PROBE_SIZE_MARGIN = 1.10
_PROBE_SKIP_ESTIMATE_RATIO = 0.85
_MIN_SEGMENT_TIMEOUT_SEC = 300
_EXTRACT_FLOOR_BPS = 2 * 1024 * 1024  # pessimistic VPS read+write for stream copy


def _copy_stream_maps() -> list[str]:
    """Video + audio only — drop data/subtitle tracks that break MP4 remux."""
    return ["-map", "0:v", "-map", "0:a?"]


def _scaled_segment_timeout(
    segment_sec: float,
    file_size: int,
    duration: float,
    max_timeout: int,
) -> int:
    """Cap per-segment extract time from segment bytes, not the global 2h default."""
    if segment_sec <= 0 or duration <= 0 or file_size <= 0:
        return min(max_timeout, _MIN_SEGMENT_TIMEOUT_SEC)
    segment_bytes = int(file_size * (segment_sec / duration))
    io_budget = segment_bytes / _EXTRACT_FLOOR_BPS
    scaled = int(max(_MIN_SEGMENT_TIMEOUT_SEC, io_budget * 2.0))
    return min(max_timeout, scaled)


def _probe_timeout_for_file(file_size: int, configured: int) -> int:
    """Scale ffprobe keyframe probes down on huge files so planning fails fast."""
    size_gb = file_size / (1024**3)
    per_gb = max(60, configured // 4)
    return min(configured, max(60, int(size_gb * per_gb)))


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


def probe_frame_step(path: str | Path, ffprobe_bin: str = "ffprobe") -> float:
    """Return nominal frame duration in seconds (inverse of avg frame rate)."""
    proc = subprocess.run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=avg_frame_rate",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        return 1.0 / 60.0
    raw = (proc.stdout or "").strip().splitlines()[0].strip()
    if "/" in raw:
        num, den = raw.split("/", 1)
        try:
            n, d = float(num), float(den)
            if d > 0 and n > 0:
                return 1.0 / (n / d)
        except ValueError:
            pass
    try:
        rate = float(raw)
        if rate > 0:
            return 1.0 / rate
    except ValueError:
        pass
    return 1.0 / 60.0


def format_merge_oneliner_bash() -> str:
    """One paste into Mac/Linux Terminal (needs mkvmerge on PATH)."""
    return (
        "f=$(ls -1v *.PART1.* | head -1); o=${f/.PART1./.}; t=1; "
        "[ -f \"${o%.*}.merge_trim_frames\" ] && "
        "t=$(tr -d '\\r\\n ' < \"${o%.*}.merge_trim_frames\"); "
        "mkvmerge -o \"$o\" \"$f\" "
        "$(i=2; while [ -f \"${f/.PART1./.PART$i.}\" ]; do "
        "[ \"$t\" -gt 0 ] && echo --split parts-frames:$t-; "
        "echo +${f/.PART1./.PART$i.}; i=$((i+1)); done)"
    )


def format_merge_oneliner_powershell() -> str:
    """One paste into Windows PowerShell (needs mkvmerge on PATH)."""
    return (
        "$p1=(gci *.PART1.*|select -first 1).Name;$o=$p1-replace'\\.PART1\\.';"
        "$t=1;$h=($o-replace'\\.[^.]+$','')+'.merge_trim_frames';"
        "if(Test-Path $h){$t=[int](gc $h)};"
        "$x=@('-o',$o,$p1);for($i=2;$i -le 20;$i++){"
        "if(Test-Path ($p1-replace'PART1',\"PART$i\")){"
        "if($t-gt0){$x+=('--split','parts-frames:'+$t+'-')};"
        "$x+=('+'+($p1-replace'PART1',\"PART$i\"))}};mkvmerge @x"
    )


def write_merge_trim_hint(
    output_dir: Path,
    stem: str,
    *,
    trim_frames: int = 0,
    split_mode: str = "ffmpeg_slice",
) -> dict:
    """Write ``{stem}.merge_trim_frames`` for the merge one-liner (0 = no trim)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    hint_path = output_dir / f"{stem}.merge_trim_frames"
    hint_path.write_text(f"{trim_frames}\n", encoding="utf-8")
    return {
        "path": str(hint_path),
        "filename": hint_path.name,
        "size_bytes": hint_path.stat().st_size,
        "part_index": 0,
        "part_count": 0,
        "is_source": False,
        "is_merge_helper": True,
        "original_basename": stem,
        "split_mode": split_mode,
    }


def _list_part_paths(output_dir: Path, stem: str, ext: str) -> list[Path]:
    parts: list[tuple[int, Path]] = []
    for path in output_dir.iterdir():
        if not path.is_file():
            continue
        name = path.name
        if not name.startswith(f"{stem}.PART") or not name.endswith(ext):
            continue
        mid = name[len(f"{stem}.PART"):-len(ext)]
        if mid.isdigit():
            parts.append((int(mid), path))
    parts.sort(key=lambda t: t[0])
    return [p for _, p in parts]


def _plan_keyframe_split(
    source: Path,
    output_dir: Path,
    part_size_bytes: int,
    *,
    ffprobe_bin: str,
    probe_timeout: int,
    ffmpeg_bin: str,
    mkvmerge_bin: str,
    ffmpeg_timeout: int,
    extract_backend: str,
    skip_check: Callable[[], None] | None,
    on_log: Callable[[str], None] | None,
) -> tuple[list[float], int]:
    """Return ``(part_start_times, target_segment_sec)`` under the byte limit."""
    size = source.stat().st_size
    stem = source.stem
    ext = source.suffix
    duration = probe_duration(source, ffprobe_bin=ffprobe_bin)
    bytes_per_sec = size / duration

    segment_time: int | None = None
    part_starts: list[float] | None = None
    last_err: str | None = None
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
            probe_timeout=probe_timeout,
            file_size=size,
        )
        probe_path = output_dir / f"{stem}.PART1{ext}"
        probe_path.unlink(missing_ok=True)

        first_end = trial_starts[1] if len(trial_starts) > 1 else duration
        est_probe_size = int((first_end - trial_starts[0]) * bytes_per_sec * _PROBE_SIZE_MARGIN)
        probe_skip_threshold = int(part_size_bytes * _PROBE_SKIP_ESTIMATE_RATIO)
        if est_probe_size > probe_skip_threshold:
            _extract_single_segment(
                source,
                probe_path,
                0,
                first_end,
                duration=duration,
                file_size=size,
                ffmpeg_bin=ffmpeg_bin,
                mkvmerge_bin=mkvmerge_bin,
                max_timeout=ffmpeg_timeout,
                extract_backend=extract_backend,
                skip_check=skip_check,
                on_log=on_log,
            )
            probe_size = probe_path.stat().st_size
            probe_path.unlink(missing_ok=True)
        else:
            _emit_log(
                on_log,
                f"Skipping probe slice (est {est_probe_size:,} bytes "
                f"≤ {probe_skip_threshold:,} threshold)",
            )
            probe_size = est_probe_size

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
            "ffmpeg keyframe-aligned split: %d part(s), ~%ds target (factor %s)",
            len(trial_starts),
            segment_time,
            factor,
        )
        _emit_log(
            on_log,
            f"ffmpeg keyframe-aligned split: {len(trial_starts)} part(s), "
            f"~{segment_time}s target (factor {factor})",
        )
        break

    if segment_time is None or part_starts is None:
        raise SplitError(
            f"Unable to slice {source.name} under {part_size_bytes:,} bytes. Last: {last_err}"
        )
    return part_starts, segment_time


def _run_segment_at_keyframes(
    source: Path,
    output_dir: Path,
    stem: str,
    ext: str,
    part_starts: list[float],
    *,
    ffmpeg_bin: str,
    timeout: int,
    skip_check: Callable[[], None] | None = None,
    on_log: Callable[[str], None] | None = None,
) -> None:
    """One-pass stream-copy split at keyframe boundaries (no duplicate boundary frames)."""
    if len(part_starts) < 2:
        raise SplitError("keyframe plan produced fewer than 2 parts")

    times_str = ",".join(f"{t:.6f}" for t in part_starts[1:])
    pattern = str(output_dir / f"{stem}.PART%d{ext}")
    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-y",
        "-i",
        str(source),
        *_copy_stream_maps(),
        "-c",
        "copy",
        "-f",
        "segment",
        "-segment_times",
        times_str,
        "-reset_timestamps",
        "1",
        "-segment_start_number",
        "1",
        pattern,
    ]
    _emit_log(
        on_log,
        f"ffmpeg one-pass keyframe segment ({len(part_starts)} parts, boundaries={times_str})",
    )

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.time() + timeout
    try:
        while True:
            if skip_check:
                skip_check()
            rc = proc.poll()
            if rc is not None:
                if rc != 0:
                    tail = (proc.stderr.read() or "")[-600:]
                    raise SplitError(f"ffmpeg segment failed (exit {rc}): {tail}")
                return
            if time.time() > deadline:
                proc.kill()
                proc.wait()
                raise SplitError(f"ffmpeg segment timed out after {timeout}s")
            time.sleep(0.25)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def iter_upload_parts_ffmpeg(
    source: str | Path,
    output_dir: str | Path,
    part_size_bytes: int,
    *,
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
    mkvmerge_bin: str = "mkvmerge",
    ffmpeg_timeout: int = 1800,
    ffprobe_keyframe_timeout: int = 300,
    extract_backend: str = "ffmpeg",
    skip_check: Callable[[], None] | None = None,
    delete_source: bool = True,
    skip_part_indices: frozenset[int] = frozenset(),
    reuse_existing_parts: bool = False,
    on_log: Callable[[str], None] | None = None,
    on_parts_planned: Callable[[int], None] | None = None,
    on_split_progress: Callable[[int, int, int, str, int], None] | None = None,
) -> Iterator[dict]:
    """Yield all parts after one ffmpeg pass (~2× source disk during split)."""
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
            "split_mode": "ffmpeg",
        }
        return

    stem = source.stem
    ext = source.suffix
    probe_timeout = _probe_timeout_for_file(size, ffprobe_keyframe_timeout)

    part_starts, _ = _plan_keyframe_split(
        source,
        output_dir,
        part_size_bytes,
        ffprobe_bin=ffprobe_bin,
        probe_timeout=probe_timeout,
        ffmpeg_bin=ffmpeg_bin,
        mkvmerge_bin=mkvmerge_bin,
        ffmpeg_timeout=ffmpeg_timeout,
        extract_backend=extract_backend,
        skip_check=skip_check,
        on_log=on_log,
    )

    original = source.name
    num_parts = len(part_starts)
    if on_parts_planned:
        on_parts_planned(num_parts)

    for stale in _list_part_paths(output_dir, stem, ext):
        stale.unlink(missing_ok=True)

    if on_split_progress:
        on_split_progress(1, 0, size, f"{stem}.PART1{ext}", num_parts)

    _run_segment_at_keyframes(
        source,
        output_dir,
        stem,
        ext,
        part_starts,
        ffmpeg_bin=ffmpeg_bin,
        timeout=ffmpeg_timeout,
        skip_check=skip_check,
        on_log=on_log,
    )

    produced = _list_part_paths(output_dir, stem, ext)
    if len(produced) != num_parts:
        raise SplitError(
            f"ffmpeg produced {len(produced)} part(s), expected {num_parts}"
        )

    for idx, part_path in enumerate(produced, start=1):
        if skip_check:
            skip_check()
        part_name = part_path.name
        part_no = idx
        if part_no in skip_part_indices:
            continue
        part_size = part_path.stat().st_size
        if reuse_existing_parts and part_size > 0 and part_size <= part_size_bytes:
            pass
        elif part_size > part_size_bytes:
            part_path.unlink(missing_ok=True)
            raise SplitError(
                f"Part {part_no} ({part_name}) is {part_size:,} bytes "
                f"(> {part_size_bytes:,} bytes)"
            )
        if on_split_progress:
            on_split_progress(part_no, part_size, part_size_bytes, part_name, num_parts)
        yield {
            "path": str(part_path),
            "filename": part_name,
            "size_bytes": part_size,
            "part_index": part_no,
            "part_count": num_parts,
            "is_source": False,
            "original_basename": original,
            "split_mode": "ffmpeg",
        }

    if num_parts > 1:
        yield write_merge_trim_hint(
            output_dir, stem, trim_frames=0, split_mode="ffmpeg",
        )

    if delete_source and not skip_part_indices:
        source.unlink(missing_ok=True)
        _emit_log(on_log, f"Removed source after splitting: {source.name}")


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
    probe_timeout: int,
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
        timeout=probe_timeout,
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
    probe_timeout: int = 300,
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
        timeout=probe_timeout,
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
            probe_timeout=probe_timeout,
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
    probe_timeout: int = 300,
    file_size: int = 0,
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
            probe_timeout=probe_timeout,
        )
        if cache is not None:
            cache.add(times)

        picked = _select_keyframe_at_or_after(times, target_sec)
        if picked is not None:
            return picked

        if end >= duration - _KEYFRAME_EPS:
            break
        cursor = max(cursor + _SPARSE_FORWARD_STEP_SEC, end - _SPARSE_LOOKBACK_SEC)

    if file_size > _FULL_SCAN_MAX_BYTES:
        raise SplitError(
            f"Sparse keyframe lookup missed target {target_sec:.3f}s in "
            f"{Path(path).name}; refusing full-file ffprobe on "
            f"{file_size / (1024**3):.1f} GiB source"
        )

    logger.warning(
        "Sparse keyframe lookup missed target %.3fs in %s; falling back to full scan",
        target_sec,
        Path(path).name,
    )
    full = probe_keyframe_times(
        path, ffprobe_bin=ffprobe_bin, probe_timeout=probe_timeout * 3
    )
    if cache is not None:
        cache.add(full)
    return _select_keyframe_at_or_after(full, target_sec)


def _keyframes_from_packets(
    path: str | Path, *, ffprobe_bin: str, probe_timeout: int
) -> list[float]:
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
        timeout=probe_timeout,
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


def probe_keyframe_times(
    path: str | Path,
    *,
    ffprobe_bin: str = "ffprobe",
    probe_timeout: int = 900,
) -> list[float]:
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
        timeout=probe_timeout,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-400:]
        raise SplitError(f"ffprobe keyframe scan failed for {Path(path).name}: {tail}")

    times = _parse_keyframe_times(proc.stdout or "")
    if not times:
        times = _keyframes_from_packets(
            path, ffprobe_bin=ffprobe_bin, probe_timeout=probe_timeout
        )

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
    probe_timeout: int = 300,
    file_size: int = 0,
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
            probe_timeout=probe_timeout,
            file_size=file_size,
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
                probe_timeout=probe_timeout,
                file_size=file_size,
            )
            if next_start is None or next_start <= starts[-1] + _KEYFRAME_EPS:
                break

        if next_start >= duration - _KEYFRAME_EPS:
            break

        starts.append(next_start)

    return starts


def _extract_single_segment_mkvmerge(
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
    """Extract ``[start_sec, end_sec)`` via mkvmerge → fifo → ffmpeg (legacy, slow on MP4)."""
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


def _extract_single_segment_ffmpeg(
    path: str | Path,
    output_path: str | Path,
    start_sec: float,
    end_sec: float,
    *,
    duration: float,
    ffmpeg_bin: str,
    timeout: int,
    skip_check: Callable[[], None] | None = None,
    on_log: Callable[[str], None] | None = None,
    on_split_progress: Callable[[int], None] | None = None,
) -> None:
    """Extract ``[start_sec, end_sec)`` via ffmpeg input seek + stream copy (fast on MP4)."""
    if end_sec - start_sec <= _KEYFRAME_EPS:
        raise SplitError(
            f"Refusing zero-length segment for {Path(output_path).name} "
            f"({start_sec:.3f}s–{end_sec:.3f}s)"
        )

    output_path = Path(output_path)
    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-y",
        "-ss",
        f"{start_sec:.6f}",
    ]
    if end_sec < duration - _KEYFRAME_EPS:
        # End before the next part's keyframe so rejoin does not duplicate that frame.
        segment_t = end_sec - start_sec - probe_frame_step(path)
        segment_t = max(_KEYFRAME_EPS, segment_t)
        cmd.extend(["-t", f"{segment_t:.6f}"])
    cmd.extend(
        [
            "-i",
            str(path),
            *_copy_stream_maps(),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    _emit_log(
        on_log,
        f"ffmpeg stream-copy {output_path.name} "
        f"{start_sec:.3f}s–{end_sec:.3f}s (timeout {timeout}s)",
    )

    stop_poll = threading.Event()
    poll_thread = None

    def _poll_output_size() -> None:
        last_at = 0.0
        while not stop_poll.is_set():
            if on_split_progress and output_path.exists():
                now = time.time()
                if now - last_at >= 1.0:
                    last_at = now
                    on_split_progress(output_path.stat().st_size)
            stop_poll.wait(0.5)

    if on_split_progress:
        poll_thread = threading.Thread(target=_poll_output_size, daemon=True)
        poll_thread.start()

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        if skip_check:
            skip_check()
        try:
            stderr = proc.communicate(timeout=timeout)[1]
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise SplitError(f"ffmpeg timed out after {timeout}s")

        stderr_tail = (stderr or "")[-600:]
        if proc.returncode != 0:
            raise SplitError(
                f"ffmpeg stream-copy failed (exit {proc.returncode}) for "
                f"{output_path.name}: {stderr_tail}"
            )
        if not output_path.exists() or output_path.stat().st_size <= 0:
            raise SplitError(f"ffmpeg produced no output for {output_path.name}")
    finally:
        stop_poll.set()
        if poll_thread is not None:
            poll_thread.join(timeout=1.5)


def _extract_single_segment(
    path: str | Path,
    output_path: str | Path,
    start_sec: float,
    end_sec: float,
    *,
    duration: float,
    file_size: int,
    ffmpeg_bin: str,
    mkvmerge_bin: str,
    max_timeout: int,
    extract_backend: str = "ffmpeg",
    skip_check: Callable[[], None] | None = None,
    on_log: Callable[[str], None] | None = None,
    on_split_progress: Callable[[int], None] | None = None,
) -> None:
    segment_sec = end_sec - start_sec
    seg_timeout = _scaled_segment_timeout(segment_sec, file_size, duration, max_timeout)
    backend = (extract_backend or "ffmpeg").strip().lower()
    if backend == "mkvmerge":
        _extract_single_segment_mkvmerge(
            path,
            output_path,
            start_sec,
            end_sec,
            duration=duration,
            ffmpeg_bin=ffmpeg_bin,
            mkvmerge_bin=mkvmerge_bin,
            timeout=seg_timeout,
            skip_check=skip_check,
            on_log=on_log,
            on_split_progress=on_split_progress,
        )
        return
    _extract_single_segment_ffmpeg(
        path,
        output_path,
        start_sec,
        end_sec,
        duration=duration,
        ffmpeg_bin=ffmpeg_bin,
        timeout=seg_timeout,
        skip_check=skip_check,
        on_log=on_log,
        on_split_progress=on_split_progress,
    )


def iter_upload_parts_sliced(
    source: str | Path,
    output_dir: str | Path,
    part_size_bytes: int,
    *,
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
    mkvmerge_bin: str = "mkvmerge",
    ffmpeg_timeout: int = 1800,
    ffprobe_keyframe_timeout: int = 300,
    extract_backend: str = "ffmpeg",
    skip_check: Callable[[], None] | None = None,
    delete_source: bool = True,
    skip_part_indices: frozenset[int] = frozenset(),
    reuse_existing_parts: bool = False,
    on_log: Callable[[str], None] | None = None,
    on_parts_planned: Callable[[int], None] | None = None,
    on_split_progress: Callable[[int, int, int, str, int], None] | None = None,
) -> Iterator[dict]:
    """Yield one ffmpeg-sliced part at a time (~source + one part on disk)."""
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
    probe_timeout = _probe_timeout_for_file(size, ffprobe_keyframe_timeout)

    part_starts, _ = _plan_keyframe_split(
        source,
        output_dir,
        part_size_bytes,
        ffprobe_bin=ffprobe_bin,
        probe_timeout=probe_timeout,
        ffmpeg_bin=ffmpeg_bin,
        mkvmerge_bin=mkvmerge_bin,
        ffmpeg_timeout=ffmpeg_timeout,
        extract_backend=extract_backend,
        skip_check=skip_check,
        on_log=on_log,
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
            file_size=size,
            ffmpeg_bin=ffmpeg_bin,
            mkvmerge_bin=mkvmerge_bin,
            max_timeout=ffmpeg_timeout,
            extract_backend=extract_backend,
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

    if num_parts > 1:
        yield write_merge_trim_hint(
            output_dir, stem, trim_frames=0, split_mode="ffmpeg_slice",
        )

    if delete_source and not skip_part_indices:
        source.unlink(missing_ok=True)
        _emit_log(on_log, f"Removed source after splitting: {source.name}")
