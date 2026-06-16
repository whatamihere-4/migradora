"""FastAPI dashboard and health endpoints."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from migradora.config import Settings
from migradora.models import QueueState
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
    app = FastAPI(title="Migradora", version="1.0.0", description="Gofile → Filester mirror")
    queue = QueueManager(settings.db_path)

    @app.get("/health")
    def health() -> dict[str, Any]:
        workers = {}
        for svc in ("downloader", "uploader", "orchestrator"):
            age = _heartbeat_age(settings.state_dir, svc)
            workers[svc] = {
                "alive": age is not None and age < settings.heartbeat_interval_sec * 3,
                "last_heartbeat_age_sec": age,
            }
        return {
            "status": "ok",
            "workers": workers,
            "queue_state": queue.get_queue_state()[0].value,
        }

    @app.get("/status")
    def status() -> dict[str, Any]:
        stats = queue.get_stats()
        state, pause_reason = queue.get_queue_state()
        gofile_stats = orchestrator.gofile_monitor.fetch_account_stats()
        filester_stats = orchestrator.filester_monitor.fetch_storage_stats()
        return {
            "queue_state": state.value,
            "pause_reason": pause_reason,
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
            "gofile": gofile_stats,
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

    @app.post("/pause")
    def pause() -> dict[str, str]:
        orchestrator.pause()
        return {"status": "paused"}

    @app.post("/discover")
    def discover(force: bool = False) -> dict[str, Any]:
        result = orchestrator.discover(force=force)
        return {"status": "ok", **result}

    return app
