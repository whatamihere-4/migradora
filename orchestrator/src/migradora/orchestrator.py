"""Main orchestrator: monitors, discovery, pipeline."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from migradora.config import Settings
from migradora.discovery.api_discovery import discover_and_enqueue
from migradora.logger import setup_logging
from migradora.models import QueueState
from migradora.monitor.filester_storage import FilesterStorageMonitor
from migradora.pipeline import PipelineCoordinator
from migradora.queue.manager import QueueManager

logger = logging.getLogger("migradora.orchestrator")


def write_heartbeat(state_dir: str) -> None:
    Path(state_dir, "orchestrator.heartbeat").write_text(str(time.time()))


class Orchestrator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.queue = QueueManager(settings.db_path)
        self.filester_monitor = FilesterStorageMonitor(settings, self.queue)
        self.pipeline = PipelineCoordinator(settings, self.queue)
        self._stop = threading.Event()

    def monitor_loop(self) -> None:
        interval = max(60, int(self.settings.worker_poll_interval_sec * 12))
        while not self._stop.is_set():
            write_heartbeat(self.settings.state_dir)
            exclude = (
                [self.pipeline._current_job_id]
                if self.pipeline._current_job_id
                else []
            )
            self.queue.reset_stale_jobs(
                self.settings.stale_job_timeout_sec, exclude_ids=exclude
            )

            state, _ = self.queue.get_queue_state()
            if state == QueueState.PAUSED_DISK:
                from migradora.utils import free_disk_gb

                free_gb = free_disk_gb(self.settings.download_dir)
                if free_gb >= self.settings.min_free_disk_gb:
                    self.queue.set_queue_state(QueueState.RUNNING, "")
                    logger.info("Disk space recovered (%.1f GB free), resuming", free_gb)

            self.filester_monitor.check_and_pause()
            self._stop.wait(interval)

    def start_background(self) -> list[threading.Thread]:
        threads = []
        monitor = threading.Thread(target=self.monitor_loop, daemon=True, name="monitor-loop")
        monitor.start()
        threads.append(monitor)
        pipeline = threading.Thread(target=self.pipeline.run_loop, daemon=True, name="pipeline")
        pipeline.start()
        threads.append(pipeline)
        return threads

    def discover(self, force: bool = False) -> dict:
        return discover_and_enqueue(self.settings, force=force)

    def resume(self) -> None:
        self.queue.set_queue_state(QueueState.RUNNING, "")
        logger.info("Queue resumed")

    def pause(self, reason: str = "manual") -> None:
        self.queue.set_queue_state(QueueState.PAUSED, reason)
        logger.info("Queue paused: %s", reason)

    def stop(self) -> None:
        self._stop.set()
        self.pipeline.stop()


def run_orchestrator(settings: Settings | None = None) -> None:
    settings = settings or Settings.load()
    settings.ensure_dirs()
    setup_logging("orchestrator", settings.log_dir, settings.log_level)

    if not settings.gofile_token:
        logger.error("GOFILE_TOKEN is required (premium Gofile account)")
    if not settings.gofile_folder_urls:
        logger.warning("GOFILE_FOLDER_URLS is empty — run discover after adding folder links")

    orch = Orchestrator(settings)
    cleared = orch.queue.clear_flat_folder_mappings()
    if cleared:
        logger.warning("Cleared %d flat Filester folder mapping(s) — will recreate nested folders", cleared)
    reset = orch.queue.reset_active_jobs()
    if reset:
        logger.warning("Reset %d orphaned downloading/uploading job(s) to pending", reset)
    state, reason = orch.queue.get_queue_state()
    if state == QueueState.PAUSED_TRAFFIC:
        logger.warning(
            "Queue paused_traffic (%s) — legacy state from old setup. Run: python -m migradora resume",
            reason,
        )
    orch.start_background()
    logger.info("Orchestrator started")

    from migradora.api.app import create_app
    import uvicorn

    app = create_app(settings, orch)
    uvicorn.run(
        app,
        host=settings.dashboard_host,
        port=settings.webui_port,
        log_level=settings.log_level.lower(),
    )
