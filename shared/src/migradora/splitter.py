"""Split large files into parts for Filester's 10 GB upload limit."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Iterator
from pathlib import Path

from migradora.ffmpeg_splitter import SplitError, iter_upload_parts_sliced

logger = logging.getLogger("migradora.splitter")

_CHUNK_SIZE = 8 * 1024 * 1024
_SKIP_CHECK_EVERY_CHUNKS = 32

_SPLIT_MODE_ALIASES = {
    "splice": "bytes",
    "byte": "bytes",
    "bytes": "bytes",
    "cat": "bytes",
    "ffmpeg_slice": "ffmpeg_slice",
    "ffmpeg-slice": "ffmpeg_slice",
    "slice": "ffmpeg_slice",
}


def parse_split_mode(raw: str, *, default: str = "bytes") -> str:
    mode = _SPLIT_MODE_ALIASES.get(raw.strip().lower())
    if mode:
        return mode
    logger.warning("Unknown FILESTER_SPLIT_MODE %r; using %s", raw, default)
    return default


def required_disk_bytes(
    file_size: int,
    part_size_bytes: int,
    *,
    split_mode: str = "bytes",
) -> int:
    """Peak bytes on disk while processing one job (source + at most one part)."""
    if file_size <= 0:
        return 0
    if file_size <= part_size_bytes:
        return file_size
    if split_mode == "ffmpeg":
        return file_size * 2
    # bytes and ffmpeg_slice: source + one part at a time
    return file_size + part_size_bytes


def _extract_part(
    source: Path,
    dest: Path,
    offset: int,
    size: int,
    skip_check: Callable[[], None] | None = None,
) -> None:
    with source.open("rb") as src, dest.open("wb") as dst:
        src.seek(offset)
        remaining = size
        chunks = 0
        while remaining > 0:
            if skip_check and chunks % _SKIP_CHECK_EVERY_CHUNKS == 0:
                skip_check()
            chunk = src.read(min(_CHUNK_SIZE, remaining))
            if not chunk:
                raise RuntimeError(
                    f"Short read extracting {dest.name} at offset {offset}"
                )
            dst.write(chunk)
            remaining -= len(chunk)
            chunks += 1


def _iter_upload_parts_bytes(
    source: Path,
    output_dir: Path,
    part_size_bytes: int,
    base_name: str | None,
    skip_check: Callable[[], None] | None,
    *,
    delete_source: bool,
) -> Iterator[dict]:
    total_size = source.stat().st_size
    if total_size <= part_size_bytes:
        yield {
            "path": str(source),
            "filename": source.name,
            "size_bytes": total_size,
            "part_index": 0,
            "part_count": 1,
            "is_source": True,
            "original_basename": source.name,
            "split_mode": "bytes",
        }
        return

    stem = base_name or source.stem
    suffix = source.suffix
    num_parts = math.ceil(total_size / part_size_bytes)
    part_prefix = source.name if not base_name else f"{stem}{suffix}"
    logger.info(
        "Splitting %s (%d bytes) into %d byte part(s) of up to %d bytes each",
        source.name,
        total_size,
        num_parts,
        part_size_bytes,
    )

    for idx in range(num_parts):
        offset = idx * part_size_bytes
        part_size = min(part_size_bytes, total_size - offset)
        part_name = f"{part_prefix}.part{idx + 1:03d}"
        part_path = output_dir / part_name
        logger.info(
            "Extracting part %d/%d: %s (%d bytes)",
            idx + 1,
            num_parts,
            part_name,
            part_size,
        )
        _extract_part(source, part_path, offset, part_size, skip_check=skip_check)
        yield {
            "path": str(part_path),
            "filename": part_name,
            "size_bytes": part_size,
            "part_index": idx + 1,
            "part_count": num_parts,
            "is_source": False,
            "original_basename": source.name,
            "split_mode": "bytes",
        }

    if delete_source:
        source.unlink(missing_ok=True)
        logger.info("Removed source after splitting: %s", source.name)


def iter_upload_parts(
    source: str | Path,
    output_dir: str | Path,
    part_size_bytes: int,
    base_name: str | None = None,
    skip_check: Callable[[], None] | None = None,
    *,
    split_mode: str = "bytes",
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
    ffmpeg_timeout: int = 7200,
    delete_source: bool = True,
) -> Iterator[dict]:
    """
    Yield upload parts one at a time.

    ``bytes`` (default): byte-range parts rejoined with ``cat``. Only one part
    exists on disk alongside the source at any moment.

    ``ffmpeg_slice``: stream-copy parts via ffmpeg one at a time (playable,
    same peak disk as bytes mode; more CPU).
    """
    source = Path(source)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not source.exists():
        raise FileNotFoundError(f"Source file not found: {source}")

    mode = parse_split_mode(split_mode)
    if mode == "ffmpeg_slice":
        yield from iter_upload_parts_sliced(
            source,
            output_dir,
            part_size_bytes,
            ffmpeg_bin=ffmpeg_bin,
            ffprobe_bin=ffprobe_bin,
            ffmpeg_timeout=ffmpeg_timeout,
            skip_check=skip_check,
            delete_source=delete_source,
        )
        return

    yield from _iter_upload_parts_bytes(
        source,
        output_dir,
        part_size_bytes,
        base_name,
        skip_check,
        delete_source=delete_source,
    )


def split_file(
    source: str | Path,
    output_dir: str | Path,
    part_size_bytes: int,
    base_name: str | None = None,
) -> list[dict]:
    """Return part metadata for a file that fits in one part. Use iter_upload_parts otherwise."""
    source = Path(source)
    if source.stat().st_size > part_size_bytes:
        raise RuntimeError(
            "File exceeds part size; use iter_upload_parts() for streaming split/upload"
        )
    return list(iter_upload_parts(source, output_dir, part_size_bytes, base_name))
