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
from migradora.splitter import iter_upload_parts
from migradora.size_limits import oversize_skip_reason, required_disk_gb
from migradora.transfer_stats import TransferTracker, eta_seconds
from migradora.utils import free_disk_gb

from migradora.filester_folders import (
    CachedFolder,
    ensure_filester_folder_path,
    organize_split_parts_into_folder,
)
from migradora.job_cleanup import cleanup_job_files

logger = logging.getLogger("migradora.pipeline")


class JobSkipped(Exception):
    def __init__(self, job_id: int) -> None:
        self.job_id = job_id
        super().__init__(f"Job {job_id} skipped")


def write_heartbeat(state_dir: str) -> None:
    Path(state_dir, "pipeline.heartbeat").write_text(str(time.time()))


def _job_upload_folder_path(job) -> str:
    if job.parent_folder_path:
        return job.parent_folder_path
    gofile_path = job.gofile_path or ""
    if "/" in gofile_path:
        return gofile_path.rsplit("/", 1)[0]
    return ""


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
        self._upload_bytes_done: int = 0
        self._upload_bytes_total: int = 0
        self._last_touch_at: float = 0.0
        self._skip_job_id: int | None = None
        self._transfer = TransferTracker()

    @property
    def status(self) -> dict:
        phase = self._current_phase
        speed_bps: float | None = None
        phase_eta_sec: float | None = None
        if phase == "downloading":
            speed_bps = self._transfer.download_bps
            phase_eta_sec = eta_seconds(
                max(0, self._progress_total - self._progress_bytes),
                speed_bps,
            )
        elif phase == "uploading":
            speed_bps = self._transfer.upload_bps
            phase_eta_sec = eta_seconds(
                max(0, self._upload_bytes_total - self._upload_bytes_done),
                speed_bps,
            )
        return {
            "current_job_id": self._current_job_id,
            "current_job_name": self._current_job_name,
            "phase": phase,
            "progress_bytes": self._progress_bytes,
            "progress_total": self._progress_total,
            "upload_bytes_done": self._upload_bytes_done,
            "upload_bytes_total": self._upload_bytes_total,
            "speed_bps": speed_bps,
            "phase_eta_sec": phase_eta_sec,
            "avg_download_bps": self._transfer.download_bps,
            "avg_upload_bps": self._transfer.upload_bps,
        }

    def stop(self) -> None:
        self._stop.set()

    def request_skip(self, job_id: int) -> None:
        self._skip_job_id = job_id

    def _check_skip(self, job_id: int) -> None:
        if self._skip_job_id == job_id:
            raise JobSkipped(job_id)

    def _finish_skip(self, job_id: int) -> None:
        record = self.queue.get_file(job_id)
        local_path = record.local_path if record else None
        cleanup_job_files(self.settings, job_id, local_path)
        self.queue.mark_skipped(job_id)
        self._skip_job_id = None
        self._current_phase = "idle"
        self._current_job_id = None
        self._current_job_name = ""
        self._progress_bytes = 0
        self._progress_total = 0
        self._upload_bytes_done = 0
        self._upload_bytes_total = 0
        logger.info("Job %d skipped; local files removed", job_id)

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
            except JobSkipped as exc:
                self._finish_skip(exc.job_id)
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

        skip_reason = oversize_skip_reason(job.size_bytes, self.settings)
        if skip_reason:
            logger.warning("Auto-skipping job %d (%s): %s", job.id, job.filename, skip_reason)
            self.queue.mark_skipped(job.id, skip_reason)
            self._current_phase = "idle"
            self._current_job_id = None
            self._current_job_name = ""
            return

        if job.size_bytes:
            need_gb = required_disk_gb(job.size_bytes, self.settings)
            free_gb = free_disk_gb(self.settings.download_dir)
            if free_gb < need_gb:
                self.queue.update_file(job.id, status=FileStatus.PENDING)
                self.queue.set_queue_state(
                    QueueState.PAUSED_DISK,
                    f"Need ~{need_gb:.0f} GB free for {job.filename} ({free_gb:.1f} GB available)",
                )
                self._current_phase = "idle"
                self._current_job_id = None
                self._current_job_name = ""
                logger.warning(
                    "Paused for disk: job %d needs ~%.0f GB, %.1f GB free",
                    job.id,
                    need_gb,
                    free_gb,
                )
                return

        self._current_phase = "downloading"
        self._progress_bytes = 0
        self._progress_total = job.size_bytes or 0
        self._upload_bytes_done = 0
        self._upload_bytes_total = 0
        self._last_touch_at = 0.0
        self._transfer.begin_phase("download")
        self.queue.update_file(job.id, status=FileStatus.DOWNLOADING)

        def on_download_progress(done: int, total: int | None) -> None:
            self._check_skip(job.id)
            self._progress_bytes = done
            if total:
                self._progress_total = total
            self._transfer.update_progress("download", done)
            now = time.time()
            if now - self._last_touch_at >= 30:
                self._last_touch_at = now
                self.queue.touch_file(job.id)

        with GofileClient(
            token=self.settings.gofile_token,
            password=self.settings.gofile_password,
            cdn_prefer=self.settings.gofile_cdn_prefer,
            cdn_probe=self.settings.gofile_cdn_probe,
            download_connections=self.settings.gofile_download_connections,
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
        self._transfer.complete_phase("download", actual_size)

        self._current_phase = "uploading"
        self._progress_bytes = 0
        self._progress_total = actual_size
        self._upload_bytes_done = 0
        self._upload_bytes_total = actual_size
        self._transfer.begin_phase("upload")
        self.queue.update_file(job.id, status=FileStatus.UPLOADING)

        slugs: list[str] = []
        with FilesterClient(
            self.settings.filester_api_key,
            self.settings.filester_api_base,
            max_retries=self.settings.upload_max_retries,
            retry_delay=self.settings.upload_retry_delay_sec,
        ) as filester:
            folder_id = ensure_filester_folder_path(
                filester,
                self.queue,
                self.settings,
                _job_upload_folder_path(job),
                self._folder_cache,
            )
            logger.info(
                "Job %d uploading to Filester folder %s (gofile path %r)",
                job.id,
                folder_id,
                _job_upload_folder_path(job) or job.gofile_path,
            )
            was_split = False
            upload_responses: list[dict] = []
            for part in iter_upload_parts(
                local_path,
                job_dir,
                self.settings.filester_max_file_bytes,
                base_name=local_path.stem,
                skip_check=lambda: self._check_skip(job.id),
                split_mode=self.settings.filester_split_mode,
                ffmpeg_bin=self.settings.ffmpeg_bin,
                ffprobe_bin=self.settings.ffprobe_bin,
                ffmpeg_timeout=self.settings.ffmpeg_timeout_sec,
            ):
                self._check_skip(job.id)
                if int(part.get("part_count") or 1) > 1:
                    was_split = True
                part_path = Path(part["path"])
                part_size = part["size_bytes"]
                part_base_done = self._upload_bytes_done
                self._progress_bytes = 0
                self._progress_total = part_size
                logger.info("Uploading %s (%d bytes)", part["filename"], part_size)

                def on_upload_progress(done: int, total: int) -> None:
                    self._check_skip(job.id)
                    self._progress_bytes = done
                    self._progress_total = total
                    cumulative = part_base_done + done
                    self._upload_bytes_done = cumulative
                    self._transfer.update_progress("upload", cumulative)

                result = filester.upload_file(
                    part_path,
                    folder_id=folder_id,
                    on_progress=on_upload_progress,
                )
                upload_responses.append(result)
                slug = result.get("slug", "")
                if not slug:
                    raise RuntimeError(f"Upload returned no slug: {result}")
                if not filester.verify_upload(slug, part_size):
                    raise RuntimeError(f"Upload verification failed: {slug}")
                slugs.append(slug)
                self._upload_bytes_done = part_base_done + part_size
                cleanup_dir(part_path)
                logger.info("Uploaded -> https://filester.me/d/%s", slug)

            if was_split:
                organize_split_parts_into_folder(
                    filester,
                    parent_folder_id=folder_id,
                    folder_name=job.filename,
                    upload_responses=upload_responses,
                )

        self._transfer.complete_phase("upload", self._upload_bytes_total)

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
        self._upload_bytes_done = 0
        self._upload_bytes_total = 0
        logger.info("Job %d complete: %s", job.id, job.filename)
