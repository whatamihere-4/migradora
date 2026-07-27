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
    r"rate limit|429|Upload attempt \d+ failed",
    re.IGNORECASE,
)


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
        if _RATE_LIMIT_RE.search(line):
            hits.append(line.strip())
    return hits[-10:]


def _api_pressure_level(per_min_5: float) -> str:
    if per_min_5 >= 10:
        return "critical"
    if per_min_5 >= 3:
        return "warn"
    return "ok"


def _assess(
    *,
    phase: str,
    speed_bps: float | None,
    per_min_5: float,
    rate_limit_hits: list[str],
) -> list[str]:
    notes: list[str] = []
    uploading = phase == "uploading"
    slow_upload = uploading and (speed_bps is None or speed_bps < 1_000_000)
    pressure = _api_pressure_level(per_min_5)

    if slow_upload and pressure in ("warn", "critical"):
        notes.append(
            "Upload is under 1 MB/s while Filester account API calls are elevated. "
            "Close dashboard tabs, confirm FILESTER_STATS_CACHE_SEC is set (default 60), "
            "and redeploy if you are on an older build."
        )
    elif slow_upload and rate_limit_hits:
        notes.append(
            "Upload is under 1 MB/s and recent logs show rate limiting or upload retries. "
            "Check `docker compose logs orchestrator` for 429 responses."
        )
    elif slow_upload:
        notes.append(
            "Upload is under 1 MB/s but Filester API pressure looks normal. "
            "Check VPS disk I/O, network, and Filester service status."
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
    else:
        notes.append("Upload speed and Filester API pressure look healthy.")

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

    windows = api_stats.get("windows") or {}
    per_min_5 = float((windows.get("5") or {}).get("per_min") or 0)

    return {
        "orchestrator": {
            "dashboard_reachable": dashboard is not None,
            "pipeline_heartbeat_age_sec": pipeline_age,
            "orchestrator_heartbeat_age_sec": orch_age,
        },
        "transfer": {
            "phase": phase,
            "current_job_id": pipeline.get("current_job_id"),
            "current_job_name": pipeline.get("current_job_name"),
            "instant_speed_bps": speed_bps,
            "instant_speed": format_speed(speed_bps),
            "avg_download_bps": pipeline.get("avg_download_bps"),
            "avg_upload_bps": pipeline.get("avg_upload_bps"),
            "avg_download": format_speed(
                float(pipeline["avg_download_bps"])
                if pipeline.get("avg_download_bps") is not None
                else None
            ),
            "avg_upload": format_speed(
                float(pipeline["avg_upload_bps"])
                if pipeline.get("avg_upload_bps") is not None
                else None
            ),
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
            per_min_5=per_min_5,
            rate_limit_hits=rate_limit_hits,
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
        print(f"Orchestrator: running (pipeline heartbeat {age_label})")
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
    print("\nRecent rate limits (log tail)")
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
