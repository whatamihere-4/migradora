"""OpenSubtitles-style hash (OSHash) — fast fingerprint for large files."""

from __future__ import annotations

import hashlib
from pathlib import Path

_BLOCK = 65536


def compute_oshash(path: str | Path) -> str:
    """Hash first and last 64 KiB of a file (MD5 hex)."""
    path = Path(path)
    size = path.stat().st_size
    with path.open("rb") as f:
        if size < _BLOCK:
            buf = f.read()
            buf += b"\x00" * (_BLOCK - len(buf))
        else:
            buf = f.read(_BLOCK)
            f.seek(size - _BLOCK)
            buf += f.read(_BLOCK)
    return hashlib.md5(buf).hexdigest()


def verify_oshash(path: str | Path, expected: str) -> bool:
    return compute_oshash(path) == expected.strip().lower()
