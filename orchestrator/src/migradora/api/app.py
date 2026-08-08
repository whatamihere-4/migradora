"""FastAPI dashboard and health endpoints."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from migradora.config import Settings
from migradora.queue.manager import QueueManager
from migradora.realdebrid_client import is_realdebrid_url, RealDebridClient, RealDebridError
from migradora.transfer_stats import (
    compute_queue_eta,
    compute_remaining_bytes,
    format_eta,
)

if TYPE_CHECKING:
    from migradora.orchestrator import Orchestrator

_DASHBOARD_HTML = Path(__file__).resolve().parents[3] / "templates" / "dashboard.html"
_REALDEBRID_HTML = Path(__file__).resolve().parents[3] / "templates" / "realdebrid.html"


class RealDebridSubmitBody(BaseModel):
    url: str = Field(..., min_length=8)
    job_id: int | None = None
    filename: str | None = None
    parent_folder_path: str | None = None


def _rd_cdn_status(settings: Settings) -> dict[str, Any]:
    raw = (settings.real_debrid_preferred_cdn or "").strip()
    if not raw:
        return {"state": "unset", "winner": None}
    return {"state": "pinned", "winner": raw}


def _job_download_source(record) -> str:
    if is_realdebrid_url(record.download_link) or is_realdebrid_url(record.gofile_url):
        return "realdebrid"
    return "gofile"


def _heartbeat_age(state_dir: str, service: str) -> float | None:
    path = Path(state_dir) / f"{service}.heartbeat"
    if not path.exists():
        return None
    try:
        return time.time() - float(path.read_text().strip())
    except (ValueError, OSError):
        return None


def create_app(settings: Settings, orchestrator: Orchestrator) -> FastAPI:
    app = FastAPI(title="Migradora", version="3.0.0", description="Gofile → Filester mirror")
    queue = QueueManager(settings.db_path)

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> HTMLResponse:
        return HTMLResponse(_DASHBOARD_HTML.read_text(encoding="utf-8"))

    @app.get("/realdebrid", response_class=HTMLResponse)
    def realdebrid_page() -> HTMLResponse:
        return HTMLResponse(_REALDEBRID_HTML.read_text(encoding="utf-8"))

    @app.get("/health")
    def health() -> dict[str, Any]:
        pipeline_age = _heartbeat_age(settings.state_dir, "pipeline")
        orch_age = _heartbeat_age(settings.state_dir, "orchestrator")
        ok = (
            pipeline_age is not None
            and pipeline_age < settings.heartbeat_interval_sec * 3
            and orch_age is not None
            and orch_age < settings.heartbeat_interval_sec * 6
        )
        return {
            "status": "ok" if ok else "degraded",
            "gofile_token_set": bool(settings.gofile_token),
            "real_debrid_token_set": bool(settings.real_debrid_api_token),
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

    def _job_payload(record, *, job_logs: list[str] | None = None) -> dict[str, Any]:
        payload = {
            "id": record.id,
            "filename": record.filename,
            "gofile_path": record.gofile_path,
            "parent_folder_path": record.parent_folder_path,
            "gofile_url": record.gofile_url,
            "size_bytes": record.size_bytes,
            "status": record.status.value,
            "attempts": record.attempts,
            "last_error": record.last_error,
            "filester_slug": record.filester_slug,
            "download_source": _job_download_source(record),
        }
        if job_logs:
            payload["job_logs"] = job_logs
        return payload

    @app.get("/status")
    def status() -> dict[str, Any]:
        stats = queue.get_stats()
        state, pause_reason = queue.get_queue_state()
        filester_stats = orchestrator.filester_monitor.get_storage_stats()
        pipeline = orchestrator.pipeline.status
        current_job = None
        current_job_size = 0
        job_id = pipeline.get("current_job_id")
        phase = pipeline.get("phase")
        if job_id and phase in ("downloading", "splitting", "uploading"):
            record = queue.get_file(job_id)
            if record:
                logs = orchestrator.pipeline.job_logs(job_id)
                current_job = _job_payload(record, job_logs=logs)
                current_job["status"] = phase
                current_job["status_text"] = pipeline.get("status_text") or ""
                current_job["upload_progress"] = pipeline.get("upload_progress")
                current_job_size = record.size_bytes or 0

        incomplete_bytes = queue.get_incomplete_bytes_total()
        remaining = compute_remaining_bytes(
            incomplete_bytes=incomplete_bytes,
            current_job_id=job_id,
            current_job_size=current_job_size,
            phase=phase or "",
            progress_bytes=int(pipeline.get("progress_bytes") or 0),
            upload_bytes_done=int(pipeline.get("upload_bytes_done") or 0),
            upload_bytes_total=int(pipeline.get("upload_bytes_total") or 0),
        )
        queue_eta = compute_queue_eta(
            remaining,
            pipeline.get("avg_download_bps"),
            pipeline.get("avg_upload_bps"),
        )

        return {
            "queue_state": state.value,
            "pause_reason": pause_reason,
            "pipeline": pipeline,
            "current_job": current_job,
            "eta": {
                "queue_sec": queue_eta["queue_sec"],
                "queue_label": format_eta(queue_eta["queue_sec"]),
                "download_sec": queue_eta["download_sec"],
                "download_label": format_eta(queue_eta["download_sec"]),
                "upload_sec": queue_eta["upload_sec"],
                "upload_label": format_eta(queue_eta["upload_sec"]),
                "phase_sec": pipeline.get("phase_eta_sec"),
                "phase_label": format_eta(pipeline.get("phase_eta_sec")),
                "remaining_download_bytes": remaining.download_bytes,
                "remaining_upload_bytes": remaining.upload_bytes,
                "avg_download_bps": pipeline.get("avg_download_bps"),
                "avg_upload_bps": pipeline.get("avg_upload_bps"),
            },
            "stats": {
                "total": stats.total,
                "pending": stats.pending,
                "downloading": stats.downloading,
                "downloaded": stats.downloaded,
                "splitting": stats.splitting,
                "uploading": stats.uploading,
                "uploaded": stats.uploaded,
                "failed": stats.failed,
                "skipped": stats.skipped,
                "disk_skipped": queue.count_disk_skipped_jobs(),
                "completion_pct": round(stats.completion_pct, 2),
                "total_bytes": stats.total_bytes,
                "uploaded_bytes": stats.uploaded_bytes,
            },
            "filester": filester_stats,
        }

    @app.get("/jobs")
    def jobs(
        status: str | None = Query(None),
        path: str | None = Query(
            None,
            description="Filter by gofile_path or parent_folder_path prefix (e.g. SLR)",
        ),
        limit: int = Query(500, ge=1, le=2000),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        total = queue.count_files(status=status, path_prefix=path)
        records = queue.list_files(
            status=status,
            limit=limit,
            offset=offset,
            path_prefix=path,
        )
        jobs = []
        for record in records:
            logs = orchestrator.pipeline.job_logs(record.id)
            jobs.append(_job_payload(record, job_logs=logs or None))
        return {
            "jobs": jobs,
            "total": total,
            "limit": limit,
            "offset": offset,
            "path_prefix": path or "",
        }

    @app.post("/resume")
    def resume() -> dict[str, str]:
        orchestrator.resume()
        return {"status": "resumed"}

    @app.post("/retry-failed")
    def retry_failed() -> dict[str, Any]:
        count = queue.reset_failed_jobs()
        exclude = (
            [orchestrator.pipeline._current_job_id]
            if orchestrator.pipeline._current_job_id
            else []
        )
        active = queue.reset_active_jobs(exclude_ids=exclude)
        orchestrator.resume()
        return {"status": "ok", "reset": count, "reset_active": active}

    @app.post("/retry-disk-skipped")
    def retry_disk_skipped() -> dict[str, Any]:
        count = queue.reset_disk_skipped_jobs()
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

    @app.post("/jobs/{job_id}/skip")
    def skip_job(job_id: int) -> dict[str, Any]:
        try:
            return orchestrator.skip_job(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/realdebrid/config")
    def realdebrid_config() -> dict[str, Any]:
        return {
            "token_set": bool(settings.real_debrid_api_token),
            "cdn": _rd_cdn_status(settings),
        }

    @app.get("/api/realdebrid/jobs")
    def realdebrid_jobs(limit: int = Query(200, ge=1, le=500)) -> dict[str, Any]:
        records = queue.list_jobs_for_rd_fallback(limit=limit)
        jobs = []
        for record in records:
            jobs.append(
                {
                    "id": record.id,
                    "filename": record.filename,
                    "parent_folder_path": record.parent_folder_path,
                    "gofile_path": record.gofile_path,
                    "size_bytes": record.size_bytes,
                    "status": record.status.value,
                    "last_error": record.last_error,
                }
            )
        return {"jobs": jobs}

    @app.post("/api/realdebrid/submit")
    def realdebrid_submit(body: RealDebridSubmitBody) -> dict[str, Any]:
        url = body.url.strip()
        if not is_realdebrid_url(url):
            raise HTTPException(
                status_code=400,
                detail="URL must be a Real-Debrid panel link (real-debrid.com/d/…) or CDN URL",
            )

        size_bytes = 0
        filename_hint = (body.filename or "").strip()
        try:
            with RealDebridClient(settings) as rd:
                meta = rd.resolve_metadata(url)
                size_bytes = int(meta.get("filesize") or 0)
                rd_name = (meta.get("filename") or "").strip()
                if rd_name and not filename_hint:
                    filename_hint = rd_name
        except RealDebridError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if body.job_id is not None:
            record = queue.get_file(body.job_id)
            if not record:
                raise HTTPException(status_code=404, detail=f"Job {body.job_id} not found")
            ok = queue.assign_realdebrid_link(
                body.job_id,
                url,
                size_bytes=size_bytes if size_bytes > 0 else None,
            )
            if not ok:
                raise HTTPException(status_code=404, detail=f"Job {body.job_id} not found")
            orchestrator.resume()
            return {
                "status": "ok",
                "job_id": body.job_id,
                "filename": record.filename,
                "parent_folder_path": record.parent_folder_path,
                "size_bytes": size_bytes or record.size_bytes,
            }

        folder = (body.parent_folder_path or "").strip().strip("/")
        name = filename_hint or (body.filename or "").strip()
        if not name:
            raise HTTPException(
                status_code=400,
                detail="filename is required when creating a new job",
            )
        try:
            job_id = queue.enqueue_realdebrid_job(
                filename=name,
                parent_folder_path=folder,
                rd_url=url,
                size_bytes=size_bytes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        orchestrator.resume()
        return {
            "status": "ok",
            "job_id": job_id,
            "filename": name,
            "parent_folder_path": folder,
            "size_bytes": size_bytes,
        }

    return app
