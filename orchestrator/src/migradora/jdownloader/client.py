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


def gofile_file_id(url: str) -> str | None:
    if "#file=" not in url:
        return None
    return url.split("#file=", 1)[1].split("&")[0].lower()


def filter_links_for_url(
    url: str,
    links: list[dict[str, Any]],
    *,
    expected_size: int | None = None,
) -> list[dict[str, Any]]:
    """Keep only the link(s) matching a file-specific Gofile URL."""
    if not links:
        return links
    file_id = gofile_file_id(url)
    if file_id:
        matched = [
            link
            for link in links
            if file_id in (link.get("url") or "").lower()
        ]
        if matched:
            return matched
    folder_base = url.split("#")[0].rstrip("/").lower()
    without_folder = [
        link
        for link in links
        if (link.get("url") or "").split("#")[0].rstrip("/").lower() != folder_base
        or "#file=" in (link.get("url") or "")
    ]
    if without_folder:
        links = without_folder
    if expected_size and len(links) > 1:
        exact = [
            link
            for link in links
            if int(link.get("bytesTotal") or 0) == expected_size
        ]
        if exact:
            return exact
    if len(links) > 1:
        if expected_size:
            links = sorted(
                links,
                key=lambda link: abs(
                    int(link.get("bytesTotal") or 0) - expected_size
                ),
            )
        else:
            links = sorted(
                links,
                key=lambda link: int(link.get("bytesTotal") or 0),
                reverse=True,
            )
        return [links[0]]
    return links


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

    def _post_action_best_effort(self, path: str, *params: Any) -> bool:
        try:
            self._post_action(path, *params)
            return True
        except httpx.HTTPStatusError as exc:
            logger.warning("JD2 %s failed (%s): %s", path, exc.response.status_code, params)
            return False
        except Exception as exc:
            logger.warning("JD2 %s failed: %s", path, exc)
            return False

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
        if links:
            self._post_action("/downloadsV2/removeLinks", links, packages)
            return
        for link_arg in ([0], []):
            if self._post_action_best_effort("/downloadsV2/removeLinks", link_arg, packages):
                return

    def clear_package(self, package_name: str) -> None:
        for query_fn, remove_fn in (
            (self.query_download_packages, self.remove_downloads),
            (self.query_linkgrabber_packages, self.remove_linkgrabber),
        ):
            try:
                packages = query_fn(package_name=package_name)
                pkg_ids = _as_int_list([p.get("uuid") for p in packages])
                if pkg_ids:
                    remove_fn(package_ids=pkg_ids)
            except Exception as exc:
                logger.warning("JD2 clear %s failed: %s", package_name, exc)

    def clear_gofile_url(self, url: str) -> None:
        """Remove duplicate Gofile links for this file from both JD2 lists."""
        file_id = gofile_file_id(url)
        folder_base = url.split("#")[0].rstrip("/").lower()
        for query_fn, remove_fn in (
            (self.query_download_links, self.remove_downloads),
            (self.query_linkgrabber_links, self.remove_linkgrabber),
        ):
            try:
                drop_ids: list[int] = []
                for link in query_fn():
                    link_url = (link.get("url") or "").lower()
                    if file_id and file_id in link_url:
                        uid = _as_int(link.get("uuid"))
                        if uid is not None:
                            drop_ids.append(uid)
                    elif not file_id and link_url.split("#")[0].rstrip("/") == folder_base:
                        uid = _as_int(link.get("uuid"))
                        if uid is not None:
                            drop_ids.append(uid)
                if drop_ids:
                    remove_fn(link_ids=drop_ids)
                    logger.info("JD2 cleared %d duplicate link(s) for %s", len(drop_ids), url[:60])
            except Exception as exc:
                logger.warning("JD2 clear_gofile_url failed: %s", exc)

    def _prune_download_links(
        self,
        keep_links: list[dict[str, Any]],
        all_links: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        keep_ids = {
            uid
            for link in keep_links
            if (uid := _as_int(link.get("uuid"))) is not None
        }
        drop_ids = [
            uid
            for link in all_links
            if (uid := _as_int(link.get("uuid"))) is not None and uid not in keep_ids
        ]
        if drop_ids:
            try:
                self.remove_downloads(link_ids=drop_ids)
                logger.info("JD2 removed %d extra download link(s)", len(drop_ids))
            except Exception as exc:
                logger.warning("JD2 prune downloads failed: %s", exc)
        return [
            link
            for link in all_links
            if _as_int(link.get("uuid")) in keep_ids
        ]

    def _resume_existing_package(
        self,
        package_name: str,
        url: str,
        *,
        expected_size: int | None = None,
    ) -> bool:
        """If package is already in downloads with the right link, resume without re-adding."""
        packages = self.query_download_packages(package_name=package_name)
        if not packages:
            return False
        pkg_uuid = _as_int(packages[0].get("uuid"))
        if pkg_uuid is None:
            return False
        links = self.query_download_links(package_uuids=[pkg_uuid])
        if not links:
            return False
        wanted = filter_links_for_url(url, links, expected_size=expected_size)
        if not wanted:
            return False
        kept = self._prune_download_links(wanted, links)
        if not kept:
            return False
        logger.info(
            "JD2 resuming %s (%d link(s), no re-add)",
            package_name,
            len(kept),
        )
        self.ensure_downloads_running()
        link_ids = _as_int_list([link.get("uuid") for link in kept])
        if link_ids:
            self.force_download(link_ids=link_ids)
        return True

    def get_download_state(self) -> str:
        result = self._get("/downloadcontroller/getCurrentState")
        return str(_unwrap_data(result) or "")

    def get_download_speed_bps(self) -> int:
        result = self._get("/downloadcontroller/getSpeedInBps")
        data = _unwrap_data(result)
        try:
            return int(data or 0)
        except (TypeError, ValueError):
            return 0

    def start_downloads(self) -> None:
        for body in ({}, []):
            try:
                resp = self._client.post("/downloadcontroller/start", json=body)
                resp.raise_for_status()
                return
            except Exception as exc:
                logger.debug("downloadcontroller/start %r failed: %s", body, exc)

    def set_downloads_paused(self, paused: bool) -> None:
        if not self._post_action_best_effort("/downloadcontroller/pause", paused):
            try:
                self._client.post(
                    "/downloadcontroller/pause",
                    params={"value": str(paused).lower()},
                )
            except Exception as exc:
                logger.debug("downloadcontroller/pause fallback failed: %s", exc)

    def move_to_downloadlist(
        self,
        link_ids: list[int] | None = None,
        package_ids: list[int] | None = None,
    ) -> None:
        links = link_ids or []
        packages = package_ids or []
        if not links and not packages:
            return
        if links and self._post_action_best_effort(
            "/linkgrabberv2/moveToDownloadlist", links, []
        ):
            return
        if packages and self._post_action_best_effort(
            "/linkgrabberv2/moveToDownloadlist", [], packages
        ):
            return
        if links and packages and self._post_action_best_effort(
            "/linkgrabberv2/moveToDownloadlist", links, packages
        ):
            return
        raise RuntimeError(
            f"JD2 moveToDownloadlist failed for links={links} packages={packages}"
        )

    def force_download(
        self,
        link_ids: list[int] | None = None,
        package_ids: list[int] | None = None,
    ) -> None:
        links = link_ids or []
        packages = package_ids or []
        if not links and not packages:
            return
        if not self._post_action_best_effort("/downloadsV2/forceDownload", links, packages):
            self._post_action_best_effort("/downloadcontroller/forceDownload", links, packages)

    def ensure_downloads_running(self) -> None:
        state = self.get_download_state().upper()
        logger.info("JD2 download controller: %s", state or "unknown")
        if state == "RUNNING":
            return

        attempts: list[tuple[str, Any]] = [
            ("unpause", lambda: self.set_downloads_paused(False)),
            ("start", self.start_downloads),
            ("toolbar", lambda: self._client.post("/toolbar/startDownloads", json=[])),
        ]
        for name, fn in attempts:
            try:
                fn()
                time.sleep(1)
                state = self.get_download_state().upper()
                logger.info("JD2 after %s: %s", name, state)
                if state == "RUNNING":
                    return
            except Exception as exc:
                logger.debug("JD2 %s failed: %s", name, exc)

        logger.warning(
            "JD2 download controller still not RUNNING (state=%s) — press Play in web UI if needed",
            self.get_download_state(),
        )

    def _prune_linkgrabber_links(
        self,
        keep_links: list[dict[str, Any]],
        all_links: list[dict[str, Any]],
    ) -> None:
        keep_ids = {
            uid
            for link in keep_links
            if (uid := _as_int(link.get("uuid"))) is not None
        }
        drop_ids = [
            uid
            for link in all_links
            if (uid := _as_int(link.get("uuid"))) is not None and uid not in keep_ids
        ]
        if drop_ids:
            try:
                self.remove_linkgrabber(link_ids=drop_ids)
                logger.info("JD2 removed %d extra linkgrabber link(s)", len(drop_ids))
            except Exception as exc:
                logger.warning("JD2 prune linkgrabber failed: %s", exc)

    def _wait_for_download_package(
        self,
        package_name: str,
        wanted_links: list[dict[str, Any]],
        timeout_sec: float = 120.0,
    ) -> list[dict[str, Any]]:
        deadline = time.time() + timeout_sec
        link_ids = _as_int_list([link.get("uuid") for link in wanted_links])
        while time.time() < deadline:
            packages = self.query_download_packages(package_name=package_name)
            if packages:
                pkg_uuid = _as_int(packages[0].get("uuid"))
                if pkg_uuid is not None:
                    dl_links = self.query_download_links(package_uuids=[pkg_uuid])
                    kept = self._prune_download_links(wanted_links, dl_links)
                    if kept:
                        return packages
                return packages
            if link_ids:
                try:
                    self.move_to_downloadlist(link_ids=link_ids)
                except Exception as exc:
                    logger.debug("JD2 promote %s by link: %s", package_name, exc)
            else:
                lg_packages = self.query_linkgrabber_packages(package_name=package_name)
                pkg_ids = _as_int_list([p.get("uuid") for p in lg_packages])
                if pkg_ids:
                    try:
                        self.move_to_downloadlist(package_ids=pkg_ids)
                    except Exception as exc:
                        logger.debug("JD2 promote %s by package: %s", package_name, exc)
            time.sleep(self.poll_interval_sec)
        return []

    def add_and_start_package(
        self,
        url: str,
        package_name: str,
        destination_folder: str,
        *,
        download_password: str = "",
        crawl_timeout_sec: int = 600,
        expected_size: int | None = None,
    ) -> None:
        """Crawl one file in linkgrabber, move only that link to downloads, start."""
        if self._resume_existing_package(
            package_name, url, expected_size=expected_size
        ):
            return

        self.clear_package(package_name)
        self.clear_gofile_url(url)

        crawl_job_id = self.add_links(
            url,
            package_name=package_name,
            destination_folder=destination_folder,
            autostart=False,
            download_password=download_password,
        )

        links = self.wait_for_linkgrabber_crawl(
            package_name,
            job_id=crawl_job_id,
            timeout_sec=crawl_timeout_sec,
        )
        wanted = filter_links_for_url(url, links, expected_size=expected_size)
        if not wanted:
            raise RuntimeError(
                f"JD2 crawl for {package_name} matched 0 links for {url[:80]}"
            )
        logger.info(
            "JD2 linkgrabber ready: %s (%d crawled, %d wanted)",
            package_name,
            len(links),
            len(wanted),
        )
        if len(wanted) < len(links):
            self._prune_linkgrabber_links(wanted, links)

        packages = self._wait_for_download_package(package_name, wanted)
        if not packages:
            raise RuntimeError(
                f"JD2 package {package_name} never reached the download list"
            )

        self.ensure_downloads_running()
        link_ids = _as_int_list([link.get("uuid") for link in wanted])
        if link_ids:
            self.force_download(link_ids=link_ids)
        else:
            pkg_ids = _as_int_list([p.get("uuid") for p in packages])
            if pkg_ids:
                self.force_download(package_ids=pkg_ids)

    def wait_until_package_finished(
        self,
        package_name: str,
        timeout_sec: int = 86400,
        *,
        url: str | None = None,
        expected_size: int | None = None,
    ) -> list[dict[str, Any]]:
        self.ensure_downloads_running()
        deadline = time.time() + timeout_sec
        stalled = 0
        missing_pkg_polls = 0
        while time.time() < deadline:
            packages = self.query_download_packages(package_name=package_name)
            if not packages:
                lg_packages = self.query_linkgrabber_packages(package_name=package_name)
                if lg_packages:
                    pkg_ids = _as_int_list([p.get("uuid") for p in lg_packages])
                    logger.warning(
                        "%s still in linkgrabber — promoting to download list",
                        package_name,
                    )
                    try:
                        self.move_to_downloadlist(package_ids=pkg_ids)
                    except Exception as exc:
                        logger.warning("JD2 promote failed: %s", exc)
                    self.ensure_downloads_running()
                    self.force_download(package_ids=pkg_ids)
                missing_pkg_polls += 1
                if missing_pkg_polls >= 12:
                    raise RuntimeError(
                        f"JD2 package {package_name} never appeared in download list"
                    )
                time.sleep(self.poll_interval_sec)
                continue
            missing_pkg_polls = 0
            pkg = packages[0]
            pkg_uuid = _as_int(pkg.get("uuid"))
            links = (
                self.query_download_links(package_uuids=[pkg_uuid])
                if pkg_uuid is not None
                else []
            )
            if url and links:
                wanted = filter_links_for_url(url, links, expected_size=expected_size)
                if wanted and len(wanted) < len(links):
                    links = self._prune_download_links(wanted, links)
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
                    if stalled in (3, 6, 9):
                        self.ensure_downloads_running()
                        if pkg_uuid is not None:
                            self.force_download(package_ids=[pkg_uuid])
                    if stalled >= 12:
                        statuses = [link.get("status") for link in links]
                        speed = self.get_download_speed_bps()
                        raise RuntimeError(
                            f"JD2 download stalled at 0% for {package_name}: "
                            f"statuses={statuses}, speed={speed} B/s, "
                            f"saveTo={pkg.get('saveTo')}"
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
