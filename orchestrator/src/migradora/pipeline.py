"""Serial pipeline: Gofile download -> split -> Filester upload -> cleanup."""

from __future__ import annotations

import logging
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path

from migradora.config import Settings
from migradora.filester_client import FilesterClient
from migradora.gofile_client import GofileClient
from migradora.models import FileStatus, QueueState
from migradora.oshash import compute_oshash, verify_oshash
from migradora.queue.manager import QueueManager
from migradora.splitter import iter_upload_parts
from migradora.size_limits import (
    disk_insufficient_skip_reason,
    oversize_skip_reason,
    required_disk_gb,
)
from migradora.stashdb_client import StashdbClient, resolve_stashdb_metadata
from migradora.transfer_stats import TransferTracker, eta_seconds, format_size
from migradora.upload_progress import UploadProgressReporter
from migradora.upload_resume import (
    UploadedPart,
    UploadResumeState,
    delete_upload_resume_state,
    load_upload_resume_state,
    save_upload_resume_state,
)
from migradora.utils import free_disk_gb

from migradora.filester_folders import (
    CachedFolder,
    ensure_filester_folder_path,
    organize_split_parts_into_folder,
)
from migradora.job_cleanup import cleanup_job_files, purge_stale_job_dirs, release_job_downloads
from migradora.job_log import JobLogStore

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
        self._job_logs = JobLogStore()
        self._upload_reporter: UploadProgressReporter | None = None
        self._activity_text: str = ""
        self._last_activity_at: float = 0.0

    def _set_activity(self, text: str, *, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_activity_at < 1.0:
            return
        self._last_activity_at = now
        self._activity_text = text.strip()

    def _append_job_log(self, job_id: int, line: str) -> None:
        self._job_logs.append(job_id, line)

    def job_logs(self, job_id: int, *, tail: int = 22) -> list[str]:
        return self._job_logs.get(job_id, tail=tail)

    def _touch_job_progress(self, job_id: int) -> None:
        """Keep stale-job detection and health heartbeats fresh during long transfers."""
        now = time.time()
        if now - self._last_touch_at < 30:
            return
        self._last_touch_at = now
        write_heartbeat(self.settings.state_dir)
        self.queue.touch_file(job_id)

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
        elif phase == "splitting":
            speed_bps = None
            phase_eta_sec = None
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
            "job_logs": self._job_logs.get(self._current_job_id or 0, tail=22)
            if self._current_job_id
            else [],
            "status_text": (
                self._upload_reporter.status_text
                if self._upload_reporter
                else self._activity_text
            ),
            "upload_progress": self._upload_reporter.snapshot()
            if self._upload_reporter
            else None,
        }

    def stop(self) -> None:
        self._stop.set()

    def request_skip(self, job_id: int) -> None:
        self._skip_job_id = job_id

    def _check_skip(self, job_id: int) -> None:
        if self._skip_job_id == job_id:
            raise JobSkipped(job_id)

    def _release_job(self, job_id: int, local_path: str | None = None) -> None:
        removed = release_job_downloads(self.settings, self.queue, job_id, local_path)
        if removed:
            logger.info("Released disk for job %d: %s", job_id, removed)

    def _finish_skip(self, job_id: int) -> None:
        record = self.queue.get_file(job_id)
        local_path = record.local_path if record else None
        self._release_job(job_id, local_path)
        self.queue.mark_skipped(job_id)
        self._skip_job_id = None
        self._current_phase = "idle"
        self._current_job_id = None
        self._current_job_name = ""
        self._progress_bytes = 0
        self._progress_total = 0
        self._upload_bytes_done = 0
        self._upload_bytes_total = 0
        self._upload_reporter = None
        self._activity_text = ""
        logger.info("Job %d skipped; local files removed", job_id)

    def _skip_job_for_disk(self, job, reason: str) -> None:
        self._release_job(job.id, job.local_path)
        self.queue.mark_skipped(job.id, reason)
        self._current_phase = "idle"
        self._current_job_id = None
        self._current_job_name = ""
        logger.warning("Skipped job %d (disk): %s", job.id, reason)

    def _try_resume_local_file(self, job, job_dir: Path) -> Path | None:
        candidates: list[Path] = []
        if job.local_path:
            candidates.append(Path(job.local_path))
        candidates.append(job_dir / job.filename)

        for path in candidates:
            if not path.is_file():
                continue
            size = path.stat().st_size
            if job.size_bytes and size != job.size_bytes:
                logger.warning(
                    "Job %d: skip resume %s — size %d != expected %d",
                    job.id,
                    path,
                    size,
                    job.size_bytes,
                )
                continue
            if job.oshash and not verify_oshash(path, job.oshash):
                logger.warning("Job %d: skip resume %s — OSHash mismatch", job.id, path)
                continue
            logger.info("Job %d: resuming from existing download %s", job.id, path)
            return path
        return None

    def _finish_upload_job(
        self,
        job,
        job_dir: Path,
        local_path: Path,
        resume_state: UploadResumeState,
        upload_responses: list[dict],
        slugs: list[str],
        was_split: bool,
        folder_id: str,
        filester: FilesterClient,
        job_log: Callable[..., None],
        upload_phase_started: bool,
    ) -> None:
        if was_split:
            cover_path: Path | None = None
            if resume_state.stashdb_cover_path:
                candidate = Path(resume_state.stashdb_cover_path)
                if candidate.is_file():
                    cover_path = candidate
            organize_split_parts_into_folder(
                filester,
                parent_folder_id=folder_id,
                folder_name=job.filename,
                folder_title=resume_state.stashdb_title,
                cover_image_path=cover_path,
                upload_responses=upload_responses,
            )
        if upload_phase_started:
            self._transfer.complete_phase("upload", self._upload_bytes_total)
        delete_upload_resume_state(job_dir)
        self._release_job(job.id, str(local_path))
        self.queue.update_file(
            job.id,
            status=FileStatus.UPLOADED,
            filester_slug=slugs,
        )
        self._current_phase = "idle"
        self._current_job_id = None
        self._current_job_name = ""
        self._progress_bytes = 0
        self._progress_total = 0
        self._upload_bytes_done = 0
        self._upload_bytes_total = 0
        self._upload_reporter = None
        self._activity_text = ""
        job_log(f"Job complete: {job.filename}")
        logger.info("Job %d complete: %s", job.id, job.filename)

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
                if (
                    state == QueueState.PAUSED_DISK
                    and self.settings.disk_pause_skip_job
                ):
                    self.queue.set_queue_state(
                        QueueState.RUNNING,
                        "DISK_PAUSE_SKIP_JOB skips jobs instead of pausing",
                    )
                else:
                    self._current_phase = f"paused:{state.value}"
                    time.sleep(self.settings.worker_poll_interval_sec)
                    continue

            if free_disk_gb(self.settings.download_dir) < self.settings.min_free_disk_gb:
                if self.settings.disk_pause_skip_job:
                    logger.warning(
                        "Free disk below %s GB; DISK_PAUSE_SKIP_JOB enabled — "
                        "continuing (oversized jobs will be skipped)",
                        self.settings.min_free_disk_gb,
                    )
                else:
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
                record = self.queue.get_file(job.id)
                self._release_job(
                    job.id,
                    record.local_path if record else None,
                )
                self._current_phase = "idle"
                self._current_job_id = None
                self._current_job_name = ""
                self._progress_bytes = 0
                self._progress_total = 0
                self._upload_bytes_done = 0
                self._upload_bytes_total = 0
                self._upload_reporter = None
                self._activity_text = ""
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

        purged = purge_stale_job_dirs(self.settings, self.queue, keep_job_id=job.id)
        if purged:
            logger.info("Purged %d stale job dir(s) before job %d", len(purged), job.id)

        skip_reason = oversize_skip_reason(job.size_bytes, self.settings)
        if skip_reason:
            logger.warning("Auto-skipping job %d (%s): %s", job.id, job.filename, skip_reason)
            self._release_job(job.id, None)
            self.queue.mark_skipped(job.id, skip_reason)
            self._current_phase = "idle"
            self._current_job_id = None
            self._current_job_name = ""
            return

        if job.size_bytes:
            need_gb = required_disk_gb(job.size_bytes, self.settings)
            free_gb = free_disk_gb(self.settings.download_dir)
            if free_gb < need_gb:
                reason = disk_insufficient_skip_reason(
                    job.filename, need_gb, free_gb
                )
                if self.settings.disk_pause_skip_job:
                    self._skip_job_for_disk(job, reason)
                    return
                self._release_job(job.id, job.local_path)
                self.queue.update_file(job.id, status=FileStatus.PENDING)
                self.queue.set_queue_state(QueueState.PAUSED_DISK, reason)
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

        resume_state = load_upload_resume_state(job_dir) or UploadResumeState()
        local_path = self._try_resume_local_file(job, job_dir)

        if local_path is None:
            self._current_phase = "downloading"
            self._progress_bytes = 0
            self._progress_total = job.size_bytes or 0
            self._upload_bytes_done = 0
            self._upload_bytes_total = 0
            self._last_touch_at = 0.0
            self._last_activity_at = 0.0
            self._activity_text = f"Downloading {job.filename}…"
            self._transfer.begin_phase("download")
            self.queue.update_file(job.id, status=FileStatus.DOWNLOADING)

            def on_download_progress(done: int, total: int | None) -> None:
                self._check_skip(job.id)
                self._progress_bytes = done
                if total:
                    self._progress_total = total
                self._transfer.update_progress("download", done)
                self._touch_job_progress(job.id)
                total_bytes = total or self._progress_total or done
                speed = self._transfer.download_bps
                pct = (done / total_bytes * 100.0) if total_bytes > 0 else 0.0
                eta = eta_seconds(max(0, total_bytes - done), speed)
                status = (
                    f"Downloading: {pct:.1f}% — {format_size(done)}/{format_size(total_bytes)}"
                )
                if speed:
                    status += f" @ {format_size(speed)}/s"
                if eta is not None and eta > 0:
                    status += f" — ETA {int(eta)}s"
                self._set_activity(status)

            url = job.gofile_url or job.download_link
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
            self._transfer.complete_phase("download", local_path.stat().st_size)
        else:
            actual = local_path.stat().st_size
            self._transfer.begin_phase("download")
            self._transfer.complete_phase("download", actual)
            self._append_job_log(job.id, f"Skipping download — using {local_path.name}")

        actual_size = local_path.stat().st_size
        if job.size_bytes and actual_size != job.size_bytes:
            logger.warning(
                "Size mismatch job %d: expected %d, got %d",
                job.id,
                job.size_bytes,
                actual_size,
            )

        oshash = compute_oshash(local_path)
        if job.oshash and not verify_oshash(local_path, job.oshash):
            logger.warning(
                "Job %d: OSHash mismatch on resume (stored %s, computed %s); "
                "re-storing computed hash",
                job.id,
                job.oshash,
                oshash,
            )

        self.queue.update_file(
            job.id,
            status=FileStatus.DOWNLOADED,
            local_path=str(local_path),
            oshash=oshash,
        )
        if not resume_state.oshash:
            resume_state.oshash = oshash
            resume_state.source_path = str(local_path)
            save_upload_resume_state(job_dir, resume_state)

        stashdb_client = StashdbClient(
            self.settings.stashdb_api_key,
            self.settings.stashdb_graphql_url,
        )
        scene_id, stash_title, cover_path = resolve_stashdb_metadata(
            stashdb_client,
            oshash,
            job_dir,
            existing_scene_id=resume_state.stashdb_scene_id,
            existing_title=resume_state.stashdb_title,
            existing_cover_path=resume_state.stashdb_cover_path,
        )
        if stash_title:
            resume_state.stashdb_scene_id = scene_id
            resume_state.stashdb_title = stash_title
            resume_state.stashdb_cover_path = cover_path
            save_upload_resume_state(job_dir, resume_state)
            self._append_job_log(job.id, f"StashDB: {stash_title}")

        self._progress_bytes = 0
        self._progress_total = actual_size
        self._upload_bytes_done = resume_state.uploaded_bytes()
        self._upload_bytes_total = actual_size

        upload_responses: list[dict] = [p.upload_response for p in resume_state.parts]
        slugs: list[str] = [p.slug for p in resume_state.parts]
        was_split = resume_state.was_split
        skip_part_indices = resume_state.skip_part_indices()
        upload_phase_started = False
        folder_path = _job_upload_folder_path(job) or job.gofile_path or "root"
        self._upload_reporter = UploadProgressReporter(
            folder_name=folder_path.rsplit("/", 1)[-1] if "/" in folder_path else folder_path,
        )
        if actual_size > self.settings.filester_max_file_bytes:
            self._upload_reporter.set_splitting(source_bytes=actual_size)

        def job_log(line: str, jid: int = job.id) -> None:
            self._append_job_log(jid, line)
            if self._upload_reporter:
                self._upload_reporter.set_activity(line)

        def on_parts_planned(count: int) -> None:
            resume_state.total_parts = count
            save_upload_resume_state(job_dir, resume_state)
            if self._upload_reporter:
                self._upload_reporter.prepare_parts(count)

        def on_split_progress(
            part_index: int,
            done_bytes: int,
            total_bytes: int,
            label: str,
            part_count: int,
        ) -> None:
            if self._upload_reporter:
                self._upload_reporter.set_split_part_progress(
                    part_index,
                    label=label,
                    done_bytes=done_bytes,
                    total_bytes=total_bytes,
                    part_count=part_count,
                )

        with FilesterClient(
            self.settings.filester_api_key,
            self.settings.filester_api_base,
            max_retries=self.settings.upload_max_retries,
            retry_delay=self.settings.upload_retry_delay_sec,
            upload_chunk_bytes=self.settings.filester_upload_chunk_bytes,
            upload_write_timeout_sec=self.settings.filester_upload_write_timeout_sec,
            upload_throttle_kbps=self.settings.upload_throttle_kbps,
        ) as filester:
            folder_id = ensure_filester_folder_path(
                filester,
                self.queue,
                self.settings,
                _job_upload_folder_path(job),
                self._folder_cache,
            )
            logger.info(
                "Job %d split/upload to Filester folder %s (gofile path %r)",
                job.id,
                folder_id,
                _job_upload_folder_path(job) or job.gofile_path,
            )
            job_log(
                f"[Filester] Split/upload to folder {folder_id} "
                f"({_job_upload_folder_path(job) or job.gofile_path or 'root'})"
            )
            if skip_part_indices:
                job_log(
                    f"Resuming upload — {len(skip_part_indices)} part(s) "
                    f"already on Filester, skipping re-split where possible"
                )

            if resume_state.upload_complete():
                upload_phase_started = bool(resume_state.parts)
                self._finish_upload_job(
                    job,
                    job_dir,
                    local_path,
                    resume_state,
                    upload_responses,
                    slugs,
                    was_split,
                    folder_id,
                    filester,
                    job_log,
                    upload_phase_started,
                )
                return

            parts_iter = iter(
                iter_upload_parts(
                    local_path,
                    job_dir,
                    self.settings.filester_max_file_bytes,
                    base_name=local_path.stem,
                    skip_check=lambda: self._check_skip(job.id),
                    split_mode=self.settings.filester_split_mode,
                    ffmpeg_bin=self.settings.ffmpeg_bin,
                    ffprobe_bin=self.settings.ffprobe_bin,
                    mkvmerge_bin=self.settings.mkvmerge_bin,
                    ffmpeg_timeout=self.settings.ffmpeg_timeout_sec,
                    skip_part_indices=skip_part_indices,
                    reuse_existing_parts=True,
                    on_log=job_log,
                    on_parts_planned=on_parts_planned,
                    on_split_progress=on_split_progress,
                )
            )
            while True:
                self._current_phase = "splitting"
                self.queue.update_file(job.id, status=FileStatus.SPLITTING)
                try:
                    part = next(parts_iter)
                except StopIteration:
                    break

                if int(part.get("part_count") or 1) > 1:
                    was_split = True
                part_path = Path(part["path"])
                part_size = part["size_bytes"]
                part_index = int(part.get("part_index") or 1)
                part_count = int(part.get("part_count") or 1)
                if resume_state.total_parts is None:
                    resume_state.total_parts = part_count
                    save_upload_resume_state(job_dir, resume_state)
                part_base_done = self._upload_bytes_done

                if part_count > 1:
                    self._upload_reporter.register_part(
                        part_index,
                        part["filename"],
                        part_size,
                        part_count,
                    )

                self._current_phase = "uploading"
                if not upload_phase_started:
                    self._transfer.begin_phase("upload")
                    self.queue.update_file(job.id, status=FileStatus.UPLOADING)
                    upload_phase_started = True

                self._progress_bytes = 0
                self._progress_total = part_size
                if part_count > 1:
                    line = (
                        f"[Filester] Uploading part {part_index}/{part_count}: "
                        f"{part['filename']} ({format_size(part_size)})"
                    )
                    logger.info(
                        "Uploading part %d/%d: %s (%s)",
                        part_index,
                        part_count,
                        part["filename"],
                        format_size(part_size),
                    )
                else:
                    line = (
                        f"[Filester] Uploading {part['filename']} "
                        f"({format_size(part_size)})"
                    )
                    logger.info(
                        "Uploading %s (%s)",
                        part["filename"],
                        format_size(part_size),
                    )
                job_log(line)

                def on_upload_progress(done: int, total: int) -> None:
                    self._check_skip(job.id)
                    self._progress_bytes = done
                    self._progress_total = total
                    cumulative = part_base_done + done
                    self._upload_bytes_done = cumulative
                    self._transfer.update_progress("upload", cumulative)
                    self._touch_job_progress(job.id)
                    speed = self._transfer.upload_bps
                    eta = eta_seconds(max(0, total - done), speed)
                    if self._upload_reporter:
                        if part_count > 1:
                            self._upload_reporter.part_progress(
                                part_index,
                                done,
                                total,
                                speed_bps=speed,
                                eta_sec=eta,
                            )
                        else:
                            self._upload_reporter.single_progress(
                                done,
                                total,
                                speed_bps=speed,
                                eta_sec=eta,
                            )

                result = filester.upload_file(
                    part_path,
                    folder_id=folder_id,
                    on_progress=on_upload_progress,
                    on_log=job_log,
                )
                upload_responses.append(result)
                slug = result.get("slug", "")
                if not slug:
                    raise RuntimeError(f"Upload returned no slug: {result}")
                if not filester.verify_upload(slug, part_size):
                    raise RuntimeError(f"Upload verification failed: {slug}")
                slugs.append(slug)
                resume_state.parts.append(
                    UploadedPart(
                        part_index=part_index,
                        filename=part["filename"],
                        size_bytes=part_size,
                        slug=slug,
                        upload_response=result,
                    )
                )
                resume_state.was_split = part_count > 1
                resume_state.total_parts = part_count
                save_upload_resume_state(job_dir, resume_state)
                self.queue.update_file(job.id, filester_slug=slugs)
                self._upload_bytes_done = part_base_done + part_size
                if part_count > 1 and self._upload_reporter:
                    self._upload_reporter.complete_part(part_index)
                cleanup_dir(part_path)
                filester.reset_connections()

            self._finish_upload_job(
                job,
                job_dir,
                local_path,
                resume_state,
                upload_responses,
                slugs,
                was_split,
                folder_id,
                filester,
                job_log,
                upload_phase_started,
            )
