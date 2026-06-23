"""Gofile Premium API client: folder crawl, link resolve, resumable download."""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
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
    ) -> None:
        if not token.strip():
            raise ValueError("GOFILE_TOKEN is required (premium account)")
        self.token = token.strip()
        self.password = password.strip()
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

    def _link_from_file_data(self, data: dict[str, Any], file_id: str) -> str | None:
        link = data.get("link") or data.get("directLink")
        if link:
            return str(link)
        server = data.get("serverSelected")
        if not server:
            servers = data.get("servers")
            if isinstance(servers, list) and servers:
                server = servers[0]
        name = data.get("name")
        if server and name:
            return f"https://{server}.gofile.io/download/web/{file_id}/{quote(name)}"
        return None

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

    def download_file(
        self,
        gofile_url: str,
        dest_path: str,
        *,
        expected_size: int | None = None,
        throttle_kbps: int = 0,
    ) -> str:
        """Download with resume support (.part file)."""
        direct = self.resolve_direct_link(gofile_url)
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(dest.suffix + ".part")
        offset = part.stat().st_size if part.exists() else 0
        headers: dict[str, str] = {}
        if offset:
            headers["Range"] = f"bytes={offset}-"
            logger.info("Resuming download at byte %d -> %s", offset, dest.name)

        mode = "ab" if offset else "wb"
        with self._client.stream("GET", direct, headers=headers, follow_redirects=True) as resp:
            if resp.status_code == 416:
                if expected_size and offset == expected_size:
                    part.rename(dest)
                    return str(dest)
                raise RuntimeError(f"Download range not satisfiable at offset {offset}")
            if resp.status_code not in (200, 206):
                resp.raise_for_status()
            with part.open(mode) as fh:
                for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                    fh.write(chunk)
                    if throttle_kbps > 0:
                        time.sleep(len(chunk) / (throttle_kbps * 1024))

        part.rename(dest)
        size = dest.stat().st_size
        if expected_size and size != expected_size:
            logger.warning("Download size %d != expected %d for %s", size, expected_size, dest.name)
        return str(dest)

    def safe_dest_path(self, directory: Path, filename: str) -> Path:
        return directory / sanitize_filename(filename)
