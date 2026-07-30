"""Remove on-disk files left behind by a queue job."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from migradora.models import FileStatus
from migradora.queue.manager import QueueManager

logger = logging.getLogger("migradora.job_cleanup")

_KEEP_DIR_STATUSES = frozenset({
    FileStatus.DOWNLOADING,
    FileStatus.UPLOADING,
    FileStatus.DOWNLOADED,
})


def cleanup_job_files(
    settings,
    job_id: int,
    local_path: str | None = None,
) -> list[str]:
    """Delete job download dir and any recorded local_path. Returns removed paths."""
    removed: list[str] = []
    job_dir = Path(settings.download_dir) / f"job-{job_id}"
    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)
        removed.append(str(job_dir))

    if local_path:
        path = Path(local_path)
        if path.exists():
            if path.is_file():
                path.unlink(missing_ok=True)
            else:
                shutil.rmtree(path, ignore_errors=True)
            removed.append(str(path))

    return removed


def release_job_downloads(
    settings,
    queue: QueueManager,
    job_id: int,
    local_path: str | None = None,
) -> list[str]:
    """Delete on-disk job data and clear ``local_path`` in the queue."""
    removed = cleanup_job_files(settings, job_id, local_path)
    queue.clear_local_path(job_id)
    return removed


def purge_stale_job_dirs(
    settings,
    queue: QueueManager,
    *,
    keep_job_id: int | None = None,
) -> list[str]:
    """Remove ``job-*`` dirs for jobs that are not actively transferring.

    Called when starting a new job so failed/skipped/completed leftovers do not
    fill the disk. The job currently being processed (``keep_job_id``) is never
    removed; dirs for jobs in downloading/uploading/downloaded states are kept.
    """
    removed: list[str] = []
    base = Path(settings.download_dir)
    if not base.is_dir():
        return removed

    for path in sorted(base.iterdir()):
        if not path.is_dir() or not path.name.startswith("job-"):
            continue
        suffix = path.name.removeprefix("job-")
        try:
            job_id = int(suffix)
        except ValueError:
            continue
        if keep_job_id is not None and job_id == keep_job_id:
            continue

        record = queue.get_file(job_id)
        if record and record.status in _KEEP_DIR_STATUSES:
            continue

        shutil.rmtree(path, ignore_errors=True)
        removed.append(str(path))
        if record and record.local_path:
            queue.clear_local_path(job_id)
        logger.info(
            "Purged stale download dir for job %d (status=%s)",
            job_id,
            record.status.value if record else "unknown",
        )

    return removed


def clear_all_downloads(download_dir: str) -> list[str]:
    """Remove all job-* directories under the download root."""
    removed: list[str] = []
    base = Path(download_dir)
    if not base.is_dir():
        return removed
    for path in base.iterdir():
        if path.is_dir() and path.name.startswith("job-"):
            shutil.rmtree(path, ignore_errors=True)
            removed.append(str(path))
    return removed
