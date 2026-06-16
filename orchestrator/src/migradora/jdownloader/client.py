"""JDownloader2 local Deprecated API client (port 3128, no MyJDownloader cloud)."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger("migradora.jdownloader")


class JDownloaderClient:
    def __init__(
        self,
        host: str = "jdownloader",
        port: int = 3128,
        timeout_sec: float = 30.0,
        poll_interval_sec: float = 5.0,
    ) -> None:
        self.base_url = f"http://{host}:{port}"
        self.poll_interval_sec = poll_interval_sec
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout_sec, connect=10.0),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> JDownloaderClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        resp = self._client.post(path, json=body or {})
        resp.raise_for_status()
        if not resp.content:
            return None
        return resp.json()

    def _get(self, path: str) -> Any:
        resp = self._client.get(path)
        resp.raise_for_status()
        if not resp.content:
            return None
        return resp.json()

    def health(self) -> bool:
        try:
            resp = self._client.get("/help")
            return resp.status_code == 200
        except Exception as exc:
            logger.debug("JD2 health check failed: %s", exc)
            return False

    def package_count(self) -> int:
        result = self._get("/downloadsV2/packageCount")
        if isinstance(result, int):
            return result
        return int(result or 0)

    def add_links(
        self,
        url: str,
        package_name: str,
        destination_folder: str,
        *,
        autostart: bool = True,
        download_password: str = "",
    ) -> Any:
        body = {
            "links": url,
            "packageName": package_name,
            "destinationFolder": destination_folder,
            "autostart": autostart,
            "autoConfirm": True,
            "overwritePackagizerRules": True,
        }
        if download_password:
            body["downloadPassword"] = download_password
        logger.info("JD2 addLinks: %s -> %s", package_name, url[:80])
        return self._post("/linkgrabberv2/addLinks", body)

    def query_download_packages(self, **query: Any) -> list[dict[str, Any]]:
        result = self._post("/downloadsV2/queryPackages", query or {})
        if isinstance(result, list):
            return result
        return []

    def query_download_links(self, **query: Any) -> list[dict[str, Any]]:
        result = self._post("/downloadsV2/queryLinks", query or {})
        if isinstance(result, list):
            return result
        return []

    def query_linkgrabber_links(self, **query: Any) -> list[dict[str, Any]]:
        result = self._post("/linkgrabberv2/queryLinks", query or {})
        if isinstance(result, list):
            return result
        return []

    def query_linkgrabber_packages(self, **query: Any) -> list[dict[str, Any]]:
        result = self._post("/linkgrabberv2/queryPackages", query or {})
        if isinstance(result, list):
            return result
        return []

    def remove_linkgrabber(self, link_ids: list[int] | None = None, package_ids: list[int] | None = None) -> None:
        body: dict[str, Any] = {
            "linkIds": link_ids or [],
            "packageIds": package_ids or [],
        }
        self._post("/linkgrabberv2/removeLinks", body)

    def remove_downloads(self, link_ids: list[int] | None = None, package_ids: list[int] | None = None) -> None:
        body: dict[str, Any] = {
            "linkIds": link_ids or [],
            "packageIds": package_ids or [],
        }
        self._post("/downloadsV2/removeLinks", body)

    def wait_until_package_finished(
        self,
        package_name: str,
        timeout_sec: int = 86400,
    ) -> list[dict[str, Any]]:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            packages = self.query_download_packages(packageName=package_name)
            if not packages:
                time.sleep(self.poll_interval_sec)
                continue
            pkg = packages[0]
            pkg_uuid = pkg.get("uuid")
            links = self.query_download_links(packageUUID=pkg_uuid) if pkg_uuid else []
            if links and all(link.get("finished") for link in links):
                failed = [l for l in links if l.get("status") and "failed" in str(l.get("status")).lower()]
                if failed:
                    raise RuntimeError(f"JD2 download failed for package {package_name}: {failed}")
                logger.info("JD2 package finished: %s (%d links)", package_name, len(links))
                return links
            running = [l for l in links if not l.get("finished")]
            if running:
                pct = sum(l.get("bytesLoaded", 0) for l in links)
                total = sum(l.get("bytesTotal", 0) for l in links) or 1
                logger.info(
                    "JD2 downloading %s: %.1f%%",
                    package_name,
                    (pct / total) * 100,
                )
            time.sleep(self.poll_interval_sec)
        raise TimeoutError(f"JD2 package {package_name} did not finish within {timeout_sec}s")

    def wait_for_linkgrabber_crawl(
        self,
        package_name: str,
        timeout_sec: int = 600,
        stable_polls: int = 3,
    ) -> list[dict[str, Any]]:
        """Poll linkgrabber until link count stabilizes (folder expanded)."""
        deadline = time.time() + timeout_sec
        last_count = -1
        stable = 0
        while time.time() < deadline:
            packages = self.query_linkgrabber_packages(name=package_name)
            if not packages:
                time.sleep(self.poll_interval_sec)
                continue
            pkg = packages[0]
            pkg_id = pkg.get("uuid") or pkg.get("id")
            links = self.query_linkgrabber_links(packageUUID=pkg_id) if pkg_id else []
            count = len(links)
            if count > 0 and count == last_count:
                stable += 1
                if stable >= stable_polls:
                    logger.info("JD2 crawl complete: %d links in %s", count, package_name)
                    return links
            else:
                stable = 0
            last_count = count
            logger.info("JD2 crawling %s: %d links so far", package_name, count)
            time.sleep(self.poll_interval_sec)
        raise TimeoutError(f"Linkgrabber crawl for {package_name} timed out")
