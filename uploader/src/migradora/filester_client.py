"""Filester REST API client."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("migradora.filester")


class FilesterClient:
    def __init__(
        self,
        api_key: str,
        api_base: str = "https://u1.filester.me",
        max_retries: int = 5,
        retry_delay: int = 30,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._client = httpx.Client(
            base_url=self.api_base,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(600.0, connect=30.0),
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> FilesterClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._client.request(method, path, **kwargs)
                if resp.status_code == 429:
                    wait = self.retry_delay * (2 ** attempt)
                    logger.warning("Filester rate limited, waiting %ds", wait)
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                if resp.content:
                    return resp.json()
                return {}
            except httpx.HTTPStatusError as exc:
                if attempt < self.max_retries and exc.response.status_code >= 500:
                    time.sleep(self.retry_delay)
                    continue
                raise
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt < self.max_retries:
                    time.sleep(self.retry_delay)
                    continue
                raise exc
        return {}

    def get_account(self) -> dict[str, Any]:
        data = self._request("GET", "/api/v1/account")
        return data.get("data", data)

    def list_folders(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/api/v1/folders")
        return data.get("data", [])

    def create_folder(self, name: str, public: int = 1) -> str:
        data = self._request(
            "POST",
            "/api/v1/folder",
            json={"name": name[:100], "public": public},
        )
        folder = data.get("data", {})
        folder_id = folder.get("identifier") or folder.get("id", "")
        if not folder_id:
            raise RuntimeError(f"Failed to create folder {name}: {data}")
        return str(folder_id)

    def get_or_create_folder(self, name: str, cache: dict[str, str]) -> str:
        if name in cache:
            return cache[name]
        for folder in self.list_folders():
            if folder.get("name") == name:
                fid = str(folder.get("id") or folder.get("identifier", ""))
                cache[name] = fid
                return fid
        fid = self.create_folder(name)
        cache[name] = fid
        return fid

    def ensure_folder_path(
        self, path_parts: list[str], cache: dict[str, str], root_name: str
    ) -> str | None:
        """Ensure nested folder hierarchy exists; returns leaf folder ID."""
        if not path_parts:
            return None
        current_id: str | None = None
        built_path = ""
        for part in path_parts:
            built_path = f"{built_path}/{part}" if built_path else part
            if built_path in cache:
                current_id = cache[built_path]
                continue
            # Create under root or nested - Filester API creates top-level folders
            # We use flat naming with path separator for simplicity when nesting unsupported
            folder_name = built_path.replace("/", " - ")[-100:]
            current_id = self.create_folder(folder_name)
            cache[built_path] = current_id
        return current_id

    def upload_file(
        self,
        file_path: str | Path,
        folder_id: str | None = None,
    ) -> dict[str, Any]:
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(str(file_path))

        headers: dict[str, str] = {}
        if folder_id:
            headers["X-Folder-ID"] = folder_id

        for attempt in range(self.max_retries + 1):
            try:
                with open(file_path, "rb") as fh:
                    files = {"file": (file_path.name, fh, "application/octet-stream")}
                    resp = self._client.post(
                        "/api/v1/upload",
                        files=files,
                        headers=headers,
                    )
                if resp.status_code == 429:
                    time.sleep(self.retry_delay * (2 ** attempt))
                    continue
                resp.raise_for_status()
                return resp.json()
            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt < self.max_retries:
                    logger.warning("Upload attempt %d failed: %s", attempt + 1, exc)
                    time.sleep(self.retry_delay)
                    continue
                raise
        raise RuntimeError(f"Upload failed after retries: {file_path}")

    def verify_upload(self, slug: str, expected_size: int) -> bool:
        try:
            status = self._request("GET", f"/api/v1/upload/status", params={"slug": slug})
            if status.get("status") != "completed":
                return False
            detail = self._request("GET", f"/api/v1/file/{slug}")
            file_data = detail.get("data", detail)
            actual_size = int(file_data.get("size", 0))
            if expected_size and actual_size != expected_size:
                logger.warning(
                    "Size mismatch for %s: expected %d, got %d",
                    slug, expected_size, actual_size,
                )
                return False
            return True
        except Exception as exc:
            logger.error("Verify failed for %s: %s", slug, exc)
            return False
