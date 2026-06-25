"""Split large files into parts for Filester's 10 GB upload limit."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Iterator
from pathlib import Path

logger = logging.getLogger("migradora.splitter")

_CHUNK_SIZE = 8 * 1024 * 1024
_SKIP_CHECK_EVERY_CHUNKS = 32


def required_disk_bytes(file_size: int, part_size_bytes: int) -> int:
    """Peak bytes on disk while processing one job (source + at most one part)."""
    if file_size <= 0:
        return 0
    if file_size <= part_size_bytes:
        return file_size
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


def iter_upload_parts(
    source: str | Path,
    output_dir: str | Path,
    part_size_bytes: int,
    base_name: str | None = None,
    skip_check: Callable[[], None] | None = None,
) -> Iterator[dict]:
    """
    Yield upload parts one at a time.

    Only one part file exists on disk alongside the source at any moment, so a
    24 GB file needs ~34 GB peak (source + 9.5 GB part) instead of ~48 GB (all parts).
    """
    source = Path(source)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not source.exists():
        raise FileNotFoundError(f"Source file not found: {source}")

    total_size = source.stat().st_size
    if total_size <= part_size_bytes:
        yield {
            "path": str(source),
            "filename": source.name,
            "size_bytes": total_size,
            "part_index": 0,
            "is_source": True,
        }
        return

    stem = base_name or source.stem
    suffix = source.suffix
    num_parts = math.ceil(total_size / part_size_bytes)
    logger.info(
        "Splitting %s (%d bytes) into %d part(s) of up to %d bytes each",
        source.name,
        total_size,
        num_parts,
        part_size_bytes,
    )

    for idx in range(num_parts):
        offset = idx * part_size_bytes
        part_size = min(part_size_bytes, total_size - offset)
        part_name = f"{stem}.part{idx + 1:03d}{suffix}"
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
            "is_source": False,
        }

    source.unlink()
    logger.info("Removed source after splitting: %s", source.name)


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
