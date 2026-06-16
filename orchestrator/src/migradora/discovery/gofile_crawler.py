"""Recursive Gofile folder discovery and enqueue."""

from __future__ import annotations

import logging
import time

from migradora.config import Settings
from migradora.gofile_client import GoFileClient
from migradora.queue.manager import QueueManager

logger = logging.getLogger("migradora.discovery")


def discover_and_enqueue(settings: Settings, force: bool = False) -> dict[str, int]:
    client = GoFileClient(token=settings.gofile_token or None)
    queue = QueueManager(settings.db_path)
    stats = {"discovered": 0, "enqueued": 0, "skipped": 0}

    for url in settings.gofile_folder_urls:
        content_id = GoFileClient.extract_content_id(url)
        logger.info("Discovering folder: %s (%s)", url, content_id)
        for item in client.iter_folder(
            content_id,
            password=settings.gofile_password or None,
            delay_sec=settings.discovery_delay_sec,
        ):
            if item.item_type != "file":
                continue
            stats["discovered"] += 1
            parent_path = "/".join(item.path.split("/")[:-1])
            job_id = queue.enqueue_file(
                gofile_content_id=item.content_id,
                gofile_path=item.path,
                filename=item.name,
                size_bytes=item.size_bytes,
                download_link=item.download_link,
                parent_folder_path=parent_path,
                force=force,
            )
            if job_id:
                stats["enqueued"] += 1
                logger.info("Enqueued: %s (%d bytes)", item.path, item.size_bytes)
            else:
                stats["skipped"] += 1
        time.sleep(settings.discovery_delay_sec)

    logger.info(
        "Discovery complete: %d files found, %d enqueued, %d skipped",
        stats["discovered"],
        stats["enqueued"],
        stats["skipped"],
    )
    return stats
