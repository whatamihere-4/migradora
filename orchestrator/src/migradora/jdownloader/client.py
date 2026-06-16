"""JDownloader2 local Deprecated API client (port 3128, no MyJDownloader cloud)."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger("migradora.jdownloader")

_LINKGRABBER_LINK_FIELDS: dict[str, Any] = {
    "url": True,
    "bytesTotal": True,
    "host": True,
    "status": True,
    "availability": True,
    "enabled": True,
}

_LINKGRABBER_PACKAGE_FIELDS: dict[str, Any] = {
    "childCount": True,
    "bytesTotal": True,
    "enabled": True,
    "hosts": True,
    "saveTo": True,
    "status": True,
}

_DOWNLOAD_LINK_FIELDS: dict[str, Any] = {
    "url": True,
    "bytesTotal": True,
    "bytesLoaded": True,
    "finished": True,
    "status": True,
    "name": True,
    "host": True,
    "running": True,
}

_DOWNLOAD_PACKAGE_FIELDS: dict[str, Any] = {
    "childCount": True,
    "bytesTotal": True,
    "bytesLoaded": True,
    "finished": True,
    "status": True,
    "running": True,
    "saveTo": True,
}


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_int_list(values: list[Any] | None) -> list[int]:
    if not values:
        return []
    out: list[int] = []
    for value in values:
        parsed = _as_int(value)
        if parsed is not None:
            out.append(parsed)
    return out


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

    def is_collecting(self) -> bool:
        result = self._get("/linkgrabberv2/isCollecting")
        return bool(result)

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
        deep_decrypt: bool = True,
    ) -> int | None:
        """Add links to linkgrabber. Returns crawl job id when available."""
        body: dict[str, Any] = {
            "links": url,
            "packageName": package_name,
            "destinationFolder": destination_folder,
            "autostart": autostart,
            "autoConfirm": True,
            "assignJobID": True,
            "overwritePackagizerRules": True,
            "deepDecrypt": deep_decrypt,
        }
        if download_password:
            body["downloadPassword"] = download_password
        logger.info("JD2 addLinks: %s -> %s", package_name, url[:80])
        result = self._post("/linkgrabberv2/addLinks", body)
        if isinstance(result, dict):
            return _as_int(result.get("id"))
        return None

    def query_download_packages(
        self,
        *,
        package_name: str | None = None,
        package_uuids: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        body = dict(_DOWNLOAD_PACKAGE_FIELDS)
        if package_uuids:
            body["packageUUIDs"] = package_uuids
        result = self._post("/downloadsV2/queryPackages", body)
        packages = result if isinstance(result, list) else []
        if package_name:
            packages = [p for p in packages if p.get("name") == package_name]
        return packages

    def query_download_links(
        self,
        *,
        package_uuids: list[int] | None = None,
        job_uuids: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        body = dict(_DOWNLOAD_LINK_FIELDS)
        if package_uuids:
            body["packageUUIDs"] = package_uuids
        if job_uuids:
            body["jobUUIDs"] = job_uuids
        result = self._post("/downloadsV2/queryLinks", body)
        return result if isinstance(result, list) else []

    def query_linkgrabber_links(
        self,
        *,
        package_uuids: list[int] | None = None,
        job_uuids: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        body = dict(_LINKGRABBER_LINK_FIELDS)
        if package_uuids:
            body["packageUUIDs"] = package_uuids
        if job_uuids:
            body["jobUUIDs"] = job_uuids
        result = self._post("/linkgrabberv2/queryLinks", body)
        return result if isinstance(result, list) else []

    def query_linkgrabber_packages(
        self,
        *,
        package_name: str | None = None,
        package_uuids: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        body = dict(_LINKGRABBER_PACKAGE_FIELDS)
        if package_uuids:
            body["packageUUIDs"] = package_uuids
        result = self._post("/linkgrabberv2/queryPackages", body)
        packages = result if isinstance(result, list) else []
        if package_name:
            packages = [p for p in packages if p.get("name") == package_name]
        return packages

    def remove_linkgrabber(
        self,
        link_ids: list[int] | None = None,
        package_ids: list[int] | None = None,
    ) -> None:
        body: dict[str, Any] = {
            "linkIds": link_ids or [],
            "packageIds": package_ids or [],
        }
        self._post("/linkgrabberv2/removeLinks", body)

    def remove_downloads(
        self,
        link_ids: list[int] | None = None,
        package_ids: list[int] | None = None,
    ) -> None:
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
            packages = self.query_download_packages(package_name=package_name)
            if not packages:
                time.sleep(self.poll_interval_sec)
                continue
            pkg = packages[0]
            pkg_uuid = _as_int(pkg.get("uuid"))
            links = (
                self.query_download_links(package_uuids=[pkg_uuid])
                if pkg_uuid is not None
                else []
            )
            if links and all(link.get("finished") for link in links):
                failed = [
                    link
                    for link in links
                    if link.get("status") and "failed" in str(link.get("status")).lower()
                ]
                if failed:
                    raise RuntimeError(
                        f"JD2 download failed for package {package_name}: {failed}"
                    )
                logger.info("JD2 package finished: %s (%d links)", package_name, len(links))
                return links
            running = [link for link in links if not link.get("finished")]
            if running:
                loaded = sum(int(link.get("bytesLoaded") or 0) for link in links)
                total = sum(int(link.get("bytesTotal") or 0) for link in links) or 1
                logger.info(
                    "JD2 downloading %s: %.1f%%",
                    package_name,
                    (loaded / total) * 100,
                )
            time.sleep(self.poll_interval_sec)
        raise TimeoutError(f"JD2 package {package_name} did not finish within {timeout_sec}s")

    def _links_for_crawl(
        self,
        package_name: str,
        job_id: int | None,
    ) -> list[dict[str, Any]]:
        if job_id is not None:
            links = self.query_linkgrabber_links(job_uuids=[job_id])
            if links:
                return links
        packages = self.query_linkgrabber_packages(package_name=package_name)
        if not packages:
            return []
        pkg_uuid = _as_int(packages[0].get("uuid"))
        if pkg_uuid is None:
            return []
        return self.query_linkgrabber_links(package_uuids=[pkg_uuid])

    def wait_for_linkgrabber_crawl(
        self,
        package_name: str,
        job_id: int | None = None,
        timeout_sec: int = 600,
        stable_polls: int = 3,
    ) -> list[dict[str, Any]]:
        """Poll linkgrabber until crawl finishes and link count stabilizes."""
        deadline = time.time() + timeout_sec
        last_count = -1
        stable = 0
        saw_collecting = False

        while time.time() < deadline:
            collecting = self.is_collecting()
            if collecting:
                saw_collecting = True

            links = self._links_for_crawl(package_name, job_id)
            count = len(links)
            crawl_done = not collecting and (saw_collecting or count > 0)

            if crawl_done and count > 0 and count == last_count:
                stable += 1
                if stable >= stable_polls:
                    logger.info("JD2 crawl complete: %d links in %s", count, package_name)
                    return links
            else:
                stable = 0

            if crawl_done and count == 0 and saw_collecting:
                raise RuntimeError(
                    f"JD2 crawl for {package_name} finished with 0 links "
                    "(check Gofile URL/password in JD2 web UI)"
                )

            last_count = count
            logger.info(
                "JD2 crawling %s: %d links, collecting=%s",
                package_name,
                count,
                collecting,
            )
            time.sleep(self.poll_interval_sec)

        raise TimeoutError(f"Linkgrabber crawl for {package_name} timed out")
