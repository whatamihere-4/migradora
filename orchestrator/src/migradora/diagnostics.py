"""One-shot transfer diagnostics: upload speed vs Filester API pressure."""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any

import httpx

from migradora.config import Settings
from migradora.monitor.filester_storage import FilesterStorageMonitor
from migradora.queue.manager import QueueManager
from migradora.transfer_stats import format_speed

_RATE_LIMIT_RE = re.compile(
    r"rate.?limit|\b429\b|Upload attempt \d+ failed",
    re.IGNORECASE,
)
_FFMPEG_LOG_MARKERS = ("migradora.ffmpeg_splitter", "[ffmpeg]")


def _heartbeat_age(state_dir: str, service: str) -> float | None:
    path = Path(state_dir) / f"{service}.heartbeat"
    if not path.exists():
        return None
    try:
        return time.time() - float(path.read_text().strip())
    except (ValueError, OSError):
        return None


def _fetch_dashboard_status(settings: Settings) -> dict[str, Any] | None:
    url = f"http://127.0.0.1:{settings.webui_port}/status"
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(url)
            resp.raise_for_status()
            payload = resp.json()
            return payload if isinstance(payload, dict) else None
    except httpx.HTTPError:
        return None


def _scan_log_tail(log_dir: str, *, max_lines: int = 500) -> list[str]:
    path = Path(log_dir) / "migradora.log"
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    hits: list[str] = []
    for line in lines[-max_lines:]:
        if any(marker in line for marker in _FFMPEG_LOG_MARKERS):
            continue
        if _RATE_LIMIT_RE.search(line):
            hits.append(line.strip())
    return hits[-10:]


def _api_pressure_level(per_min_5: float) -> str:
    if per_min_5 >= 10:
        return "critical"
    if per_min_5 >= 3:
        return "warn"
    return "ok"


def _upload_ratio(download_bps: float | None, upload_bps: float | None) -> float | None:
    if not download_bps or not upload_bps or download_bps <= 0:
        return None
    return upload_bps / download_bps


def _assess(
    *,
    phase: str,
    speed_bps: float | None,
    download_bps: float | None,
    upload_bps: float | None,
    per_min_5: float,
    rate_limit_hits: list[str],
    pipeline_heartbeat_age: float | None,
    heartbeat_interval_sec: int,
) -> list[str]:
    notes: list[str] = []
    uploading = phase == "uploading"
    ratio = _upload_ratio(download_bps, upload_bps)
    slow_upload = uploading and (
        speed_bps is None
        or speed_bps < 5_000_000
        or (download_bps and download_bps >= 10_000_000 and ratio is not None and ratio < 0.2)
    )
    pressure = _api_pressure_level(per_min_5)
    heartbeat_stale = (
        uploading
        and pipeline_heartbeat_age is not None
        and pipeline_heartbeat_age > heartbeat_interval_sec * 3
    )

    if heartbeat_stale:
        notes.append(
            f"Pipeline heartbeat is {pipeline_heartbeat_age:.0f}s old during an active "
            f"{phase} — upgrade to a build that heartbeats during transfers (fixed in recent "
            f"versions). Health checks will show degraded even while work continues."
        )

    if slow_upload and ratio is not None and download_bps and download_bps >= 10_000_000:
        notes.append(
            f"Upload is only {ratio * 100:.0f}% of download speed "
            f"({format_speed(upload_bps)} vs {format_speed(download_bps)}). "
            "This is not normal for a healthy VPS→Filester link."
        )

    if slow_upload and pressure in ("warn", "critical"):
        notes.append(
            "Filester account API calls are elevated — close dashboard tabs and confirm "
            "FILESTER_STATS_CACHE_SEC=60."
        )
    elif slow_upload and rate_limit_hits:
        notes.append(
            "Recent logs show rate limiting or upload retries. "
            "Check `docker compose logs orchestrator | grep -iE 'rate limit|429|Upload attempt'`."
        )
    elif slow_upload:
        notes.append(
            "Run `./scripts/test-filester-upload-speed.sh` to measure raw VPS→Filester "
            "throughput outside the queue. If that is also slow, the bottleneck is network "
            "or Filester — not migradora API polling."
        )
        notes.append(
            "If raw upload speed is fine but jobs degrade over time, try smaller parts "
            "(e.g. FILESTER_MAX_FILE_BYTES=2147483648) so each upload resets the TCP "
            "connection more often."
        )
    elif pressure == "critical":
        notes.append(
            "Filester account API is being polled heavily (>10 calls/min). "
            "This can throttle uploads — close extra dashboard tabs."
        )
    elif pressure == "warn":
        notes.append(
            "Filester account API call rate is elevated. Monitor upload speed; "
            "expect ~1 call/min from the background monitor with caching enabled."
        )
    elif heartbeat_stale:
        pass
    else:
        notes.append("Transfer speed and Filester API pressure look healthy.")

    return notes


