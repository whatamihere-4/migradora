"""Serial pipeline: Gofile download -> split -> Filester upload -> cleanup."""

from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path

from migradora.config import Settings
from migradora.filester_client import FilesterClient
from migradora.gofile_client import GofileClient
from migradora.models import FileStatus, QueueState
from migradora.queue.manager import QueueManager
from migradora.splitter import split_file
from migradora.utils import free_disk_gb

from migradora.filester_folders import CachedFolder, ensure_filester_folder_path

logger = logging.getLogger("migradora.pipeline")


def write_heartbeat(state_dir: str) -> None:
    Path(state_dir, "pipeline.heartbeat").write_text(str(time.time()))


def cleanup_dir(path: Path) -> None:
    if path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


class PipelineCoordinator:
    def __init__(self, settings: Settings, queue: QueueManager) -> None:
        self.settings = settings
        self.queue = queue
        self._stop = threading.Event()
        self._current_job_id: int | None = None
        self._current_phase: str = "idle"
        self._current_job_name: str = ""
        self._folder_cache: dict[str, CachedFolder] = {}
        self._progress_bytes: int = 0
        self._progress_total: int = 0
        self._last_touch_at: float = 0.0

    @property
    def status(self) -> dict:
        return {
            "current_job_id": self._current_job_id,
            "current_job_name": self._current_job_name,
            "phase": self._current_phase,
            "progress_bytes": self._progress_bytes,
            "progress_total": self._progress_total,
        }

    def stop(self) -> None:
        self._stop.set()

    def run_loop(self) -> None:
        logger.info("Pipeline started")
        while not self._stop.is_set():
            write_heartbeat(self.settings.state_dir)
            exclude = [self._current_job_id] if self._current_job_id else []
            self.queue.reset_stale_jobs(
                self.settings.stale_job_timeout_sec, exclude_ids=exclude
            )

            state, _ = self.queue.get_queue_state()
            if state != QueueState.RUNNING:
                self._current_phase = f"paused:{state.value}"
                time.sleep(self.settings.worker_poll_interval_sec)
                continue

            if free_disk_gb(self.settings.download_dir) < self.settings.min_free_disk_gb:
                self.queue.set_queue_state(
                    QueueState.PAUSED_DISK,
                    f"Free disk below {self.settings.min_free_disk_gb} GB",
                )
                continue

            job = self.queue.claim_pending_job()
            if not job:
                self._current_phase = "idle"
                self._current_job_id = None
                time.sleep(self.settings.worker_poll_interval_sec)
                continue

            self._current_job_id = job.id
            self._current_job_name = job.filename
            try:
                self._process_job(job)
            except Exception as exc:
                logger.error("Pipeline failed for job %d: %s", job.id, exc)
                self._progress_bytes = 0
                self._progress_total = 0
                if job.attempts >= self.settings.download_max_retries:
                    self.queue.mark_failed(job.id, str(exc), retry=False)
                else:
                    self.queue.mark_failed(job.id, str(exc), retry=True)
                    time.sleep(self.settings.download_retry_delay_sec)

        logger.info("Pipeline stopped")

    def _process_job(self, job) -> None:
        url = job.gofile_url or job.download_link
        if not url:
            raise RuntimeError(f"Job {job.id} has no gofile_url")

        job_dir = Path(self.settings.download_dir) / f"job-{job.id}"
        job_dir.mkdir(parents=True, exist_ok=True)

        self._current_phase = "downloading"
        self._progress_bytes = 0
        self._progress_total = job.size_bytes or 0
        self._last_touch_at = 0.0
        self.queue.update_file(job.id, status=FileStatus.DOWNLOADING)

        def on_download_progress(done: int, total: int | None) -> None:
            self._progress_bytes = done
            if total:
                self._progress_total = total
            now = time.time()
            if now - self._last_touch_at >= 30:
                self._last_touch_at = now
                self.queue.touch_file(job.id)

        with GofileClient(
            token=self.settings.gofile_token,
            password=self.settings.gofile_password,
        ) as gofile:
            dest = gofile.safe_dest_path(job_dir, job.filename)
            gofile.download_file(
                url,
                str(dest),
                expected_size=job.size_bytes or None,
                throttle_kbps=self.settings.download_throttle_kbps,
                on_progress=on_download_progress,
            )

        local_path = dest
        actual_size = local_path.stat().st_size
        if job.size_bytes and actual_size != job.size_bytes:
            logger.warning(
                "Size mismatch job %d: expected %d, got %d",
                job.id,
                job.size_bytes,
                actual_size,
            )

        self.queue.update_file(
            job.id,
            status=FileStatus.DOWNLOADED,
            local_path=str(local_path),
        )

        self._current_phase = "uploading"
        self._progress_bytes = 0
        self._progress_total = local_path.stat().st_size
        self.queue.update_file(job.id, status=FileStatus.UPLOADING)
        parts = split_file(
            local_path,
            job_dir,
            self.settings.filester_max_file_bytes,
            base_name=local_path.stem,
        )

        slugs: list[str] = []
        with FilesterClient(
            self.settings.filester_api_key,
            self.settings.filester_api_base,
            max_retries=self.settings.upload_max_retries,
            retry_delay=self.settings.upload_retry_delay_sec,
        ) as filester:
            folder_id = ensure_filester_folder_path(
                filester, self.queue, self.settings, job.parent_folder_path, self._folder_cache
            )
            for part in parts:
                part_path = Path(part["path"])
                self._progress_bytes = 0
                self._progress_total = part["size_bytes"]
                logger.info("Uploading %s (%d bytes)", part["filename"], part["size_bytes"])
                result = filester.upload_file(part_path, folder_id=folder_id)
                slug = result.get("slug", "")
                if not slug:
                    raise RuntimeError(f"Upload returned no slug: {result}")
                if not filester.verify_upload(slug, part["size_bytes"]):
                    raise RuntimeError(f"Upload verification failed: {slug}")
                slugs.append(slug)
                cleanup_dir(part_path)
                logger.info("Uploaded -> https://filester.me/d/%s", slug)

        cleanup_dir(job_dir)
        self.queue.update_file(
            job.id,
            status=FileStatus.UPLOADED,
            filester_slug=slugs,
            local_path=None,
        )
        self._current_phase = "idle"
        self._current_job_id = None
        self._current_job_name = ""
        self._progress_bytes = 0
        self._progress_total = 0
        logger.info("Job %d complete: %s", job.id, job.filename)
