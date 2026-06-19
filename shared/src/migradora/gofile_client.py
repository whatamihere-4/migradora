"""Resolve Gofile page URLs to direct CDN download links."""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import httpx

logger = logging.getLogger("migradora.gofile")

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# From https://gofile.io/dist/js/config.js — works for per-file /contents/{fileId}
_STATIC_WT = "4fd6sg89d7s6"
_WT_SALTS = ("gf2026x", "5d4f7g8sd45fsd")


def parse_gofile_url(url: str) -> tuple[str, str | None]:
    """Return (folder_id, file_id) from a Gofile folder/file URL."""
    parsed = urlparse(url)
    folder_id: str | None = None
    if parsed.path.startswith("/d/"):
        folder_id = parsed.path.split("/d/", 1)[1].split("/")[0]
    query = parse_qs(parsed.query)
    if "c" in query and query["c"]:
        folder_id = query["c"][0]
    if not folder_id:
        raise ValueError(f"Could not parse Gofile folder id from {url}")
    file_id = None
    if parsed.fragment.startswith("file="):
        file_id = parsed.fragment.split("file=", 1)[1].split("&")[0]
    return folder_id, file_id


class GofileClient:
    def __init__(
        self,
        token: str = "",
        password: str = "",
        *,
        timeout_sec: float = 60.0,
    ) -> None:
        self._config_token = token.strip()
        self.token = self._config_token
        self.password = password.strip()
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout_sec, connect=15.0),
            headers={
                "User-Agent": _USER_AGENT,
                "Referer": "https://gofile.io/",
                "Origin": "https://gofile.io",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GofileClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _ensure_token(self) -> str:
        if self.token:
            self._client.cookies.set("accountToken", self.token, domain="gofile.io")
            return self.token
        resp = self._client.post("https://api.gofile.io/accounts")
        if resp.status_code == 429:
            raise RuntimeError(
                "Gofile rate-limited guest account creation (429). "
                "Set GOFILE_TOKEN in .env and wait a few minutes."
            )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "ok":
            raise RuntimeError(f"Gofile guest account failed: {data}")
        self.token = str(data["data"]["token"])
        self._client.cookies.set("accountToken", self.token, domain="gofile.io")
        logger.info("Created Gofile guest token")
        return self.token

    def _website_tokens(self) -> list[str]:
        token = self._ensure_token()
        epoch = int(time.time() / 14400)
        dynamic = [
            hashlib.sha256(
                f"{_USER_AGENT}::en-US::{token}::{epoch}::{salt}".encode()
            ).hexdigest()
            for salt in _WT_SALTS
        ]
        return [_STATIC_WT, *dynamic]

    def _request_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        max_429_retries: int = 3,
    ) -> dict[str, Any]:
        last_error = "unknown"
        for attempt in range(max_429_retries):
            for wt in self._website_tokens():
                token = self._ensure_token()
                headers = {
                    "Authorization": f"Bearer {token}",
                    "X-Website-Token": wt,
                    "X-BL": "en-US",
                }
                query = dict(params or {})
                query.setdefault("wt", wt)
                query.setdefault("cache", "true")
                resp = self._client.get(url, params=query, headers=headers)
                if resp.status_code == 429:
                    wait = int(resp.headers.get("retry-after", 30 + attempt * 30))
                    logger.warning(
                        "Gofile 429 on %s — waiting %ds (attempt %d/%d)",
                        url,
                        wait,
                        attempt + 1,
                        max_429_retries,
                    )
                    time.sleep(min(wait, 120))
                    last_error = "429"
                    break
                if resp.status_code == 401:
                    last_error = "http-401"
                    continue
                try:
                    body = resp.json()
                except Exception:
                    resp.raise_for_status()
                    raise
                status = body.get("status", "")
                if status == "ok":
                    return body
                last_error = status
                if status in ("error-wrongToken", "error-notPremium"):
                    continue
                raise RuntimeError(f"Gofile API error: {status}")
            else:
                continue
            continue
        raise RuntimeError(
            f"Gofile request failed ({last_error}). "
            "Wait a few minutes if rate-limited, or set GOFILE_TOKEN in .env."
        )

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

    def _get_file_info(self, file_id: str) -> dict[str, Any]:
        """Per-file metadata — avoids folder listing API (less rate limiting)."""
        body = self._request_json(f"https://api.gofile.io/contents/{file_id}")
        return body["data"]

    def _get_folder(self, folder_id: str) -> dict[str, Any]:
        params: dict[str, Any] = {
            "contentFilter": "",
            "page": "1",
            "pageSize": "1000",
            "sortField": "name",
            "sortDirection": "1",
        }
        if self.password:
            params["password"] = hashlib.sha256(self.password.encode()).hexdigest()
        body = self._request_json(
            f"https://api.gofile.io/contents/{folder_id}",
            params=params,
        )
        return body["data"]

    def resolve_direct_link(self, gofile_url: str) -> str:
        """Resolve a Gofile file URL to a direct CDN download link."""
        folder_id, file_id = parse_gofile_url(gofile_url)
        if not file_id:
            raise ValueError(f"Gofile URL must include #file=... : {gofile_url}")

        errors: list[str] = []

        try:
            info = self._get_file_info(file_id)
            link = self._link_from_file_data(info, file_id)
            if link:
                logger.info(
                    "Resolved Gofile file %s via per-file API -> %s",
                    info.get("name", file_id),
                    link[:80],
                )
                return link
            errors.append("per-file API returned no link")
        except Exception as exc:
            errors.append(f"per-file API: {exc}")
            logger.warning("Gofile per-file lookup failed: %s", exc)

        try:
            folder = self._get_folder(folder_id)
            children = folder.get("children") or {}
            file_id_lower = file_id.lower()
            for child in children.values():
                if str(child.get("id", "")).lower() != file_id_lower:
                    continue
                if child.get("type") != "file":
                    continue
                link = self._link_from_file_data(child, file_id)
                if link:
                    logger.info(
                        "Resolved Gofile file %s via folder API -> %s",
                        child.get("name", file_id),
                        link[:80],
                    )
                    return link
            errors.append("file not in folder children")
        except Exception as exc:
            errors.append(f"folder API: {exc}")
            logger.warning("Gofile folder lookup failed: %s", exc)

        raise RuntimeError(
            f"Could not resolve Gofile download link for {file_id}: {'; '.join(errors)}"
        )

    def download_file(
        self,
        gofile_url: str,
        dest_path: str,
        *,
        expected_size: int | None = None,
    ) -> str:
        """Download directly to dest_path (bypasses JD2)."""
        direct = self.resolve_direct_link(gofile_url)
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._client.stream("GET", direct, follow_redirects=True) as resp:
            resp.raise_for_status()
            with dest.open("wb") as fh:
                for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                    fh.write(chunk)
        size = dest.stat().st_size
        if expected_size and size != expected_size:
            logger.warning("Download size %d != expected %d", size, expected_size)
        return str(dest)
