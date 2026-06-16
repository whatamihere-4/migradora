"""Uploader worker: claims jobs, uploads to Filester, verifies, cleans up."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from migradora.config import Settings
from migradora.filester_client import FilesterClient
from migradora.logger import setup_logging
from migradora.models import FileStatus, QueueState, utc_now
from migradora.queue.manager import QueueManager

logger = logging.getLogger("migradora.uploader")


def write_heartbeat(state_dir: str) -> None:
    Path(state_dir, "uploader.heartbeat").write_text(str(time.time()))


def ensure_filester_folder(
    client: FilesterClient,
    queue: QueueManager,
    settings: Settings,
    parent_folder_path: str,
    folder_cache: dict[str, str],
) -> str | None:
    if not parent_folder_path:
        root_id = queue.get_folder_mapping("__root__")
        if root_id:
            return root_id
        root_id = client.create_folder(settings.filester_root_folder_name)
        queue.save_folder_mapping("__root__", root_id, settings.filester_root_folder_name)
        folder_cache["__root__"] = root_id
        return root_id

    existing = queue.get_folder_mapping(parent_folder_path)
    if existing:
        return existing

    # Build nested path as flattened folder name
    folder_name = parent_folder_path.replace("/", " - ")[-100:]
    folder_id = client.create_folder(folder_name)
    queue.save_folder_mapping(parent_folder_path, folder_id, folder_name)
    folder_cache[parent_folder_path] = folder_id
    return folder_id


def cleanup_local(paths: list[str]) -> None:
    for p in paths:
        try:
            path = Path(p)
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                for child in path.iterdir():
                    cleanup_local([str(child)])
                path.rmdir()
        except OSError as exc:
            logger.warning("Cleanup failed for %s: %s", p, exc)


def process_upload(
    record,
    settings: Settings,
    queue: QueueManager,
    client: FilesterClient,
    folder_cache: dict[str, str],
) -> None:
    if not record.local_path or not Path(record.local_path).exists():
        queue.mark_failed(record.id, "Local file missing", retry=False)
        return

    folder_id = ensure_filester_folder(
        client, queue, settings, record.parent_folder_path, folder_cache
    )

    local_path = Path(record.local_path)
    size = local_path.stat().st_size
    logger.info("Uploading %s (%d bytes) to Filester", record.filename, size)

    result = client.upload_file(local_path, folder_id=folder_id)
    slug = result.get("slug", "")
    if not slug:
        raise RuntimeError(f"Upload returned no slug: {result}")

    if not client.verify_upload(slug, size):
        raise RuntimeError(f"Upload verification failed for {slug}")

    slugs = record.filester_slug + [slug]
    queue.update_file(
        record.id,
        status=FileStatus.UPLOADED,
        filester_slug=slugs,
        local_path=None,
    )

    # If this was a split part, mark parent uploaded when all parts are done
    if record.is_part and record.parent_file_id:
        with queue.connection() as conn:
            pending = conn.execute(
                """SELECT COUNT(*) as c FROM files
                   WHERE parent_file_id=? AND is_part=1 AND status != ?""",
                (record.parent_file_id, FileStatus.UPLOADED.value),
            ).fetchone()
            if pending and pending["c"] == 0:
                conn.execute(
                    "UPDATE files SET status=?, updated_at=? WHERE id=?",
                    (FileStatus.UPLOADED.value, utc_now(), record.parent_file_id),
                )

    # Cleanup local file and empty parent dir
    parent_dir = str(local_path.parent)
    cleanup_local([str(local_path)])
    try:
        if Path(parent_dir).exists() and not any(Path(parent_dir).iterdir()):
            Path(parent_dir).rmdir()
    except OSError:
        pass

    logger.info("Uploaded %s -> https://filester.me/d/%s", record.filename, slug)


def run_worker(settings: Settings | None = None) -> None:
    settings = settings or Settings.load()
    settings.ensure_dirs()
    setup_logging("uploader", settings.log_dir, settings.log_level)
    queue = QueueManager(settings.db_path)
    folder_cache: dict[str, str] = {}

    logger.info("Uploader worker started")

    with FilesterClient(
        settings.filester_api_key,
        settings.filester_api_base,
        max_retries=settings.upload_max_retries,
        retry_delay=settings.upload_retry_delay_sec,
    ) as client:
        while True:
            write_heartbeat(settings.state_dir)
            queue.reset_stale_jobs(settings.stale_job_timeout_sec)

            state, reason = queue.get_queue_state()
            if state != QueueState.RUNNING:
                logger.debug("Queue paused (%s): %s", state.value, reason)
                time.sleep(settings.worker_poll_interval_sec)
                continue

            job = queue.claim_upload_job()
            if not job:
                time.sleep(settings.worker_poll_interval_sec)
                continue

            logger.info("Processing upload job %d: %s", job.id, job.filename)
            try:
                process_upload(job, settings, queue, client, folder_cache)
            except Exception as exc:
                logger.error("Upload failed for %s: %s", job.filename, exc)
                if job.attempts >= settings.upload_max_retries:
                    queue.mark_failed(job.id, str(exc), retry=False)
                else:
                    queue.update_file(
                        job.id,
                        status=FileStatus.DOWNLOADED,
                        last_error=str(exc),
                    )


if __name__ == "__main__":
    run_worker()
