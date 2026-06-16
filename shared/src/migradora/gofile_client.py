"""
Gofile API client adapted from martadams89/gofile-dl (MIT License).
https://github.com/martadams89/gofile-dl

Provides web-scraping fallback for free accounts (March 2026 API restrictions),
resumable downloads, and recursive folder discovery.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional

import requests
from pathvalidate import sanitize_filename

logger = logging.getLogger("migradora.gofile")

DEFAULT_TIMEOUT = 30
CONTENT_TIMEOUT = 45


@dataclass
class GofileItem:
    content_id: str
    name: str
    item_type: str  # "file" or "folder"
    size_bytes: int
    download_link: str | None
    path: str


class GoFileClient:
    def __init__(self, token: str | None = None) -> None:
        self.token = token or ""
        self.wt = ""
        self._session = requests.Session()

    def _ensure_auth(self) -> None:
        if not self.token:
            if os.getenv("GOFILE_TOKEN"):
                self.token = os.getenv("GOFILE_TOKEN", "")
                logger.info("Using GOFILE_TOKEN from environment")
            else:
                for attempt in range(3):
                    try:
                        resp = requests.post(
                            "https://api.gofile.io/accounts", timeout=DEFAULT_TIMEOUT
                        )
                        data = resp.json()
                        if data.get("status") == "ok":
                            self.token = data["data"].get("token", "")
                            break
                    except requests.exceptions.Timeout:
                        if attempt < 2:
                            time.sleep(2)
        if not self.wt:
            try:
                js = requests.get(
                    "https://gofile.io/dist/js/config.js", timeout=DEFAULT_TIMEOUT
                ).text
                if 'appdata.wt = "' in js:
                    self.wt = js.split('appdata.wt = "')[1].split('"')[0]
            except Exception as exc:
                logger.warning("Failed to fetch wt token: %s", exc)

    def get_content(
        self, content_id: str, password: str | None = None
    ) -> dict[str, Any] | None:
        self._ensure_auth()
        params: dict[str, str] = {}
        if password:
            params["password"] = hashlib.sha256(password.encode()).hexdigest()

        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-Website-Token": self.wt,
        }

        for attempt in range(3):
            try:
                resp = self._session.get(
                    f"https://api.gofile.io/contents/{content_id}",
                    headers=headers,
                    params=params,
                    timeout=CONTENT_TIMEOUT,
                )
                data = resp.json()
                if data.get("status") == "ok":
                    return data
                if data.get("status") == "error-notPremium":
                    logger.warning("API notPremium for %s, trying web fallback", content_id)
                    return self._get_content_from_web(content_id, password)
                logger.error("Gofile API error for %s: %s", content_id, data)
                return None
            except requests.exceptions.Timeout:
                if attempt < 2:
                    time.sleep(3)
                else:
                    logger.error("Timeout fetching content %s", content_id)
        return None

    def _get_content_from_web(
        self, content_id: str, password: str | None = None
    ) -> dict[str, Any] | None:
        url = f"https://gofile.io/d/{content_id}"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://gofile.io/",
        }
        try:
            page = self._session.get(url, headers=headers, timeout=CONTENT_TIMEOUT)
            page.raise_for_status()

            api_headers = {
                "User-Agent": headers["User-Agent"],
                "Accept": "application/json",
                "Origin": "https://gofile.io",
                "Referer": url,
                "Authorization": f"Bearer {self.token}",
                "X-Website-Token": self.wt,
            }
            params: dict[str, str] = {}
            if password:
                params["password"] = hashlib.sha256(password.encode()).hexdigest()

            api_resp = self._session.get(
                f"https://api.gofile.io/contents/{content_id}",
                headers=api_headers,
                params=params,
                timeout=CONTENT_TIMEOUT,
            )
            data = api_resp.json()
            if data.get("status") == "ok":
                logger.info("Web fallback successful for %s", content_id)
                return data

            patterns = [
                r"contentData\s*=\s*({.*?});",
                r"window\.contentData\s*=\s*({.*?});",
            ]
            for pattern in patterns:
                matches = re.findall(pattern, page.text, re.DOTALL)
                for match in matches:
                    try:
                        content_json = json.loads(match)
                        return {"status": "ok", "data": content_json}
                    except json.JSONDecodeError:
                        continue
        except Exception as exc:
            logger.error("Web fallback failed for %s: %s", content_id, exc)
        return None

    def iter_folder(
        self,
        content_id: str,
        base_path: str = "",
        password: str | None = None,
        delay_sec: float = 0,
    ) -> Iterator[GofileItem]:
        data = self.get_content(content_id, password)
        if not data or data.get("status") != "ok":
            return

        info = data["data"]
        if info.get("passwordStatus", "passwordOk") != "passwordOk":
            logger.error("Invalid password for %s", content_id)
            return

        if info.get("type") == "file":
            yield GofileItem(
                content_id=content_id,
                name=info.get("name", "unknown"),
                item_type="file",
                size_bytes=int(info.get("size", 0) or 0),
                download_link=info.get("link"),
                path=base_path or info.get("name", ""),
            )
            return

        folder_name = sanitize_filename(info.get("name", "folder"))
        folder_path = f"{base_path}/{folder_name}" if base_path else folder_name

        children = info.get("children") or info.get("contents") or {}
        for child_id, child in children.items():
            child_type = child.get("type", "file")
            if child_type == "folder":
                if delay_sec:
                    time.sleep(delay_sec)
                yield from self.iter_folder(
                    child_id, folder_path, password=password, delay_sec=delay_sec
                )
            else:
                yield GofileItem(
                    content_id=child_id,
                    name=child.get("name", "unknown"),
                    item_type="file",
                    size_bytes=int(child.get("size", 0) or 0),
                    download_link=child.get("link"),
                    path=f"{folder_path}/{child.get('name', 'unknown')}",
                )

    def download_file(
        self,
        link: str,
        dest_path: str,
        *,
        max_retries: int = 5,
        retry_delay: int = 30,
        throttle_kbps: int = 0,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> None:
        """Download with resumable .part files and optional throttling."""
        temp = dest_path + ".part"
        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)

        for attempt in range(max_retries + 1):
            try:
                existing = os.path.getsize(temp) if os.path.exists(temp) else 0
                headers = {
                    "Cookie": f"accountToken={self.token}",
                    "Range": f"bytes={existing}-",
                }
                with self._session.get(
                    link, headers=headers, stream=True, timeout=DEFAULT_TIMEOUT
                ) as resp:
                    resp.raise_for_status()
                    total = int(resp.headers.get("Content-Length", 0)) + existing
                    downloaded = existing

                    with open(temp, "ab") as fh:
                        bytes_since_check = 0
                        last_check = time.time()
                        for chunk in resp.iter_content(chunk_size=1024 * 1024):
                            if not chunk:
                                continue
                            fh.write(chunk)
                            downloaded += len(chunk)
                            if progress_callback and total > 0:
                                progress_callback(downloaded, total)
                            if throttle_kbps:
                                bytes_since_check += len(chunk)
                                elapsed = time.time() - last_check
                                if elapsed > 0:
                                    rate = bytes_since_check / elapsed
                                    if rate > throttle_kbps * 1024:
                                        sleep_t = (bytes_since_check / (throttle_kbps * 1024)) - elapsed
                                        if sleep_t > 0:
                                            time.sleep(sleep_t)
                                    bytes_since_check = 0
                                    last_check = time.time()

                os.rename(temp, dest_path)
                if progress_callback:
                    progress_callback(downloaded, downloaded or 1)
                logger.info("Downloaded %s", dest_path)
                return
            except Exception as exc:
                logger.warning("Download attempt %d failed for %s: %s", attempt + 1, dest_path, exc)
                if attempt < max_retries:
                    time.sleep(retry_delay)
                else:
                    if os.path.exists(temp):
                        os.remove(temp)
                    raise

    @staticmethod
    def extract_content_id(url: str) -> str:
        return url.rstrip("/").split("/")[-1]
