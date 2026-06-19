"""FastAPI dashboard and health endpoints."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Query

from migradora.config import Settings
from migradora.queue.manager import QueueManager

if TYPE_CHECKING:
    from migradora.orchestrator import Orchestrator


def _heartbeat_age(state_dir: str, service: str) -> float | None:
    path = Path(state_dir) / f"{service}.heartbeat"
    if not path.exists():
        return None
    try:
        return time.time() - float(path.read_text().strip())
    except (ValueError, OSError):
        return None


def create_app(settings: Settings, orchestrator: Orchestrator) -> FastAPI:
    app = FastAPI(title="Migradora", version="2.0.0", description="Gofile → Filester mirror via JDownloader2")
    queue = QueueManager(settings.db_path)

    @app.get("/health")
    def health() -> dict[str, Any]:
        pipeline_age = _heartbeat_age(settings.state_dir, "pipeline")
        orch_age = _heartbeat_age(settings.state_dir, "orchestrator")
        jd2_ok = orchestrator.jd2_healthy()
        return {
            "status": "ok" if jd2_ok else "degraded",
            "jdownloader_api": jd2_ok,
            "pipeline": {
                "alive": pipeline_age is not None and pipeline_age < settings.heartbeat_interval_sec * 3,
                "last_heartbeat_age_sec": pipeline_age,
            },
            "orchestrator": {
                "alive": orch_age is not None and orch_age < settings.heartbeat_interval_sec * 6,
                "last_heartbeat_age_sec": orch_age,
            },
            "queue_state": queue.get_queue_state()[0].value,
        }

    @app.get("/status")
    def status() -> dict[str, Any]:
        stats = queue.get_stats()
        state, pause_reason = queue.get_queue_state()
        filester_stats = orchestrator.filester_monitor.fetch_storage_stats()
        return {
            "queue_state": state.value,
            "pause_reason": pause_reason,
            "pipeline": orchestrator.pipeline.status,
            "jdownloader_api": orchestrator.jd2_healthy(),
            "stats": {
                "total": stats.total,
                "pending": stats.pending,
                "downloading": stats.downloading,
                "downloaded": stats.downloaded,
                "uploading": stats.uploading,
                "uploaded": stats.uploaded,
                "failed": stats.failed,
                "completion_pct": round(stats.completion_pct, 2),
                "total_bytes": stats.total_bytes,
                "uploaded_bytes": stats.uploaded_bytes,
            },
            "filester": filester_stats,
        }

    @app.get("/jobs")
    def jobs(
        status: str | None = Query(None),
        limit: int = Query(100, ge=1, le=500),
    ) -> dict[str, Any]:
        records = queue.list_files(status=status, limit=limit)
        return {
            "jobs": [
                {
                    "id": r.id,
                    "filename": r.filename,
                    "gofile_path": r.gofile_path,
                    "gofile_url": r.gofile_url,
                    "size_bytes": r.size_bytes,
                    "status": r.status.value,
                    "attempts": r.attempts,
                    "last_error": r.last_error,
                    "filester_slug": r.filester_slug,
                }
                for r in records
            ]
        }

    @app.post("/resume")
    def resume() -> dict[str, str]:
        orchestrator.resume()
        return {"status": "resumed"}

    @app.post("/retry-failed")
    def retry_failed() -> dict[str, Any]:
        count = queue.reset_failed_jobs()
        orchestrator.resume()
        return {"status": "ok", "reset": count}

    @app.post("/pause")
    def pause() -> dict[str, str]:
        orchestrator.pause()
        return {"status": "paused"}

    @app.post("/discover")
    def discover(force: bool = False) -> dict[str, Any]:
        result = orchestrator.discover(force=force)
        return {"status": "ok", **result}

    @app.post("/vpn/rotate")
    def vpn_rotate() -> dict[str, Any]:
        if not settings.vpn_enabled:
            return {"status": "error", "message": "Set VPN_ENABLED=true and use docker-compose.vpn.yml"}
        from migradora.vpn import get_egress_ip, rotate_vpn

        before = get_egress_ip(settings.gluetun_control_url)
        result = rotate_vpn(settings.gluetun_control_url)
        return {
            "status": "ok",
            "ip_before": result.get("ip_before") or before,
            "ip_after": result.get("ip_after"),
        }

    return app
