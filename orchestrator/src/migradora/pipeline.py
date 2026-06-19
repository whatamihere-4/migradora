"""Serial pipeline: JD2 download -> split -> Filester upload -> cleanup."""

from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path

import httpx

from migradora.config import Settings
from migradora.filester_client import FilesterClient
from migradora.jdownloader.client import JDownloaderClient, _as_int_list
from migradora.models import FileStatus, QueueState
from migradora.queue.manager import QueueManager
from migradora.splitter import split_file
from migradora.utils import free_disk_gb
from migradora.vpn import is_gofile_traffic_block, rotate_vpn

logger = logging.getLogger("migradora.pipeline")


def _is_transient_jd2_error(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, TimeoutError)):
        return True
    if isinstance(exc, OSError) and getattr(exc, "errno", None) in (111, 113):
        return True
    msg = str(exc).lower()
    return "connection refused" in msg or "connect error" in msg


def write_heartbeat(state_dir: str) -> None:
    Path(state_dir, "pipeline.heartbeat").write_text(str(time.time()))


def find_completed_file(dest_dir: Path, stable_sec: float = 3.0) -> Path:
    """Return largest stable file under dest_dir (searches subdirs). No .part files."""
    if not dest_dir.is_dir():
        raise FileNotFoundError(f"Download directory missing: {dest_dir}")
    candidates = [
        p for p in dest_dir.rglob("*")
        if p.is_file() and not p.name.endswith(".part")
    ]
    if not candidates:
        raise FileNotFoundError(f"No completed file in {dest_dir}")
    # Prefer largest file (main download)
    candidates.sort(key=lambda p: p.stat().st_size, reverse=True)
    path = candidates[0]
    size1 = path.stat().st_size
    time.sleep(stable_sec)
    size2 = path.stat().st_size
    if size1 != size2:
        raise RuntimeError(f"File still growing: {path}")
    return path


def cleanup_dir(path: Path) -> None:
    if path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def ensure_filester_folder(
    client: FilesterClient,
    queue: QueueManager,
    settings: Settings,
    parent_folder_path: str,
    cache: dict[str, str],
) -> str | None:
    if not parent_folder_path:
        root_id = queue.get_folder_mapping("__root__")
        if root_id:
            return root_id
        root_id = client.create_folder(settings.filester_root_folder_name)
        queue.save_folder_mapping("__root__", root_id, settings.filester_root_folder_name)
        cache["__root__"] = root_id
        return root_id
    existing = queue.get_folder_mapping(parent_folder_path)
    if existing:
        return existing
    folder_name = parent_folder_path.replace("/", " - ")[-100:]
    folder_id = client.create_folder(folder_name)
    queue.save_folder_mapping(parent_folder_path, folder_id, folder_name)
    cache[parent_folder_path] = folder_id
    return folder_id


