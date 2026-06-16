"""Split large files into parts for Filester's 10 GB upload limit."""

from __future__ import annotations

import logging
import math
import subprocess
from pathlib import Path

logger = logging.getLogger("migradora.splitter")


def split_file(
    source: str | Path,
    output_dir: str | Path,
    part_size_bytes: int,
    base_name: str | None = None,
) -> list[dict]:
    source = Path(source)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not source.exists():
        raise FileNotFoundError(f"Source file not found: {source}")

    total_size = source.stat().st_size
    if total_size <= part_size_bytes:
        return [{
            "path": str(source),
            "filename": source.name,
            "size_bytes": total_size,
            "part_index": 0,
        }]

    stem = base_name or source.stem
    prefix = f"{stem}.part"
    cmd = [
        "split",
        "-b", str(part_size_bytes),
        "-a", "3",
        "-d",
        "--additional-suffix=.bin",
        str(source),
        str(output_dir / prefix),
    ]
    logger.info("Splitting %s into ~%d byte parts", source, part_size_bytes)
    subprocess.run(cmd, check=True, capture_output=True, text=True)

    parts: list[dict] = []
    for idx, part_path in enumerate(sorted(output_dir.glob(f"{prefix}*.bin"))):
        new_name = f"{stem}.part{idx + 1:03d}{source.suffix}"
        new_path = output_dir / new_name
        part_path.rename(new_path)
        parts.append({
            "path": str(new_path),
            "filename": new_name,
            "size_bytes": new_path.stat().st_size,
            "part_index": idx + 1,
        })

    source.unlink()
    logger.info("Split %s into %d parts", source.name, len(parts))
    return parts
