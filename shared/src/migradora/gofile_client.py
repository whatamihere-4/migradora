"""Gofile Premium API client: folder crawl, link resolve, resumable download."""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import httpx
from pathvalidate import sanitize_filename

logger = logging.getLogger("migradora.gofile")

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_STATIC_WT = "4fd6sg89d7s6"
_API = "https://api.gofile.io"
_PAGE_SIZE = 1000
_CDN_PREFER_VALUES = frozenset({"eu", "na", "auto"})
_PROBE_SAMPLE_BYTES = 2 * 1024 * 1024


def _host_from_gofile_url(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host.endswith(".gofile.io"):
        return host[: -len(".gofile.io")]
    return host


def _server_hosts_from_file_data(data: dict[str, Any]) -> list[str]:
    hosts: list[str] = []
    servers = data.get("servers")
    if isinstance(servers, list):
        hosts.extend(str(s).strip() for s in servers if s)
    selected = data.get("serverSelected")
    if selected:
        hosts.append(str(selected).strip())
    link = data.get("link") or data.get("directLink")
    if link:
        host = _host_from_gofile_url(str(link))
        if host:
            hosts.append(host)
    seen: set[str] = set()
    ordered: list[str] = []
    for host in hosts:
        if host and host not in seen:
            seen.add(host)
            ordered.append(host)
    return ordered


def _region_rank(host: str, prefer: str) -> tuple[int, str]:
    host_l = host.lower()
    if prefer == "auto":
        return (0, host_l)
    if prefer == "eu":
        if "eu" in host_l or host_l.startswith("store-eu"):
            return (0, host_l)
        if "na" in host_l or host_l.startswith("store-na"):
            return (2, host_l)
        return (1, host_l)
    if prefer == "na":
        if "na" in host_l or host_l.startswith("store-na"):
            return (0, host_l)
        if "eu" in host_l or host_l.startswith("store-eu"):
            return (2, host_l)
        return (1, host_l)
    return (1, host_l)


def _order_server_hosts(hosts: list[str], prefer: str) -> list[str]:
    if prefer == "auto" or not hosts:
        return hosts
    return sorted(hosts, key=lambda h: _region_rank(h, prefer))


def _build_download_url(server: str, file_id: str, name: str) -> str:
    return f"https://{server}.gofile.io/download/web/{file_id}/{quote(name)}"


@dataclass(frozen=True)
class GofileFile:
    file_id: str
    folder_id: str
    name: str
    size_bytes: int
    path: str

    @property
    def page_url(self) -> str:
        return f"https://gofile.io/d/{self.folder_id}#file={self.file_id}"

    @property
    def parent_folder_path(self) -> str:
        if "/" not in self.path:
            return ""
        return self.path.rsplit("/", 1)[0]


def parse_folder_url(url: str) -> str:
    """Extract folder/content id from a Gofile folder URL."""
    parsed = urlparse(url.strip())
    if parsed.path.startswith("/d/"):
        return parsed.path.split("/d/", 1)[1].split("/")[0]
    query = parse_qs(parsed.query)
    if "c" in query and query["c"]:
        return query["c"][0]
    raise ValueError(f"Not a Gofile folder URL: {url}")


def parse_gofile_url(url: str) -> tuple[str, str | None]:
    """Return (folder_id, file_id) from a Gofile folder/file URL."""
    folder_id = parse_folder_url(url.split("#")[0])
    parsed = urlparse(url)
    file_id = None
    if parsed.fragment.startswith("file="):
        file_id = parsed.fragment.split("file=", 1)[1].split("&")[0]
    return folder_id, file_id


class GofileClient:
    """Premium Gofile API client. Requires GOFILE_TOKEN (subscription or PAYG)."""

    def __init__(
        self,
        token: str,
        password: str = "",
        *,
        timeout_sec: float = 120.0,
        cdn_prefer: str = "eu",
        cdn_probe: bool = False,
        download_connections: int = 1,
    ) -> None:
        if not token.strip():
            raise ValueError("GOFILE_TOKEN is required (premium account)")
        self.token = token.strip()
        self.password = password.strip()
        prefer = (cdn_prefer or "eu").lower()
        if prefer not in _CDN_PREFER_VALUES:
            raise ValueError(f"cdn_prefer must be one of {sorted(_CDN_PREFER_VALUES)}")
        self.cdn_prefer = prefer
        self.cdn_probe = cdn_probe
        self.download_connections = max(1, download_connections)
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout_sec, connect=30.0),
            headers={
                "User-Agent": _USER_AGENT,
                "Referer": "https://gofile.io/",
                "Origin": "https://gofile.io",
                "Authorization": f"Bearer {self.token}",
                "X-Website-Token": _STATIC_WT,
                "X-BL": "en-US",
            },
        )
        self._client.cookies.set("accountToken", self.token, domain="gofile.io")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GofileClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _request_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        max_429_retries: int = 5,
    ) -> dict[str, Any]:
        url = f"{_API}{path}"
        query = dict(params or {})
        query.setdefault("wt", _STATIC_WT)
        query.setdefault("cache", "true")
        last_error = "unknown"

        for attempt in range(max_429_retries):
            resp = self._client.get(url, params=query)
            if resp.status_code == 429:
                wait = int(resp.headers.get("retry-after", 30 + attempt * 30))
                logger.warning("Gofile 429 — waiting %ds (%d/%d)", wait, attempt + 1, max_429_retries)
                time.sleep(min(wait, 120))
                last_error = "429"
                continue
            resp.raise_for_status()
            body = resp.json()
            status = body.get("status", "")
            if status == "ok":
                return body
            last_error = status
            raise RuntimeError(f"Gofile API error: {status}")

        raise RuntimeError(f"Gofile rate limited ({last_error}). Wait and retry.")

    def _folder_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {
            "contentFilter": "",
            "sortField": "name",
            "sortDirection": "1",
        }
        if self.password:
            params["password"] = hashlib.sha256(self.password.encode()).hexdigest()
        return params

    def _get_folder_page(self, folder_id: str, page: int) -> dict[str, Any]:
        params = self._folder_params()
        params.update(page=str(page), pageSize=str(_PAGE_SIZE))
        body = self._request_json(f"/contents/{folder_id}", params=params)
        return body["data"]

    def _probe_download_speed(self, url: str, sample_bytes: int = _PROBE_SAMPLE_BYTES) -> float:
        """Return bytes/sec for a short Range sample, or 0.0 if the URL is unusable."""
        try:
            headers = {"Range": f"bytes=0-{sample_bytes - 1}"}
            nbytes = 0
            start = time.monotonic()
            with self._client.stream(
                "GET", url, headers=headers, follow_redirects=True
            ) as resp:
                if resp.status_code not in (200, 206):
                    return 0.0
                for chunk in resp.iter_bytes(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    nbytes += len(chunk)
                    if nbytes >= sample_bytes:
                        break
            elapsed = time.monotonic() - start
            return nbytes / elapsed if elapsed > 0 else 0.0
        except Exception:
            logger.debug("CDN probe failed for %s", url[:80], exc_info=True)
            return 0.0

    def _candidate_download_urls(self, data: dict[str, Any], file_id: str) -> list[str]:
        name = data.get("name")
        if not name:
            link = data.get("link") or data.get("directLink")
            return [str(link)] if link else []

        hosts = _order_server_hosts(_server_hosts_from_file_data(data), self.cdn_prefer)
        if hosts:
            return [_build_download_url(host, file_id, str(name)) for host in hosts]

        link = data.get("link") or data.get("directLink")
        return [str(link)] if link else []

    def _pick_download_url(self, candidates: list[str]) -> str | None:
        if not candidates:
            return None
        if len(candidates) == 1 or not self.cdn_probe:
            return candidates[0]

        best_url = candidates[0]
        best_speed = 0.0
        for url in candidates:
            speed = self._probe_download_speed(url)
            logger.info("CDN probe %s -> %.1f MiB/s", _host_from_gofile_url(url), speed / (1024**2))
            if speed > best_speed:
                best_speed = speed
                best_url = url
        if best_speed <= 0:
            logger.warning("CDN probe found no working mirror; using %s", best_url[:80])
        return best_url

    def _link_from_file_data(self, data: dict[str, Any], file_id: str) -> str | None:
        candidates = self._candidate_download_urls(data, file_id)
        link = self._pick_download_url(candidates)
        if link and len(candidates) > 1:
            host = _host_from_gofile_url(link)
            logger.info(
                "CDN pick %s (%d candidates, prefer=%s, probe=%s)",
                host,
                len(candidates),
                self.cdn_prefer,
                self.cdn_probe,
            )
        return link

    def get_file_info(self, file_id: str) -> dict[str, Any]:
        body = self._request_json(f"/contents/{file_id}")
        return body["data"]

    def resolve_direct_link(self, gofile_url: str) -> str:
        """Resolve a Gofile file page URL to a CDN download link."""
        _, file_id = parse_gofile_url(gofile_url)
        if not file_id:
            raise ValueError(f"Gofile URL must include #file=... : {gofile_url}")
        info = self.get_file_info(file_id)
        link = self._link_from_file_data(info, file_id)
        if not link:
            raise RuntimeError(f"No download link for file {file_id}")
        logger.info("Resolved %s -> %s", info.get("name", file_id), link[:80])
        return link

    def iter_files(self, folder_url: str) -> Iterator[GofileFile]:
        """Recursively yield files under a shared folder URL."""
        root_id = parse_folder_url(folder_url)
        data = self._get_folder_page(root_id, 1)
        root_name = (data.get("name") or "Shared").strip()
        yield from self._walk_folder(root_id, path_prefix=root_name)

    def _walk_folder(
        self,
        folder_id: str,
        *,
        path_prefix: str,
    ) -> Iterator[GofileFile]:
        page = 1
        while True:
            data = self._get_folder_page(folder_id, page)
            children = data.get("children") or {}
            if not children:
                break
            for child in children.values():
                ctype = child.get("type")
                name = child.get("name") or "unknown"
                child_id = str(child.get("id", ""))
                rel = f"{path_prefix}/{name}".lstrip("/")
                if ctype == "folder":
                    yield from self._walk_folder(child_id, path_prefix=rel)
                elif ctype == "file":
                    yield GofileFile(
                        file_id=child_id,
                        folder_id=folder_id,
                        name=name,
                        size_bytes=int(child.get("size") or 0),
                        path=rel,
                    )
            if len(children) < _PAGE_SIZE:
                break
            page += 1

    def _download_single_stream(
        self,
        url: str,
        part: Path,
        *,
        offset: int,
        expected_size: int | None,
        throttle_kbps: int,
        on_progress: Callable[[int, int | None], None] | None,
    ) -> None:
        headers: dict[str, str] = {}
        if offset:
            headers["Range"] = f"bytes={offset}-"
            logger.info("Resuming download at byte %d -> %s", offset, part.stem)
        mode = "ab" if offset else "wb"
        with self._client.stream("GET", url, headers=headers, follow_redirects=True) as resp:
            if resp.status_code == 416:
                if expected_size and offset == expected_size:
                    return
                raise RuntimeError(f"Download range not satisfiable at offset {offset}")
            if resp.status_code not in (200, 206):
                resp.raise_for_status()
            with part.open(mode) as fh:
                for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                    fh.write(chunk)
                    if on_progress:
                        on_progress(part.stat().st_size, expected_size)
                    if throttle_kbps > 0:
                        time.sleep(len(chunk) / (throttle_kbps * 1024))

    def _download_parallel_ranges(
        self,
        url: str,
        part: Path,
        total_size: int,
        *,
        connections: int,
        on_progress: Callable[[int, int | None], None] | None,
    ) -> None:
        connections = min(connections, total_size)
        chunk = (total_size + connections - 1) // connections
        ranges: list[tuple[int, int]] = []
        for index in range(connections):
            start = index * chunk
            if start >= total_size:
                break
            end = min(start + chunk - 1, total_size - 1)
            ranges.append((start, end))

        progress_bytes = 0
        progress_lock = threading.Lock()

        def fetch_range(span: tuple[int, int]) -> Path:
            start, end = span
            temp = part.with_suffix(f"{part.suffix}.{start}")
            headers = {"Range": f"bytes={start}-{end}"}
            with self._client.stream(
                "GET", url, headers=headers, follow_redirects=True
            ) as resp:
                if resp.status_code not in (200, 206):
                    resp.raise_for_status()
                with temp.open("wb") as fh:
                    for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                        fh.write(chunk)
            return temp

        temps: list[tuple[int, Path]] = []
        with ThreadPoolExecutor(max_workers=len(ranges)) as pool:
            futures = {pool.submit(fetch_range, span): span for span in ranges}
            for future in as_completed(futures):
                start, _ = futures[future]
                temp = future.result()
                temps.append((start, temp))
                if on_progress:
                    with progress_lock:
                        progress_bytes += temp.stat().st_size
                        on_progress(progress_bytes, total_size)

        with part.open("wb") as out:
            for _, temp in sorted(temps, key=lambda item: item[0]):
                out.write(temp.read_bytes())
                temp.unlink(missing_ok=True)

    def download_file(
        self,
        gofile_url: str,
        dest_path: str,
        *,
        expected_size: int | None = None,
        throttle_kbps: int = 0,
        on_progress: Callable[[int, int | None], None] | None = None,
    ) -> str:
        """Download with resume support (.part file)."""
        direct = self.resolve_direct_link(gofile_url)
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(dest.suffix + ".part")
        offset = part.stat().st_size if part.exists() else 0
        if offset and expected_size and offset == expected_size:
            part.rename(dest)
            return str(dest)

        use_parallel = (
            offset == 0
            and throttle_kbps <= 0
            and self.download_connections > 1
            and expected_size
            and expected_size > 0
        )
        if use_parallel:
            logger.info(
                "Parallel download (%d connections) -> %s",
                self.download_connections,
                dest.name,
            )
            self._download_parallel_ranges(
                direct,
                part,
                expected_size,
                connections=self.download_connections,
                on_progress=on_progress,
            )
        else:
            self._download_single_stream(
                direct,
                part,
                offset=offset,
                expected_size=expected_size,
                throttle_kbps=throttle_kbps,
                on_progress=on_progress,
            )

        part.rename(dest)
        size = dest.stat().st_size
        if expected_size and size != expected_size:
            logger.warning("Download size %d != expected %d for %s", size, expected_size, dest.name)
        return str(dest)

    def safe_dest_path(self, directory: Path, filename: str) -> Path:
        return directory / sanitize_filename(filename)
