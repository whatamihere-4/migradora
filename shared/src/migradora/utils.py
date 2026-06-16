"""Shared utility helpers."""

from __future__ import annotations

import shutil


def free_disk_gb(path: str) -> float:
    usage = shutil.disk_usage(path)
    return usage.free / (1024 ** 3)