class PipelineCoordinator:
    def __init__(self, settings: Settings, queue: QueueManager) -> None:
        self.settings = settings
        self.queue = queue
        self._stop = threading.Event()
        self._current_job_id: int | None = None
        self._current_phase: str = "idle"
        self._folder_cache: dict[str, str] = {}

    @property
    def status(self) -> dict:
        return {
            "current_job_id": self._current_job_id,
            "phase": self._current_phase,
        }

    def stop(self) -> None:
        self._stop.set()

    def _handle_gofile_traffic_block(self, job_id: int, message: str) -> None:
        logger.warning("Gofile traffic/IP block detected for job %d: %s", job_id, message)
        self.queue.mark_failed(job_id, message, retry=True)

        if self.settings.vpn_enabled and self.settings.vpn_rotate_on_ban:
            try:
                result = rotate_vpn(self.settings.gluetun_control_url)
                logger.info(
                    "VPN rotated after Gofile block (%s -> %s)",
                    result.get("ip_before"),
                    result.get("ip_after"),
                )
                self.queue.set_queue_state(
                    QueueState.RUNNING,
                    "VPN rotated after Gofile traffic block",
                )
                return
            except Exception as exc:
                logger.error("VPN rotate failed: %s", exc)

        self.queue.set_queue_state(
            QueueState.PAUSED_TRAFFIC,
            f"Gofile traffic/IP block — rotate VPN then resume: {message[:200]}",
        )

    def run_loop(self) -> None:
        logger.info("Pipeline coordinator started")
        jd2_ready = False
        while not self._stop.is_set():
            write_heartbeat(self.settings.state_dir)
            self.queue.reset_stale_jobs(self.settings.stale_job_timeout_sec)

            state, reason = self.queue.get_queue_state()
            if state != QueueState.RUNNING:
                self._current_phase = f"paused:{state.value}"
                time.sleep(self.settings.worker_poll_interval_sec)
                continue

            if not jd2_ready:
                self._current_phase = "waiting:jd2"
                try:
                    with JDownloaderClient(
                        host=self.settings.jd2_host,
                        port=self.settings.jd2_port,
                        timeout_sec=self.settings.jd2_api_timeout_sec,
                    ) as jd2:
                        jd2.wait_until_healthy(
                            timeout_sec=self.settings.jd2_startup_wait_sec,
                            interval_sec=self.settings.jd2_poll_interval_sec,
                        )
                    jd2_ready = True
                    logger.info("JDownloader API ready at %s:%d", self.settings.jd2_host, self.settings.jd2_port)
                except TimeoutError as exc:
                    logger.warning("%s — retrying", exc)
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
            try:
                self._process_job(job)
            except Exception as exc:
                logger.error("Pipeline failed for job %d: %s", job.id, exc)
                if is_gofile_traffic_block(str(exc)):
                    self._handle_gofile_traffic_block(job.id, str(exc))
                    continue
                if _is_transient_jd2_error(exc):
                    logger.warning("Transient JD2 error for job %d, requeueing", job.id)
                    self.queue.requeue_job(job.id, str(exc))
                    jd2_ready = False
                    time.sleep(self.settings.download_retry_delay_sec)
                    continue
                if job.attempts >= self.settings.download_max_retries:
                    self.queue.mark_failed(job.id, str(exc), retry=False)
                else:
                    self.queue.mark_failed(job.id, str(exc), retry=True)

        logger.info("Pipeline coordinator stopped")

    def _process_job(self, job) -> None:
        url = job.gofile_url or job.download_link
        if not url:
            raise RuntimeError(f"Job {job.id} has no gofile_url")

        pkg_name = f"migradora-{job.id}"
        # JD2 (UID 1000) must create dirs under /output — do not mkdir from orchestrator (root).
        jd2_dest = self.settings.jd2_download_dir.rstrip("/")
        local_dest = Path(self.settings.download_dir) / pkg_name

        self._current_phase = "downloading"
        self.queue.update_file(job.id, jd2_package_name=pkg_name)

        with JDownloaderClient(
            host=self.settings.jd2_host,
            port=self.settings.jd2_port,
            timeout_sec=self.settings.jd2_api_timeout_sec,
            poll_interval_sec=self.settings.jd2_poll_interval_sec,
        ) as jd2:
            jd2.wait_until_healthy(
                timeout_sec=min(60.0, self.settings.jd2_startup_wait_sec),
                interval_sec=self.settings.jd2_poll_interval_sec,
            )
            jd2.add_and_start_package(
                url,
                package_name=pkg_name,
                destination_folder=jd2_dest,
                download_password=self.settings.gofile_password,
                crawl_timeout_sec=self.settings.jd2_crawl_timeout_sec,
                expected_size=job.size_bytes or None,
            )
            links = jd2.wait_until_package_finished(
                pkg_name,
                url=url,
                expected_size=job.size_bytes or None,
            )
            # Update size from JD2 if we didn't know it
            if links and not job.size_bytes:
                total = sum(int(l.get("bytesTotal") or 0) for l in links)
                if total:
                    self.queue.update_file(job.id, status=FileStatus.DOWNLOADING)

        local_path = find_completed_file(local_dest)
        actual_size = local_path.stat().st_size
        if job.size_bytes and actual_size != job.size_bytes:
            logger.warning(
                "Size mismatch job %d: expected %d, got %d",
                job.id, job.size_bytes, actual_size,
            )

        self.queue.update_file(
            job.id,
            status=FileStatus.DOWNLOADED,
            local_path=str(local_path),
        )

        self._current_phase = "uploading"
        parts = split_file(
            local_path,
            local_dest,
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
            folder_id = ensure_filester_folder(
                filester, self.queue, self.settings, job.parent_folder_path, self._folder_cache
            )
            for part in parts:
                part_path = Path(part["path"])
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

        cleanup_dir(local_dest)
        try:
            jd2_pkg_cleanup = JDownloaderClient(
                host=self.settings.jd2_host,
                port=self.settings.jd2_port,
            )
            packages = jd2_pkg_cleanup.query_download_packages(package_name=pkg_name)
            pkg_ids = _as_int_list([p.get("uuid") for p in packages])
            jd2_pkg_cleanup.remove_downloads(package_ids=pkg_ids)
            jd2_pkg_cleanup.close()
        except Exception as exc:
            logger.warning("JD2 cleanup failed for %s: %s", pkg_name, exc)

        self.queue.update_file(
            job.id,
            status=FileStatus.UPLOADED,
            filester_slug=slugs,
            local_path=None,
        )
        self._current_phase = "idle"
        self._current_job_id = None
        logger.info("Job %d complete: %s", job.id, job.filename)
