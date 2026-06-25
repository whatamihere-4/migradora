"""CLI entry point for migradora orchestrator."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from migradora.config import Settings
from migradora.discovery.api_discovery import discover_and_enqueue
from migradora.filester_probe import configure_probe_parser, run_probe_args
from migradora.job_cleanup import clear_all_downloads
from migradora.logger import setup_logging
from migradora.models import QueueState
from migradora.orchestrator import run_orchestrator
from migradora.queue.manager import QueueManager


def cmd_discover(settings: Settings, force: bool) -> int:
    result = discover_and_enqueue(settings, force=force)
    print(json.dumps(result, indent=2))
    return 0


def cmd_status(settings: Settings) -> int:
    queue = QueueManager(settings.db_path)
    stats = queue.get_stats()
    state, reason = queue.get_queue_state()
    print(json.dumps({
        "queue_state": state.value,
        "pause_reason": reason,
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
    }, indent=2))
    return 0


def cmd_resume(settings: Settings) -> int:
    queue = QueueManager(settings.db_path)
    queue.set_queue_state(QueueState.RUNNING, "")
    print("Queue resumed")
    return 0


def cmd_retry_failed(settings: Settings) -> int:
    queue = QueueManager(settings.db_path)
    count = queue.reset_failed_jobs()
    active = queue.reset_active_jobs()
    queue.set_queue_state(QueueState.RUNNING, "")
    print(json.dumps({"reset": count, "reset_active": active, "queue_state": "running"}, indent=2))
    return 0


def cmd_run(settings: Settings) -> int:
    run_orchestrator(settings)
    return 0


def cmd_reset(settings: Settings, *, yes: bool, discover_after: bool) -> int:
    if not yes:
        print(
            "This deletes all queue jobs, Filester folder mappings, and local job downloads.\n"
            "Re-run with: python -m migradora reset --yes",
            file=sys.stderr,
        )
        return 1

    queue = QueueManager(settings.db_path)
    counts = queue.reset_queue()
    removed = clear_all_downloads(settings.download_dir)

    heartbeat = Path(settings.state_dir) / "pipeline.heartbeat"
    if heartbeat.exists():
        heartbeat.unlink(missing_ok=True)

    result: dict[str, object] = {
        **counts,
        "download_dirs_removed": len(removed),
        "queue_state": "running",
    }

    if discover_after:
        result["discover"] = discover_and_enqueue(settings, force=False)

    print(json.dumps(result, indent=2))
    print(
        "\nQueue reset. Run `python -m migradora discover` if you did not pass --discover.",
        file=sys.stderr,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Migradora: Gofile → Filester mirror")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("discover", help="Crawl GOFILE_FOLDER_URLS via API and enqueue files")
    sub.add_parser("status", help="Show queue status")
    sub.add_parser("resume", help="Resume paused queue")
    sub.add_parser("retry-failed", help="Reset failed jobs to pending and resume queue")
    reset_p = sub.add_parser(
        "reset",
        help="Wipe queue database, folder mappings, and local job downloads",
    )
    reset_p.add_argument(
        "--yes",
        action="store_true",
        help="Confirm destructive reset",
    )
    reset_p.add_argument(
        "--discover",
        action="store_true",
        help="Run discover immediately after reset",
    )
    sub.add_parser("run", help="Run orchestrator with dashboard")
    probe = sub.add_parser("filester-probe", help="Probe Filester folder API")
    configure_probe_parser(probe)

    parser.add_argument("--force", action="store_true", help="Re-enqueue uploaded files (discover)")
    args = parser.parse_args()

    settings = Settings.load()
    settings.ensure_dirs()
    setup_logging("orchestrator", settings.log_dir, settings.log_level)

    if args.command == "filester-probe":
        return run_probe_args(args)

    if args.command == "reset":
        return cmd_reset(settings, yes=args.yes, discover_after=args.discover)

    commands = {
        "discover": lambda: cmd_discover(settings, args.force),
        "status": lambda: cmd_status(settings),
        "resume": lambda: cmd_resume(settings),
        "retry-failed": lambda: cmd_retry_failed(settings),
        "run": lambda: cmd_run(settings),
    }
    return commands[args.command]()


if __name__ == "__main__":
    sys.exit(main())
