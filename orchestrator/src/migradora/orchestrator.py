"""Main orchestrator: monitors, discovery, pipeline coordinator."""

from __future__ import annotations

import logging
import threading
import time

from migradora.config import Settings
from migradora.discovery.jd2_discovery import discover_and_enqueue
from migradora.jdownloader.client import JDownloaderClient
from migradora.logger import setup_logging
from migradora.models import QueueState
from migradora.monitor.filester_storage import FilesterStorageMonitor
from migradora.pipeline import PipelineCoordinator
from migradora.queue.manager import QueueManager

logger = logging.getLogger("migradora.orchestrator")


def write_heartbeat(state_dir: str) -> None:
    from pathlib import Path
    Path(state_dir, "orchestrator.heartbeat").write_text(str(time.time()))


class Orchestrator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.queue = QueueManager(settings.db_path)
        self.filester_monitor = FilesterStorageMonitor(settings, self.queue)
        self.pipeline = PipelineCoordinator(settings, self.queue)
        self._stop = threading.Event()
        self._jd2_client = JDownloaderClient(
            host=settings.jd2_host,
            port=settings.jd2_port,
            timeout_sec=settings.jd2_api_timeout_sec,
        )

    def monitor_loop(self) -> None:
        interval = max(60, int(self.settings.worker_poll_interval_sec * 12))
        while not self._stop.is_set():
            write_heartbeat(self.settings.state_dir)
            self.queue.reset_stale_jobs(self.settings.stale_job_timeout_sec)

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

    def jd2_healthy(self) -> bool:
        return self._jd2_client.health()

    def stop(self) -> None:
        self._stop.set()
        self.pipeline.stop()


def run_orchestrator(settings: Settings | None = None) -> None:
    settings = settings or Settings.load()
    settings.ensure_dirs()
    setup_logging("orchestrator", settings.log_dir, settings.log_level)

    from migradora.jd2_config import (
        ensure_general_settings,
        ensure_remote_api_enabled,
        jd2_initialized,
    )
    if jd2_initialized("/jd2-config"):
        jd2_changed = ensure_remote_api_enabled("/jd2-config", "/templates")
        jd2_changed = ensure_general_settings("/jd2-config", "/output", "/templates") or jd2_changed
        if jd2_changed:
            logger.warning(
                "JD2 config was updated — restart jdownloader: "
                "docker compose restart jdownloader"
            )
    else:
        logger.warning(
            "JD2 not initialized. Start with empty data/jd2/config, wait for "
            "web UI :5800, then run: ./scripts/jd2-enable-api.sh"
        )

    orch = Orchestrator(settings)
    state, reason = orch.queue.get_queue_state()
    if state.value == "paused_traffic" and "GB" in (reason or ""):
        logger.warning(
            "Queue is paused_traffic from an old Gofile account traffic check (%s). "
            "That monitor was removed — this is not your VPS usage. "
            "Run: python -m migradora resume  (or enable VPN + rotate IP for download blocks)",
            reason,
        )
    orch.start_background()
    logger.info("Orchestrator started (pipeline + monitors)")

    from migradora.api.app import create_app
    import uvicorn

    app = create_app(settings, orch)
    uvicorn.run(
        app,
        host=settings.dashboard_host,
        port=settings.dashboard_bind_port,
        log_level=settings.log_level.lower(),
    )
