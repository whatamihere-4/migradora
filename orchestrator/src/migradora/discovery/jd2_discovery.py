"""Discover Gofile files via JDownloader2 linkgrabber crawl."""

from __future__ import annotations

import hashlib
import logging
import time
import uuid

from migradora.config import Settings
from migradora.jdownloader.client import JDownloaderClient
from migradora.queue.manager import QueueManager

logger = logging.getLogger("migradora.discovery")


def _content_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:32]


def discover_and_enqueue(settings: Settings, force: bool = False) -> dict[str, int]:
    queue = QueueManager(settings.db_path)
    stats = {"discovered": 0, "enqueued": 0, "skipped": 0}

    with JDownloaderClient(
        host=settings.jd2_host,
        port=settings.jd2_port,
        timeout_sec=settings.jd2_api_timeout_sec,
        poll_interval_sec=settings.jd2_poll_interval_sec,
    ) as jd2:
        if not jd2.health():
            raise RuntimeError(
                f"JDownloader API not reachable at {settings.jd2_host}:{settings.jd2_port}. "
                "Enable Deprecated API in JD2 settings (port 3128)."
            )

        for folder_url in settings.gofile_folder_urls:
            pkg_name = f"migradora-discover-{uuid.uuid4().hex[:8]}"
            logger.info("Discovering via JD2: %s", folder_url)
            jd2.add_links(
                folder_url,
                package_name=pkg_name,
                destination_folder=settings.jd2_download_dir,
                autostart=False,
                download_password=settings.gofile_password,
            )
            time.sleep(settings.discovery_delay_sec)

            try:
                links = jd2.wait_for_linkgrabber_crawl(
                    pkg_name,
                    timeout_sec=settings.jd2_crawl_timeout_sec,
                )
            except TimeoutError as exc:
                logger.error("Crawl timed out for %s: %s", folder_url, exc)
                continue

            packages = jd2.query_linkgrabber_packages(name=pkg_name)
            pkg_ids = [p.get("uuid") or p.get("id") for p in packages if p.get("uuid") or p.get("id")]
            link_ids = [l.get("uuid") or l.get("id") for l in links if l.get("uuid") or l.get("id")]

            for link in links:
                url = link.get("url") or link.get("variant") or ""
                if not url:
                    continue
                name = link.get("name") or link.get("filename") or "unknown"
                size = int(link.get("bytesTotal") or link.get("size") or 0)
                stats["discovered"] += 1
                parent = folder_url.rstrip("/").split("/")[-1]
                gofile_path = f"{parent}/{name}"
                job_id = queue.enqueue_file(
                    gofile_content_id=_content_id(url),
                    gofile_path=gofile_path,
                    filename=name,
                    size_bytes=size,
                    gofile_url=url,
                    parent_folder_path=parent,
                    force=force,
                )
                if job_id:
                    stats["enqueued"] += 1
                    logger.info("Enqueued: %s (%d bytes)", gofile_path, size)
                else:
                    stats["skipped"] += 1

            try:
                jd2.remove_linkgrabber(
                    link_ids=[i for i in link_ids if i],
                    package_ids=[i for i in pkg_ids if i],
                )
            except Exception as exc:
                logger.warning("Failed to clean linkgrabber after discover: %s", exc)

            time.sleep(settings.discovery_delay_sec)

    logger.info(
        "Discovery complete: %d found, %d enqueued, %d skipped",
        stats["discovered"],
        stats["enqueued"],
        stats["skipped"],
    )
    return stats