def run_transfer_diagnostics(
    settings: Settings,
    *,
    refresh_filester: bool = False,
) -> dict[str, Any]:
    queue = QueueManager(settings.db_path)
    monitor = FilesterStorageMonitor(settings, queue)
    api_stats = queue.bandwidth_log_stats()
    rate_limit_hits = _scan_log_tail(settings.log_dir)

    pipeline_age = _heartbeat_age(settings.state_dir, "pipeline")
    orch_age = _heartbeat_age(settings.state_dir, "orchestrator")
    dashboard = _fetch_dashboard_status(settings)

    pipeline: dict[str, Any] = {}
    filester_cached: dict[str, Any] = {}
    if dashboard:
        pipeline = dashboard.get("pipeline") or {}
        filester_cached = dashboard.get("filester") or {}

    if refresh_filester:
        filester_live = monitor.fetch_storage_stats(log=False)
    else:
        filester_live = monitor.get_storage_stats()

    phase = str(pipeline.get("phase") or "idle")
    speed_bps = pipeline.get("speed_bps")
    if speed_bps is not None:
        speed_bps = float(speed_bps)
    download_bps = pipeline.get("avg_download_bps")
    upload_bps = pipeline.get("avg_upload_bps")
    if download_bps is not None:
        download_bps = float(download_bps)
    if upload_bps is not None:
        upload_bps = float(upload_bps)

    windows = api_stats.get("windows") or {}
    per_min_5 = float((windows.get("5") or {}).get("per_min") or 0)
    ratio = _upload_ratio(download_bps, upload_bps)

    return {
        "orchestrator": {
            "dashboard_reachable": dashboard is not None,
            "pipeline_heartbeat_age_sec": pipeline_age,
            "orchestrator_heartbeat_age_sec": orch_age,
            "pipeline_heartbeat_stale": (
                pipeline_age is not None
                and phase in ("downloading", "uploading")
                and pipeline_age > settings.heartbeat_interval_sec * 3
            ),
        },
        "transfer": {
            "phase": phase,
            "current_job_id": pipeline.get("current_job_id"),
            "current_job_name": pipeline.get("current_job_name"),
            "instant_speed_bps": speed_bps,
            "instant_speed": format_speed(speed_bps),
            "avg_download_bps": download_bps,
            "avg_upload_bps": upload_bps,
            "avg_download": format_speed(download_bps),
            "avg_upload": format_speed(upload_bps),
            "upload_to_download_ratio": ratio,
            "progress_bytes": pipeline.get("progress_bytes"),
            "progress_total": pipeline.get("progress_total"),
            "upload_bytes_done": pipeline.get("upload_bytes_done"),
            "upload_bytes_total": pipeline.get("upload_bytes_total"),
        },
        "filester_api": {
            "stats_cache_sec": settings.filester_stats_cache_sec,
            "bandwidth_log_rows": api_stats.get("total_rows"),
            "account_calls": api_stats.get("windows"),
            "api_requests_today": filester_live.get("api_requests_today")
            or filester_cached.get("api_requests_today"),
            "live_fetch": refresh_filester,
            "error": filester_live.get("error"),
        },
        "logs": {
            "rate_limit_hits_recent": rate_limit_hits,
        },
        "assessment": _assess(
            phase=phase,
            speed_bps=speed_bps,
            download_bps=download_bps,
            upload_bps=upload_bps,
            per_min_5=per_min_5,
            rate_limit_hits=rate_limit_hits,
            pipeline_heartbeat_age=pipeline_age,
            heartbeat_interval_sec=settings.heartbeat_interval_sec,
        ),
    }


