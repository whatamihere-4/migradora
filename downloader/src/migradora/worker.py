"""Downloader worker: claims jobs, downloads from Gofile, splits if needed."""

from __future__ import annotations

import hashlib
import logging
import os
import time
from pathlib import Path

from pathvalidate import sanitize_filename

from migradora.config import Settings
from migradora.gofile_client import GoFileClient
from migradora.logger import setup_logging
from migradora.models import FileStatus, QueueState
from migradora.queue.manager import QueueManager
from migradora.splitter import split_file
from migradora.utils import free_disk_gb

logger = logging.getLogger("migradora.downloader")


def compute_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_heartbeat(state_dir: str) -> None:
    Path(state_dir, "downloader.heartbeat").write_text(str(time.time()))


def process_download(record, settings: Settings, queue: QueueManager, client: GoFileClient) -> None:
    dest_dir = Path(settings.download_dir) / str(record.id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / sanitize_filename(record.filename)

    if not record.download_link:
        queue.mark_failed(record.id, "No download link available", retry=False)
        return

    last_report = [0.0]

    def on_progress(done: int, total: int) -> None:
        now = time.time()
        if now - last_report[0] < 2:
            return
        last_report[0] = now
        pct = (done / total * 100) if total else 0
        logger.info(
            "Downloading %s: %.1f%% (%d/%d bytes)",
            record.filename, pct, done, total,
            extra={"job_id": record.id, "filename": record.filename, "phase": "download", "bytes": done},
        )

    client.download_file(
        record.download_link,
        str(dest_path),
        max_retries=settings.download_max_retries,
        retry_delay=settings.download_retry_delay_sec,
        throttle_kbps=settings.download_throttle_kbps,
        progress_callback=on_progress,
    )

    actual_size = dest_path.stat().st_size
    if record.size_bytes and actual_size != record.size_bytes:
        dest_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"Size mismatch: expected {record.size_bytes}, got {actual_size}"
        )

    sha256 = None
    if settings.verify_hash:
        sha256 = compute_sha256(str(dest_path))

    if actual_size > settings.filester_max_file_bytes:
        queue.update_file(record.id, status=FileStatus.SPLITTING, local_path=str(dest_path))
        parts = split_file(
            dest_path,
            dest_dir,
            settings.filester_max_file_bytes,
            base_name=dest_path.stem,
        )
        for part in parts:
            queue.enqueue_part(
                parent_file_id=record.id,
                filename=part["filename"],
                size_bytes=part["size_bytes"],
                local_path=part["path"],
                part_index=part["part_index"],
                gofile_content_id=record.gofile_content_id,
                parent_folder_path=record.parent_folder_path,
            )
        queue.update_file(
            record.id,
            status=FileStatus.SKIPPED,
            local_path=None,
            sha256=sha256,
            last_error=f"Split into {len(parts)} parts for upload",
        )
        logger.info("Split %s into %d parts", record.filename, len(parts))
    else:
        queue.update_file(
            record.id,
            status=FileStatus.DOWNLOADED,
            local_path=str(dest_path),
            sha256=sha256,
        )


def run_worker(settings: Settings | None = None) -> None:
    settings = settings or Settings.load()
    settings.ensure_dirs()
    setup_logging("downloader", settings.log_dir, settings.log_level)
    queue = QueueManager(settings.db_path)
    client = GoFileClient(token=settings.gofile_token or None)

    logger.info("Downloader worker started")

    while True:
        write_heartbeat(settings.state_dir)
        queue.reset_stale_jobs(settings.stale_job_timeout_sec)

        state, reason = queue.get_queue_state()
        if state != QueueState.RUNNING:
            logger.debug("Queue paused (%s): %s", state.value, reason)
            time.sleep(settings.worker_poll_interval_sec)
            continue

        free_gb = free_disk_gb(settings.download_dir)
        if free_gb < settings.min_free_disk_gb:
            queue.set_queue_state(
                QueueState.PAUSED_DISK,
                f"Free disk {free_gb:.1f} GB < minimum {settings.min_free_disk_gb} GB",
            )
            time.sleep(settings.worker_poll_interval_sec)
            continue

        job = queue.claim_download_job()
        if not job:
            time.sleep(settings.worker_poll_interval_sec)
            continue

        logger.info("Processing download job %d: %s", job.id, job.filename)
        try:
            process_download(job, settings, queue, client)
            logger.info("Download complete: %s", job.filename)
        except Exception as exc:
            logger.error("Download failed for %s: %s", job.filename, exc)
            max_attempts = settings.download_max_retries
            if job.attempts >= max_attempts:
                queue.mark_failed(job.id, str(exc), retry=False)
            else:
                queue.mark_failed(job.id, str(exc), retry=True)


if __name__ == "__main__":
    run_worker()
