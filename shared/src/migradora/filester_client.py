"""Filester REST API client."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("migradora.filester")


@dataclass(frozen=True)
class FilesterFolder:
    """Folder refs: identifier is used for uploads; db_id for nested create parent_id."""

    identifier: str
    name: str
    db_id: int | None = None
    parent_db_id: int | None = None


class FolderIndex:
    """Lookup folders by (parent_db_id, name) or identifier."""

    def __init__(self, folders: list[FilesterFolder]) -> None:
        self._by_key: dict[tuple[int | None, str], FilesterFolder] = {}
        self._by_identifier: dict[str, FilesterFolder] = {}
        for folder in folders:
            self.add(folder)

    def add(self, folder: FilesterFolder) -> None:
        self._by_key[(folder.parent_db_id, folder.name)] = folder
        self._by_identifier[folder.identifier] = folder

    def get(self, parent_db_id: int | None, name: str) -> FilesterFolder | None:
        return self._by_key.get((parent_db_id, name))

    def by_identifier(self, identifier: str) -> FilesterFolder | None:
        return self._by_identifier.get(identifier)

    def all_folders(self) -> list[FilesterFolder]:
        return list(self._by_identifier.values())

    def find_child(
        self,
        name: str,
        *,
        parent_db_id: int | None = None,
        parent_identifier: str | None = None,
    ) -> FilesterFolder | None:
        if parent_db_id is not None:
            hit = self.get(parent_db_id, name)
            if hit:
                return hit
        if parent_identifier:
            parent = self.by_identifier(parent_identifier)
            if parent and parent.db_id is not None:
                hit = self.get(parent.db_id, name)
                if hit:
                    return hit
        if parent_db_id is None and parent_identifier is None:
            return self.get(None, name)
        return None


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
        self._folder_index: FolderIndex | None = None

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

    @staticmethod
    def _parse_folder(raw: dict[str, Any]) -> FilesterFolder | None:
        name = (raw.get("name") or "").strip()
        if not name:
            return None

        identifier = str(raw.get("identifier") or raw.get("slug") or "").strip()
        db_id: int | None = None
        for key in ("id", "ID", "folder_id"):
            value = raw.get(key)
            if isinstance(value, int) and value > 0:
                db_id = value
                break
            if isinstance(value, str) and value.isdigit():
                db_id = int(value)
                break

        if not identifier:
            value = raw.get("id")
            if isinstance(value, str) and value and not value.isdigit():
                identifier = value

        if not identifier:
            return None

        parent_raw = raw.get("parent_id")
        parent_db_id: int | None = None
        if isinstance(parent_raw, int) and parent_raw > 0:
            parent_db_id = parent_raw
        elif isinstance(parent_raw, str) and parent_raw.isdigit():
            parent_db_id = int(parent_raw)

        return FilesterFolder(
            identifier=identifier,
            name=name,
            db_id=db_id,
            parent_db_id=parent_db_id,
        )

    def list_folders(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/api/v1/folders")
        return data.get("data", [])

    def _load_folders(self) -> list[FilesterFolder]:
        folders: list[FilesterFolder] = []
        seen: set[str] = set()

        for raw in self.list_folders():
            folder = self._parse_folder(raw)
            if folder and folder.identifier not in seen:
                folders.append(folder)
                seen.add(folder.identifier)

        try:
            data = self._request("GET", "/api/user/folders")
        except httpx.HTTPError as exc:
            logger.warning("Filester /api/user/folders unavailable: %s", exc)
            return folders

        for raw in data.get("folders") or []:
            folder = self._parse_folder(raw)
            if folder and folder.identifier not in seen:
                folders.append(folder)
                seen.add(folder.identifier)

        self._walk_hierarchical(data.get("hierarchical") or [], None, folders, seen)
        return folders

    def _walk_hierarchical(
        self,
        nodes: list[dict[str, Any]],
        parent_db_id: int | None,
        out: list[FilesterFolder],
        seen: set[str],
    ) -> None:
        for raw in nodes:
            parsed = self._parse_folder(raw)
            if not parsed:
                continue
            folder = parsed
            if folder.parent_db_id is None and parent_db_id is not None:
                folder = FilesterFolder(
                    identifier=folder.identifier,
                    name=folder.name,
                    db_id=folder.db_id,
                    parent_db_id=parent_db_id,
                )
            if folder.identifier not in seen:
                out.append(folder)
                seen.add(folder.identifier)
            child_parent = folder.db_id
            subs = raw.get("subfolders") or []
            if subs:
                self._walk_hierarchical(subs, child_parent, out, seen)

    def folder_index(self, *, refresh: bool = False) -> FolderIndex:
        if self._folder_index is None or refresh:
            self._folder_index = FolderIndex(self._load_folders())
        return self._folder_index

    def resolve_folder(self, identifier: str, name: str | None = None) -> FilesterFolder:
        """Reload folder list and return the best match for a folder identifier."""
        index = self.folder_index(refresh=True)
        folder = index.by_identifier(identifier)
        if folder:
            return folder
        if name:
            for candidate in index.all_folders():
                if candidate.name == name and candidate.identifier == identifier:
                    return candidate
        return FilesterFolder(identifier=identifier, name=name or "", db_id=None)

    def create_folder(
        self,
        name: str,
        *,
        parent_db_id: int | None = None,
        parent_identifier: str | None = None,
        public: int = 1,
    ) -> FilesterFolder:
        base: dict[str, object] = {"name": name[:100], "public": public}
        payloads: list[dict[str, object]] = []
        if parent_db_id:
            payloads.append({**base, "parent_id": parent_db_id})
        if parent_identifier:
            payloads.append({**base, "parent_id": parent_identifier})
            payloads.append({**base, "parent_folder_id": parent_identifier})
        if not payloads:
            payloads.append(base)

        last_error: Exception | None = None
        created_identifier: str | None = None
        for payload in payloads:
            for endpoint in ("/api/folder/create", "/api/v1/folder"):
                try:
                    data = self._request("POST", endpoint, json=payload)
                    folder = self._parse_folder(data.get("data", {}))
                    if folder and folder.identifier:
                        created_identifier = folder.identifier
                        resolved = self.resolve_folder(folder.identifier, name)
                        if self._folder_index is not None:
                            self._folder_index.add(resolved)
                        logger.info(
                            "Created Filester folder %r -> %s (db_id=%s, parent_db_id=%s)",
                            name,
                            resolved.identifier,
                            resolved.db_id,
                            parent_db_id or parent_identifier,
                        )
                        return resolved
                    identifier = str((data.get("data") or {}).get("identifier", ""))
                    if identifier:
                        created_identifier = identifier
                        resolved = self.resolve_folder(identifier, name)
                        if self._folder_index is not None:
                            self._folder_index.add(resolved)
                        return resolved
                except httpx.HTTPError as exc:
                    last_error = exc
                    logger.debug(
                        "Filester %s failed for %r (payload keys %s): %s",
                        endpoint,
                        name,
                        list(payload.keys()),
                        exc,
                    )

        if created_identifier:
            return self.resolve_folder(created_identifier, name)
        raise RuntimeError(f"Failed to create folder {name!r}: {last_error}")

    def upload_file(self, file_path: str | Path, folder_id: str | None = None) -> dict[str, Any]:
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
                    resp = self._client.post("/api/v1/upload", files=files, headers=headers)
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
            status = self._request("GET", "/api/v1/upload/status", params={"slug": slug})
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