def _print_human(report: dict[str, Any]) -> None:
    orch = report["orchestrator"]
    transfer = report["transfer"]
    api = report["filester_api"]
    logs = report["logs"]

    print("=== Migradora transfer diagnostics ===\n")

    if orch["dashboard_reachable"]:
        pipe_age = orch["pipeline_heartbeat_age_sec"]
        age_label = f"{pipe_age:.0f}s ago" if pipe_age is not None else "unknown"
        stale = " [STALE]" if orch.get("pipeline_heartbeat_stale") else ""
        print(f"Orchestrator: running (pipeline heartbeat {age_label}{stale})")
    else:
        print("Orchestrator: dashboard not reachable on localhost (is `migradora run` active?)")

    phase = transfer["phase"]
    job_id = transfer["current_job_id"]
    job_name = transfer["current_job_name"] or ""
    if job_id and phase in ("downloading", "uploading"):
        print(f"\nCurrent job: #{job_id} {job_name} [{phase}]")
        print(f"  Instant speed: {transfer['instant_speed']}")
        print(f"  Avg download:  {transfer['avg_download']}")
        print(f"  Avg upload:    {transfer['avg_upload']}")
        ratio = transfer.get("upload_to_download_ratio")
        if ratio is not None:
            print(f"  Upload/download ratio: {ratio * 100:.0f}%")
        done = transfer.get("upload_bytes_done") or transfer.get("progress_bytes") or 0
        total = transfer.get("upload_bytes_total") or transfer.get("progress_total") or 0
        if total:
            pct = round((done / total) * 100)
            print(f"  Progress:      {done:,} / {total:,} bytes ({pct}%)")
    else:
        print(f"\nPipeline phase: {phase}")

    print("\nFilester API pressure")
    print(f"  bandwidth_log rows:  {api.get('bandwidth_log_rows', 0):,}")
    print(f"  Stats cache TTL:     {api.get('stats_cache_sec')}s")
    calls = api.get("account_calls") or {}
    for label, title in (("1", "1 min"), ("5", "5 min"), ("15", "15 min")):
        block = calls.get(label) or {}
        count = int(block.get("count") or 0)
        per_min = float(block.get("per_min") or 0)
        flag = ""
        if label == "5":
            level = _api_pressure_level(per_min)
            if level == "critical":
                flag = "  [HIGH]"
            elif level == "warn":
                flag = "  [elevated]"
            else:
                flag = "  [OK]"
        print(f"  Account calls ({title}): {count:4d}  ({per_min:.1f}/min){flag}")

    today = api.get("api_requests_today")
    if today is not None:
        print(f"  api_requests_today:  {today}")
    if api.get("error"):
        print(f"  Filester error:      {api['error']}")

    hits = logs.get("rate_limit_hits_recent") or []
    print("\nRecent rate limits / upload retries (log tail)")
    if hits:
        for line in hits:
            print(f"  {line}")
    else:
        print("  none in last 500 lines")

    print("\nAssessment")
    for note in report.get("assessment") or []:
        print(f"  {note}")


def configure_diag_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Force a live Filester /account fetch (does not write bandwidth_log)",
    )


def run_diag_args(args: argparse.Namespace, settings: Settings) -> int:
    report = run_transfer_diagnostics(settings, refresh_filester=args.refresh)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_human(report)
    return 0
