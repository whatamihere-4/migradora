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


def _unwrap_data(result: Any) -> Any:
    """JD2 Deprecated API wraps payloads as {\"data\": ...}."""
    if isinstance(result, dict) and "data" in result:
        return result["data"]
    return result


def _as_list(result: Any) -> list[dict[str, Any]]:
    data = _unwrap_data(result)
    return data if isinstance(data, list) else []


def _as_dict(result: Any) -> dict[str, Any]:
    data = _unwrap_data(result)
    return data if isinstance(data, dict) else {}


def _link_failed(link: dict[str, Any]) -> str | None:
    status = str(link.get("status") or "").lower()
    if not status:
        return None
    markers = ("invalid", "error", "failed", "offline", "blocked", "unavailable")
    if any(marker in status for marker in markers):
        return str(link.get("status"))
    return None


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

    def _post_action(self, path: str, *params: Any) -> Any:
        """POST multi-parameter JD2 actions (body is a JSON array of arguments)."""
        resp = self._client.post(path, json=list(params))
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

    def wait_until_healthy(
        self,
        timeout_sec: float = 180.0,
        interval_sec: float = 5.0,
    ) -> None:
        """Block until JD2 Deprecated API responds (startup can take 1–3 min)."""
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if self.health():
                return
            time.sleep(interval_sec)
        raise TimeoutError(
            f"JDownloader API at {self.base_url} not ready after {timeout_sec:.0f}s"
        )

    def is_collecting(self) -> bool:
        result = self._get("/linkgrabberv2/isCollecting")
        return bool(_unwrap_data(result))

    def package_count(self) -> int:
        result = self._get("/downloadsV2/packageCount")
        data = _unwrap_data(result)
        if isinstance(data, int):
            return data
        return int(data or 0)

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
        job = _as_dict(result)
        return _as_int(job.get("id"))

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
        packages = _as_list(result)
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
        return _as_list(result)

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
        return _as_list(result)

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
        packages = _as_list(result)
        if package_name:
            packages = [p for p in packages if p.get("name") == package_name]
        return packages

    def remove_linkgrabber(
        self,
        link_ids: list[int] | None = None,
        package_ids: list[int] | None = None,
    ) -> None:
        links = link_ids or []
        packages = package_ids or []
        if not links and not packages:
            return
        # JD2 expects [linkIds, packageIds]; use [0] when removing by package only.
        self._post_action(
            "/linkgrabberv2/removeLinks",
            links if links else [0],
            packages,
        )

    def remove_downloads(
        self,
        link_ids: list[int] | None = None,
        package_ids: list[int] | None = None,
    ) -> None:
        links = link_ids or []
        packages = package_ids or []
        if not links and not packages:
            return
        self._post_action(
            "/downloadsV2/removeLinks",
            links if links else [0],
            packages,
        )

    def wait_until_package_finished(
        self,
        package_name: str,
        timeout_sec: int = 86400,
    ) -> list[dict[str, Any]]:
        deadline = time.time() + timeout_sec
        stalled = 0
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
            if links:
                failed = [reason for link in links if (reason := _link_failed(link))]
                if failed:
                    raise RuntimeError(
                        f"JD2 download failed for package {package_name}: {failed[0]}"
                    )
            if links and all(link.get("finished") for link in links):
                logger.info("JD2 package finished: %s (%d links)", package_name, len(links))
                return links
            running = [link for link in links if not link.get("finished")]
            if running:
                loaded = sum(int(link.get("bytesLoaded") or 0) for link in links)
                total = sum(int(link.get("bytesTotal") or 0) for link in links) or 1
                if loaded == 0:
                    stalled += 1
                    if stalled >= 12:
                        statuses = [link.get("status") for link in links]
                        raise RuntimeError(
                            f"JD2 download stalled at 0% for {package_name}: {statuses}"
                        )
                else:
                    stalled = 0
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
        *,
        known_link_ids: set[int] | None = None,
    ) -> list[dict[str, Any]]:
        if job_id is not None:
            links = self.query_linkgrabber_links(job_uuids=[job_id])
            if links:
                return links

        packages = self.query_linkgrabber_packages(package_name=package_name)
        if packages:
            pkg_uuid = _as_int(packages[0].get("uuid"))
            if pkg_uuid is not None:
                links = self.query_linkgrabber_links(package_uuids=[pkg_uuid])
                if links:
                    return links

        all_links = self.query_linkgrabber_links()
        if known_link_ids:
            return [
                link
                for link in all_links
                if _as_int(link.get("uuid")) not in known_link_ids
            ]
        return all_links

    def wait_for_linkgrabber_crawl(
        self,
        package_name: str,
        job_id: int | None = None,
        timeout_sec: int = 600,
        stable_polls: int = 3,
        *,
        known_link_ids: set[int] | None = None,
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

            links = self._links_for_crawl(
                package_name,
                job_id,
                known_link_ids=known_link_ids,
            )
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
