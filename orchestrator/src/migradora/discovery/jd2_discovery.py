"""Discover Gofile files via JDownloader2 linkgrabber crawl."""

from __future__ import annotations

import hashlib
import logging
import time
import uuid

from migradora.config import Settings
from migradora.jdownloader.client import JDownloaderClient, _as_int, _as_int_list
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
            known_link_ids = {
                uid
                for link in jd2.query_linkgrabber_links()
                if (uid := _as_int(link.get("uuid"))) is not None
            }
            crawl_job_id = jd2.add_links(
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
                    job_id=crawl_job_id,
                    timeout_sec=settings.jd2_crawl_timeout_sec,
                    known_link_ids=known_link_ids,
                )
            except TimeoutError as exc:
                logger.error("Crawl timed out for %s: %s", folder_url, exc)
                continue
            except RuntimeError as exc:
                logger.error("Crawl failed for %s: %s", folder_url, exc)
                continue

            folder_norm = folder_url.rstrip("/")
            file_links = [
                link
                for link in links
                if (link.get("url") or "").rstrip("/") != folder_norm
                and (
                    int(link.get("bytesTotal") or 0) > 0
                    or str(link.get("availability") or "").upper() == "ONLINE"
                )
            ]
            if not file_links:
                logger.warning(
                    "JD2 returned %d linkgrabber entries but no downloadable files for %s",
                    len(links),
                    folder_url,
                )
                file_links = [
                    link
                    for link in links
                    if (link.get("url") or "").rstrip("/") != folder_norm
                ]

            packages = jd2.query_linkgrabber_packages(package_name=pkg_name)
            pkg_ids = _as_int_list([p.get("uuid") for p in packages])
            if not pkg_ids:
                pkg_ids = _as_int_list([link.get("packageUUID") for link in links])
            link_ids = _as_int_list([link.get("uuid") for link in links])

            for link in file_links:
                url = link.get("url") or link.get("variant") or ""
                if not url:
                    continue
                if "#file=" not in url:
                    logger.warning("Skipping non-file Gofile URL: %s", url[:80])
                    continue
                name = link.get("name") or link.get("filename") or "unknown"
                size = int(link.get("bytesTotal") or link.get("size") or 0)
                stats["discovered"] += 1
                parent = folder_url.rstrip("/").split("/")[-1]
                gofile_path = f"{parent}/{name}"
                queue_job_id = queue.enqueue_file(
                    gofile_content_id=_content_id(url),
                    gofile_path=gofile_path,
                    filename=name,
                    size_bytes=size,
                    gofile_url=url,
                    parent_folder_path=parent,
                    force=force,
                )
                if queue_job_id:
                    stats["enqueued"] += 1
                    logger.info("Enqueued: %s (%d bytes)", gofile_path, size)
                else:
                    stats["skipped"] += 1

            try:
                if pkg_ids:
                    jd2.remove_linkgrabber(package_ids=pkg_ids)
                elif link_ids:
                    jd2.remove_linkgrabber(link_ids=link_ids)
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
