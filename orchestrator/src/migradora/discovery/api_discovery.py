"""Discover Gofile files via Premium API folder crawl."""

from __future__ import annotations

import logging
import time

from migradora.config import Settings
from migradora.gofile_client import GofileClient
from migradora.queue.manager import QueueManager

logger = logging.getLogger("migradora.discovery")


def discover_and_enqueue(settings: Settings, force: bool = False) -> dict[str, int]:
    if not settings.gofile_folder_urls:
        raise RuntimeError("Set GOFILE_FOLDER_URLS in .env (comma-separated folder links)")
    if not settings.gofile_token:
        raise RuntimeError("Set GOFILE_TOKEN in .env (premium account)")

    queue = QueueManager(settings.db_path)
    stats = {"discovered": 0, "enqueued": 0, "skipped": 0}

    with GofileClient(
        token=settings.gofile_token,
        password=settings.gofile_password,
    ) as gofile:
        for folder_url in settings.gofile_folder_urls:
            logger.info("Discovering folder: %s", folder_url)
            for gf in gofile.iter_files(folder_url):
                stats["discovered"] += 1
                job_id = queue.enqueue_file(
                    gofile_content_id=gf.file_id,
                    gofile_path=gf.path,
                    filename=gf.name,
                    size_bytes=gf.size_bytes,
                    gofile_url=gf.page_url,
                    parent_folder_path=gf.parent_folder_path,
                    force=force,
                )
                if job_id:
                    stats["enqueued"] += 1
                    logger.info("Enqueued: %s (%d bytes)", gf.path, gf.size_bytes)
                else:
                    stats["skipped"] += 1
            time.sleep(settings.discovery_delay_sec)

    logger.info(
        "Discovery complete: %d found, %d enqueued, %d skipped",
        stats["discovered"],
        stats["enqueued"],
        stats["skipped"],
    )
    return stats
