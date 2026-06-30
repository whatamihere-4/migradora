"""Disk and source-file size limits for the serial pipeline."""

from __future__ import annotations

from migradora.config import Settings
from migradora.splitter import required_disk_bytes
from migradora.utils import free_disk_gb

_GIB = 1024**3


def max_processable_source_bytes(settings: Settings, *, free_gb: float | None = None) -> int:
    """
    Largest single source file we can process on this VPS.

    Peak disk while splitting is ``source + one upload part`` (see ``required_disk_bytes``),
    plus ``MIN_FREE_DISK_GB`` headroom.
    """
    if settings.max_source_file_bytes > 0:
        return settings.max_source_file_bytes

    if settings.disk_budget_gb > 0:
        budget_gb = float(settings.disk_budget_gb)
    else:
        budget_gb = free_gb if free_gb is not None else free_disk_gb(settings.download_dir)

    part_gb = settings.filester_max_file_bytes / _GIB
    usable_gb = budget_gb - settings.min_free_disk_gb - part_gb
    return int(max(0.0, usable_gb) * _GIB)


def required_disk_gb(file_size: int, settings: Settings) -> float:
    if file_size <= 0:
        return float(settings.min_free_disk_gb)
    return (
        required_disk_bytes(
            file_size,
            settings.filester_max_file_bytes,
            split_mode=settings.filester_split_mode,
        )
        / _GIB
        + settings.min_free_disk_gb
    )


def oversize_skip_reason(file_size: int, settings: Settings) -> str | None:
    if not settings.auto_skip_oversized or file_size <= 0:
        return None
    limit = max_processable_source_bytes(settings)
    if limit <= 0:
        return "File size unknown or disk budget too small"
    if file_size > limit:
        return (
            f"File too large for VPS disk budget "
            f"({file_size / _GIB:.1f} GiB > {limit / _GIB:.1f} GiB max)"
        )
    return None
