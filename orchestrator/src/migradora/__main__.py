"""CLI entry point for migradora orchestrator."""

from __future__ import annotations

import argparse
import json
import sys

from migradora.config import Settings
from migradora.filester_probe import run_probe
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Migradora: Gofile → Filester mirror")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("discover", help="Crawl GOFILE_FOLDER_URLS via API and enqueue files")
    sub.add_parser("status", help="Show queue status")
    sub.add_parser("resume", help="Resume paused queue")
    sub.add_parser("retry-failed", help="Reset failed jobs to pending and resume queue")
    sub.add_parser("run", help="Run orchestrator with dashboard")
    probe = sub.add_parser("filester-probe", help="Probe Filester folder API (list/create tests)")
    probe.add_argument("--name", default="migradora-probe-test")
    probe.add_argument("--parent-db-id", type=int, default=None)
    probe.add_argument("--parent-identifier", default=None)
    probe.add_argument("--search", default=None)
    probe.add_argument("--dry-run", action="store_true")

    parser.add_argument("--force", action="store_true", help="Re-enqueue uploaded files (discover)")
    args = parser.parse_args()

    settings = Settings.load()
    settings.ensure_dirs()
    setup_logging("orchestrator", settings.log_dir, settings.log_level)

    if args.command == "filester-probe":
        probe_argv = []
        if args.name != "migradora-probe-test":
            probe_argv.extend(["--name", args.name])
        if args.parent_db_id is not None:
            probe_argv.extend(["--parent-db-id", str(args.parent_db_id)])
        if args.parent_identifier:
            probe_argv.extend(["--parent-identifier", args.parent_identifier])
        if args.search:
            probe_argv.extend(["--search", args.search])
        if args.dry_run:
            probe_argv.append("--dry-run")
        return run_probe(probe_argv)

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
