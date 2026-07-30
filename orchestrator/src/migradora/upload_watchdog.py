"""Upload speed watchdog — restart orchestrator when Filester uploads stay too slow.

Only reacts during the ``uploading`` pipeline phase (not ``splitting``).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from migradora.config import Settings
from migradora.job_cleanup import release_job_downloads
from migradora.models import FileStatus, QueueState
from migradora.queue.manager import QueueManager
from migradora.transfer_stats import format_speed

logger = logging.getLogger("migradora.upload_watchdog")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_RESTART = 2


def _state_path(settings: Settings) -> Path:
    return Path(settings.state_dir) / "upload-watchdog.json"


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _fetch_status(settings: Settings) -> dict[str, Any] | None:
    url = f"http://127.0.0.1:{settings.webui_port}/status"
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url)
            resp.raise_for_status()
            payload = resp.json()
            return payload if isinstance(payload, dict) else None
    except httpx.HTTPError as exc:
        logger.warning("Watchdog could not reach /status: %s", exc)
        return None


def _min_bps(settings: Settings) -> float:
    return settings.upload_watchdog_min_mbps * 1024 * 1024


def prepare_job_restart(settings: Settings, job_id: int, reason: str) -> list[str]:
    queue = QueueManager(settings.db_path)
    record = queue.get_file(job_id)
    removed = release_job_downloads(
        settings,
        queue,
        job_id,
        record.local_path if record else None,
    )
    queue.rewind_job(job_id, reason=reason)
    queue.set_queue_state(QueueState.RUNNING, "")
    return removed


def run_watchdog_once(
    settings: Settings,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Check upload speed once; update state file; optionally prepare a restart."""
    now = time.time()
    state_path = _state_path(settings)
    state = _load_state(state_path)
    min_bps = _min_bps(settings)

    result: dict[str, Any] = {
        "enabled": settings.upload_watchdog_enabled,
        "min_mbps": settings.upload_watchdog_min_mbps,
        "min_bps": min_bps,
        "sustain_sec": settings.upload_watchdog_sustain_sec,
        "action": "none",
        "restart_required": False,
    }

    if not settings.upload_watchdog_enabled:
        result["action"] = "disabled"
        return result

    last_restart = float(state.get("last_restart_at") or 0)
    if last_restart and (now - last_restart) < settings.upload_watchdog_cooldown_sec:
        remaining = int(settings.upload_watchdog_cooldown_sec - (now - last_restart))
        result["action"] = "cooldown"
        result["cooldown_remaining_sec"] = remaining
        _save_state(state_path, state)
        return result

    status = _fetch_status(settings)
    if not status:
        result["action"] = "status_unreachable"
        return result

    pipeline = status.get("pipeline") or {}
    phase = str(pipeline.get("phase") or "idle")
    job_id = pipeline.get("current_job_id")
    speed_bps = pipeline.get("speed_bps")
    if speed_bps is not None:
        speed_bps = float(speed_bps)

    result.update({
        "phase": phase,
        "job_id": job_id,
        "speed_bps": speed_bps,
        "speed": format_speed(speed_bps),
    })

    if phase != "uploading":
        state["low_since"] = None
        state["last_job_id"] = job_id
        _save_state(state_path, state)
        result["action"] = "idle"
        return result

    if speed_bps is None or speed_bps <= 0:
        result["action"] = "waiting_for_speed"
        _save_state(state_path, state)
        return result

    if speed_bps >= min_bps:
        state["low_since"] = None
        state["last_speed_bps"] = speed_bps
        state["last_job_id"] = job_id
        _save_state(state_path, state)
        result["action"] = "ok"
        return result

    low_since = state.get("low_since")
    if not low_since or state.get("last_job_id") != job_id:
        state["low_since"] = now
        state["last_job_id"] = job_id
        state["last_speed_bps"] = speed_bps
        _save_state(state_path, state)
        result["action"] = "slow_started"
        result["low_duration_sec"] = 0
        return result

    low_since_f = float(low_since)
    low_duration = now - low_since_f
    result["low_duration_sec"] = round(low_duration, 1)

    if low_duration < settings.upload_watchdog_sustain_sec:
        state["last_speed_bps"] = speed_bps
        _save_state(state_path, state)
        result["action"] = "slow_continuing"
        return result

    reason = (
        f"Upload below {settings.upload_watchdog_min_mbps:g} MB/s for "
        f"{int(low_duration)}s ({format_speed(speed_bps)})"
    )
    result["action"] = "restart"
    result["restart_required"] = True
    result["reason"] = reason

    if dry_run:
        result["dry_run"] = True
        return result

    if job_id:
        removed = prepare_job_restart(settings, int(job_id), reason)
        result["job_id"] = job_id
        result["removed_paths"] = removed
        logger.warning(
            "Upload watchdog restarting job %s: %s (removed %s)",
            job_id,
            reason,
            removed,
        )
    else:
        logger.warning("Upload watchdog triggered without current job id: %s", reason)

    state["low_since"] = None
    state["last_restart_at"] = now
    state["last_speed_bps"] = speed_bps
    _save_state(state_path, state)
    return result


def configure_watchdog_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single check (default when not looping)",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Poll continuously (for running inside the container)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would happen without resetting the job",
    )


def run_watchdog_args(args: argparse.Namespace, settings: Settings) -> int:
    if args.loop:
        poll = settings.upload_watchdog_poll_sec
        while True:
            result = run_watchdog_once(settings, dry_run=args.dry_run)
            if args.json:
                print(json.dumps(result))
            elif result.get("action") not in ("none", "ok", "idle", "cooldown"):
                print(json.dumps(result, indent=2))
            if result.get("restart_required") and not args.dry_run:
                return EXIT_RESTART
            time.sleep(poll)

    result = run_watchdog_once(settings, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        action = result.get("action")
        if action == "disabled":
            print("Upload watchdog disabled (set UPLOAD_WATCHDOG_ENABLED=true)")
        elif action == "cooldown":
            print(
                f"Upload watchdog in cooldown "
                f"({result.get('cooldown_remaining_sec')}s remaining)"
            )
        elif action == "ok":
            print(f"Upload OK: {result.get('speed')} (min {settings.upload_watchdog_min_mbps:g} MB/s)")
        elif action == "idle":
            print(f"Not uploading (phase={result.get('phase')})")
        elif action in ("slow_started", "slow_continuing"):
            print(
                f"Upload slow: {result.get('speed')} for "
                f"{result.get('low_duration_sec', 0):.0f}s "
                f"(threshold {settings.upload_watchdog_sustain_sec}s)"
            )
        elif action == "restart":
            print(f"Upload watchdog triggered: {result.get('reason')}")
            if result.get("removed_paths"):
                print(f"  Cleaned: {result['removed_paths']}")
            print("  Host should restart the orchestrator container.")
        elif action == "status_unreachable":
            print("Upload watchdog: could not reach /status", file=sys.stderr)
            return EXIT_ERROR
        else:
            print(json.dumps(result, indent=2))

    if result.get("restart_required") and not args.dry_run:
        return EXIT_RESTART
    if result.get("action") == "status_unreachable":
        return EXIT_ERROR
    return EXIT_OK
