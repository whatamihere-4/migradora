"""Resolve Gofile page URLs to direct CDN download links."""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

logger = logging.getLogger("migradora.gofile")

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
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
        self.token = token.strip()
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
            return self.token
        resp = self._client.post("https://api.gofile.io/accounts")
        if resp.status_code == 429:
            raise RuntimeError(
                "Gofile rate-limited guest account creation (429). "
                "Set GOFILE_TOKEN in .env from https://gofile.io/myProfile and wait a few minutes."
            )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "ok":
            raise RuntimeError(f"Gofile guest account failed: {data}")
        self.token = str(data["data"]["token"])
        logger.info("Created Gofile guest token")
        return self.token

    def _website_tokens(self, lang: str = "en-US") -> list[str]:
        token = self._ensure_token()
        epoch = int(time.time() / 14400)
        return [
            hashlib.sha256(
                f"{_USER_AGENT}::{lang}::{token}::{epoch}::{salt}".encode()
            ).hexdigest()
            for salt in _WT_SALTS
        ]

    def _get_folder(self, folder_id: str) -> dict[str, Any]:
        token = self._ensure_token()
        params: dict[str, Any] = {
            "contentFilter": "",
            "page": "1",
            "pageSize": "1000",
            "sortField": "name",
            "sortDirection": "1",
        }
        if self.password:
            params["password"] = hashlib.sha256(self.password.encode()).hexdigest()
        last_error = "unknown"
        for wt in self._website_tokens():
            headers = {
                "Authorization": f"Bearer {token}",
                "X-Website-Token": wt,
                "X-BL": "en-US",
            }
            query = {**params, "wt": wt, "cache": "true"}
            resp = self._client.get(
                f"https://api.gofile.io/contents/{folder_id}",
                params=query,
                headers=headers,
            )
            if resp.status_code == 429:
                raise RuntimeError(
                    "Gofile API rate limited (429). Wait a few minutes or rotate VPN."
                )
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
                return body["data"]
            last_error = status
            if status in ("error-wrongToken", "error-notPremium"):
                continue
            break
        hint = (
            "Set GOFILE_TOKEN in .env (from https://gofile.io/myProfile)."
            if not self.token
            else "Check GOFILE_TOKEN / VPN egress IP."
        )
        raise RuntimeError(f"Gofile folder lookup failed ({last_error}). {hint}")

    def resolve_direct_link(self, gofile_url: str) -> str:
        """Resolve a Gofile file URL to a direct CDN download link."""
        folder_id, file_id = parse_gofile_url(gofile_url)
        folder = self._get_folder(folder_id)
        children = folder.get("children") or {}
        if not file_id:
            raise ValueError(f"Gofile URL must include #file=... : {gofile_url}")

        file_id_lower = file_id.lower()
        for child in children.values():
            if str(child.get("id", "")).lower() != file_id_lower:
                continue
            if child.get("type") != "file":
                continue
            link = child.get("link") or child.get("directLink")
            if link:
                logger.info(
                    "Resolved Gofile file %s -> %s",
                    child.get("name", file_id),
                    str(link)[:80],
                )
                return str(link)
        raise FileNotFoundError(
            f"File {file_id} not found in Gofile folder {folder_id}"
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
