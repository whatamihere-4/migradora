"""Transfer speed tracking and ETA estimation."""

from __future__ import annotations

import time
from dataclasses import dataclass


def format_eta(seconds: float | None) -> str:
    """Human-readable duration (e.g. ``2h 15m``)."""
    if seconds is None or seconds < 0 or seconds != seconds:  # NaN
        return "—"
    if seconds == 0:
        return "0s"
    total = int(round(seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def eta_seconds(remaining_bytes: int, speed_bps: float | None) -> float | None:
    if remaining_bytes <= 0:
        return 0.0
    if not speed_bps or speed_bps <= 0:
        return None
    return remaining_bytes / speed_bps


class TransferTracker:
    """Rolling average download/upload throughput (bytes per second)."""

    def __init__(self, sample_interval_sec: float = 1.0, ema_weight: float = 0.25) -> None:
        self._sample_interval = sample_interval_sec
        self._ema_weight = ema_weight
        self._download_bps: float | None = None
        self._upload_bps: float | None = None
        self._phase: str | None = None
        self._phase_started_at: float = 0.0
        self._last_sample_at: float = 0.0
        self._last_sample_bytes: int = 0

    @property
    def download_bps(self) -> float | None:
        return self._download_bps

    @property
    def upload_bps(self) -> float | None:
        return self._upload_bps

    def begin_phase(self, phase: str) -> None:
        self._phase = phase
        now = time.time()
        self._phase_started_at = now
        self._last_sample_at = now
        self._last_sample_bytes = 0

    def update_progress(self, phase: str, bytes_done: int) -> None:
        now = time.time()
        elapsed = now - self._last_sample_at
        if elapsed < self._sample_interval:
            return
        delta = bytes_done - self._last_sample_bytes
        if delta > 0:
            self._record_speed(phase, delta / elapsed)
        self._last_sample_at = now
        self._last_sample_bytes = bytes_done

    def complete_phase(self, phase: str, total_bytes: int) -> None:
        duration = time.time() - self._phase_started_at
        if total_bytes > 0 and duration > 0:
            self._record_speed(phase, total_bytes / duration)
        self._phase = None

    def _record_speed(self, phase: str, bps: float) -> None:
        if phase == "download":
            if self._download_bps is None:
                self._download_bps = bps
            else:
                w = self._ema_weight
                self._download_bps = w * bps + (1 - w) * self._download_bps
        elif phase == "upload":
            if self._upload_bps is None:
                self._upload_bps = bps
            else:
                w = self._ema_weight
                self._upload_bps = w * bps + (1 - w) * self._upload_bps

    def snapshot(self) -> dict[str, float | None]:
        return {
            "download_bps": self._download_bps,
            "upload_bps": self._upload_bps,
        }


@dataclass
class TransferRemaining:
    download_bytes: int
    upload_bytes: int


def compute_remaining_bytes(
    *,
    incomplete_bytes: int,
    current_job_id: int | None,
    current_job_size: int,
    phase: str,
    progress_bytes: int,
    upload_bytes_done: int,
    upload_bytes_total: int,
) -> TransferRemaining:
    """Bytes still to download/upload for the queue (incomplete jobs only)."""
    if incomplete_bytes <= 0:
        return TransferRemaining(download_bytes=0, upload_bytes=0)

    remaining_dl = incomplete_bytes
    remaining_ul = incomplete_bytes

    if not current_job_id or phase not in ("downloading", "uploading"):
        return TransferRemaining(download_bytes=remaining_dl, upload_bytes=remaining_ul)

    size = current_job_size or 0
    if phase == "downloading":
        remaining_dl -= min(progress_bytes, size)
    elif phase == "uploading":
        remaining_dl -= size
        remaining_ul -= min(upload_bytes_done, upload_bytes_total or size)

    return TransferRemaining(
        download_bytes=max(0, remaining_dl),
        upload_bytes=max(0, remaining_ul),
    )


def compute_queue_eta(
    remaining: TransferRemaining,
    download_bps: float | None,
    upload_bps: float | None,
) -> dict[str, float | None]:
    """Queue ETA = remaining_dl / avg_dl + remaining_ul / avg_ul."""
    dl_sec = eta_seconds(remaining.download_bytes, download_bps)
    ul_sec = eta_seconds(remaining.upload_bytes, upload_bps)
    if dl_sec is None and ul_sec is None:
        total = None
    elif dl_sec is None:
        total = ul_sec
    elif ul_sec is None:
        total = dl_sec
    else:
        total = dl_sec + ul_sec
    return {
        "queue_sec": total,
        "download_sec": dl_sec,
        "upload_sec": ul_sec,
    }
