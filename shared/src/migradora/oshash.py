"""OpenSubtitles / Stash-compatible OSHASH — fast fingerprint for large files."""

from __future__ import annotations

import struct
from pathlib import Path

_BLOCK = 65536


def compute_oshash(path: str | Path) -> str:
    """StashDB OSHASH: size + sum of 64-bit LE chunks over head/tail 64 KiB, mod 2**64."""
    path = Path(path)
    size = path.stat().st_size
    if size < _BLOCK:
        raise ValueError(f"file too small for oshash ({size} < {_BLOCK} bytes)")

    with path.open("rb") as f:
        head = f.read(_BLOCK)
        f.seek(-_BLOCK, 2)
        tail = f.read(_BLOCK)

    if len(head) != _BLOCK or len(tail) != _BLOCK:
        raise ValueError("could not read full head/tail chunks for oshash")

    h = size
    chunks = _BLOCK // 8
    fmt = "<" + "Q" * chunks
    for v in struct.unpack_from(fmt, head, 0):
        h += v
    for v in struct.unpack_from(fmt, tail, 0):
        h += v
    h &= (1 << 64) - 1
    return f"{h:016x}"


def verify_oshash(path: str | Path, expected: str) -> bool:
    return compute_oshash(path) == expected.strip().lower()
