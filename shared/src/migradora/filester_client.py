"""Filester REST API client."""

from __future__ import annotations

import logging
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from migradora.transfer_stats import format_size

logger = logging.getLogger("migradora.filester")


@dataclass(frozen=True)
class FilesterFolder:
    """Folder refs: identifier is used for uploads; db_id for nested create parent_id."""

    identifier: str
    name: str
    db_id: int | None = None
    parent_db_id: int | None = None
    parent_identifier: str | None = None


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
            hit = self.get(None, name)
            if hit:
                return hit

        return self._find_by_name_fallback(name, parent_db_id, parent_identifier)

    def _find_by_name_fallback(
        self,
        name: str,
        parent_db_id: int | None,
        parent_identifier: str | None,
    ) -> FilesterFolder | None:
        matches = [f for f in self.all_folders() if f.name == name]
        if parent_db_id is not None:
            matches = [f for f in matches if f.parent_db_id == parent_db_id]
        elif parent_identifier:
            parent = self.by_identifier(parent_identifier)
            if parent and parent.db_id is not None:
                matches = [f for f in matches if f.parent_db_id == parent.db_id]
            else:
                return None
        else:
            matches = [f for f in matches if f.parent_db_id is None]
        if len(matches) == 1:
            return matches[0]
        return None


class FilesterClient:
    def __init__(
        self,
        api_key: str,
        api_base: str = "https://u1.filester.me",
        max_retries: int = 5,
        retry_delay: int = 30,
        upload_chunk_bytes: int = 1024 * 1024,
        upload_write_timeout_sec: int = 120,
        upload_throttle_kbps: int = 0,
        api_read_timeout_sec: int = 60,
        verify_upload_enabled: bool = True,
        verify_upload_strict: bool = False,
        verify_upload_request_timeout_sec: int = 20,
        verify_upload_max_wait_sec: int = 45,
        verify_upload_poll_sec: float = 2.0,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._api_key = api_key
        self._upload_chunk_bytes = max(64 * 1024, upload_chunk_bytes)
        self._upload_write_timeout_sec = max(30, upload_write_timeout_sec)
        self._upload_throttle_kbps = max(0, upload_throttle_kbps)
        self._api_read_timeout_sec = max(10, api_read_timeout_sec)
        self._verify_upload_enabled = verify_upload_enabled
        self._verify_upload_strict = verify_upload_strict
        self._verify_upload_request_timeout_sec = max(5, verify_upload_request_timeout_sec)
        self._verify_upload_max_wait_sec = max(5, verify_upload_max_wait_sec)
        self._verify_upload_poll_sec = max(0.5, verify_upload_poll_sec)
        self._client = self._make_client()
        self._folder_index: FolderIndex | None = None
        self._nested_folder_cache: dict[tuple[str, str], FilesterFolder] = {}

    def _make_client(self) -> httpx.Client:
        # Fresh TCP per request avoids stale keep-alive sockets on long uploads.
        # write timeout: retry when the socket stops accepting data (frozen connection).
        return httpx.Client(
            base_url=self.api_base,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=httpx.Timeout(
                600.0,
                connect=30.0,
                write=float(self._upload_write_timeout_sec),
            ),
            limits=httpx.Limits(max_keepalive_connections=0, max_connections=10),
        )

    def reset_connections(self) -> None:
        """Drop pooled connections between large upload parts."""
        self._client.close()
        self._client = self._make_client()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> FilesterClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _api_timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            float(self._api_read_timeout_sec),
            connect=15.0,
            write=float(self._api_read_timeout_sec),
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        timeout: httpx.Timeout | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        req_timeout = timeout or self._api_timeout()
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._client.request(method, path, timeout=req_timeout, **kwargs)
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

    def _api_request_once(
        self,
        method: str,
        path: str,
        *,
        timeout: httpx.Timeout | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Single HTTP attempt with no retries (used for post-upload verify)."""
        req_timeout = timeout or self._api_timeout()
        resp = self._client.request(method, path, timeout=req_timeout, **kwargs)
        if resp.status_code == 429:
            raise httpx.HTTPStatusError(
                "rate limited",
                request=resp.request,
                response=resp,
            )
        resp.raise_for_status()
        if resp.content:
            parsed = resp.json()
            return parsed if isinstance(parsed, dict) else {}
        return {}

    def _post_folder(self, endpoint: str, payload: dict[str, object]) -> dict[str, Any]:
        return self._request("POST", endpoint, json=payload)

    def _raw_request(
        self, method: str, path: str, **kwargs: Any
    ) -> tuple[int, dict[str, Any] | None, str]:
        resp = self._client.request(method, path, **kwargs)
        body: dict[str, Any] | None = None
        text = resp.text or ""
        if resp.content:
            try:
                parsed = resp.json()
                if isinstance(parsed, dict):
                    body = parsed
            except ValueError:
                body = None
        return resp.status_code, body, text

    def get_account(self) -> dict[str, Any]:
        data = self._request("GET", "/api/v1/account")
        return data.get("data", data)

    @staticmethod
    def _parse_parent_identifier(raw: dict[str, Any]) -> str | None:
        parent = raw.get("parent")
        if isinstance(parent, str):
            value = parent.strip()
            if value and value.lower() != "root":
                return value
        parent_id = raw.get("parent_id")
        if isinstance(parent_id, str):
            value = parent_id.strip()
            if value and value.lower() != "root" and not value.isdigit():
                return value
        return None

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
            if isinstance(value, str) and value:
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
            parent_identifier=FilesterClient._parse_parent_identifier(raw),
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

        return folders

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

    def list_child_folders(self, parent_identifier: str) -> list[FilesterFolder]:
        """List folders nested under ``parent_identifier``.

        Filester's ``GET /api/v1/folder/{id}/folders`` currently returns a flat
        account list without parent metadata. Rows are only kept when the API
        includes a ``parent`` field matching ``parent_identifier``.
        """
        parent = (parent_identifier or "").strip()
        if not parent:
            return []

        folders: list[FilesterFolder] = []
        seen: set[str] = set()
        candidates = [
            f"/api/v1/folders?parent={parent}",
            f"/api/v1/folder/{parent}/folders",
        ]
        for path in candidates:
            try:
                data = self._request("GET", path)
            except httpx.HTTPError:
                continue
            rows = data.get("data")
            if not isinstance(rows, list):
                continue
            for raw in rows:
                if not isinstance(raw, dict):
                    continue
                row_parent = self._parse_parent_identifier(raw)
                if row_parent != parent:
                    continue
                folder = self._parse_folder(raw)
                if folder and folder.identifier not in seen:
                    folders.append(folder)
                    seen.add(folder.identifier)
        return folders

    def _find_folder_under_parent(
        self,
        name: str,
        parent_identifier: str,
    ) -> FilesterFolder | None:
        """Return a folder named ``name`` only when API reports the expected parent."""
        parent = (parent_identifier or "").strip()
        if not parent:
            return None
        matches = [
            folder
            for folder in self.list_child_folders(parent)
            if folder.name == name
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def _resolve_existing_nested_folder(
        self,
        name: str,
        parent_identifier: str,
    ) -> FilesterFolder | None:
        """Find an existing nested folder by name, verifying parent via folder detail."""
        parent = (parent_identifier or "").strip()
        if not parent:
            return None

        cached = self._cached_nested_folder(name, parent)
        if cached:
            return cached

        index = self.folder_index(refresh=True)
        for folder in index.all_folders():
            if folder.name != name:
                continue
            actual_parent = folder.parent_identifier or self.get_folder_parent_identifier(
                folder.identifier
            )
            if actual_parent != parent:
                continue
            resolved = FilesterFolder(
                identifier=folder.identifier,
                name=folder.name,
                db_id=folder.db_id,
                parent_db_id=folder.parent_db_id,
                parent_identifier=actual_parent,
            )
            if self._folder_index is not None:
                self._folder_index.add(resolved)
            self._remember_nested_folder(resolved, parent)
            return resolved
        return None

    def _folder_is_at_account_root(self, folder_identifier: str) -> bool:
        parent = self.get_folder_parent_identifier(folder_identifier)
        return parent is None or parent == "root"

    def _remember_nested_folder(
        self,
        folder: FilesterFolder,
        parent_identifier: str,
    ) -> None:
        parent = (parent_identifier or "").strip()
        if parent and folder.name:
            self._nested_folder_cache[(parent, folder.name)] = folder

    def _cached_nested_folder(
        self,
        name: str,
        parent_identifier: str,
    ) -> FilesterFolder | None:
        return self._nested_folder_cache.get(
            ((parent_identifier or "").strip(), name)
        )

    def get_folder_parent_identifier(self, folder_identifier: str) -> str | None:
        """Best-effort parent lookup for a folder identifier."""
        fid = (folder_identifier or "").strip()
        if not fid:
            return None
        index = self.folder_index()
        cached = index.by_identifier(fid)
        if cached and cached.parent_identifier:
            return cached.parent_identifier

        for path in (
            f"/api/v1/folder/{fid}",
            f"/api/v1/folders/{fid}",
            f"/api/v1/folder/{fid}/detail",
        ):
            try:
                status, body, _text = self._raw_request("GET", path)
            except httpx.HTTPError:
                continue
            if status != 200 or not body:
                continue
            data = body.get("data", body)
            if isinstance(data, dict):
                folder = data.get("folder")
                if isinstance(folder, dict):
                    parent = self._parse_parent_identifier(folder)
                    if parent:
                        return parent
                parent = self._parse_parent_identifier(data)
                if parent:
                    return parent
        return None

    def find_folder(
        self,
        name: str,
        *,
        parent_db_id: int | None = None,
        parent_identifier: str | None = None,
    ) -> FilesterFolder | None:
        if parent_identifier:
            hit = self._find_folder_under_parent(name, parent_identifier)
            if hit:
                return hit
            return None

        index = self.folder_index(refresh=parent_db_id is None)
        if parent_db_id is not None:
            return index.find_child(name, parent_db_id=parent_db_id)

        matches = [f for f in index.all_folders() if f.name == name]
        if not matches:
            return None
        if len(matches) > 1:
            logger.warning(
                "Multiple root Filester folders named %r; using %s",
                name,
                matches[0].identifier,
            )
        return matches[0]

    def folder_is_under_parent(
        self,
        child_identifier: str,
        *,
        parent_identifier: str,
    ) -> bool:
        parent = (parent_identifier or "").strip()
        child = (child_identifier or "").strip()
        if not parent or not child:
            return False
        actual_parent = self.get_folder_parent_identifier(child)
        if actual_parent:
            return actual_parent == parent
        return False

    def assert_nested_folder(
        self,
        folder: FilesterFolder,
        name: str,
        *,
        parent_identifier: str | None = None,
        parent_db_id: int | None = None,
    ) -> None:
        """Raise if a folder intended to be nested is actually at account root."""
        if not parent_identifier and parent_db_id is None:
            return

        expected_parent = (parent_identifier or "").strip()
        if folder.parent_identifier:
            if folder.parent_identifier == expected_parent:
                return
            if folder.parent_identifier == "root" or not expected_parent:
                pass
            else:
                raise RuntimeError(
                    f"Folder {name!r} ({folder.identifier}) has parent "
                    f"{folder.parent_identifier}, expected {expected_parent}"
                )

        if expected_parent and self.folder_is_under_parent(
            folder.identifier,
            parent_identifier=expected_parent,
        ):
            return

        if expected_parent:
            actual_parent = self.get_folder_parent_identifier(folder.identifier)
            if actual_parent == expected_parent:
                return
            if actual_parent in (None, "root"):
                raise RuntimeError(
                    f"Folder {name!r} ({folder.identifier}) is at the Filester "
                    f"account root, not under {expected_parent}. "
                    f"Delete the top-level {name!r} folder on Filester and retry."
                )
            raise RuntimeError(
                f"Folder {name!r} ({folder.identifier}) is under {actual_parent}, "
                f"not {expected_parent}"
            )

        root = self.find_folder(name)
        if root and root.identifier == folder.identifier:
            parent = parent_identifier or str(parent_db_id)
            raise RuntimeError(
                f"Folder {name!r} exists at the Filester account root "
                f"({folder.identifier}), not under {parent}. "
                f"Delete the top-level {name!r} folder on Filester and retry."
            )
        raise RuntimeError(
            f"Folder {name!r} ({folder.identifier}) is not nested under "
            f"{parent_identifier or parent_db_id}"
        )

    @staticmethod
    def _identifier_from_error(exc: httpx.HTTPStatusError) -> str | None:
        try:
            body = exc.response.json()
        except ValueError:
            return None
        if not isinstance(body, dict):
            return None
        data = body.get("data")
        if isinstance(data, dict):
            for key in ("identifier", "id", "folder_id"):
                value = data.get(key)
                if isinstance(value, str) and value:
                    return value
        return None

    def create_folder(
        self,
        name: str,
        *,
        parent_db_id: int | None = None,
        parent_identifier: str | None = None,
        public: int = 1,
        name_suffix: str | None = None,
    ) -> FilesterFolder:
        folder_name = name[:100]
        if name_suffix:
            suffix = str(name_suffix).strip()
            max_base = 100 - len(suffix) - 1
            if max_base < 1:
                folder_name = suffix[:100]
            else:
                folder_name = f"{folder_name[:max_base].rstrip()}-{suffix}"

        nested_parent = (parent_identifier or "").strip()
        if not nested_parent and parent_db_id is None:
            existing = self.find_folder(folder_name)
            if existing:
                logger.info(
                    "Reusing Filester folder %r -> %s (parent=root)",
                    folder_name,
                    existing.identifier,
                )
                return existing
        elif nested_parent:
            existing = self._cached_nested_folder(folder_name, nested_parent)
            if not existing:
                existing = self._find_folder_under_parent(folder_name, nested_parent)
            if not existing:
                existing = self._resolve_existing_nested_folder(folder_name, nested_parent)
            if existing:
                logger.info(
                    "Reusing Filester folder %r -> %s (parent=%s)",
                    folder_name,
                    existing.identifier,
                    nested_parent,
                )
                return existing

        payload: dict[str, object] = {"name": folder_name, "public": public}
        if parent_db_id is not None:
            payload["parent"] = parent_db_id
        elif nested_parent:
            payload["parent"] = nested_parent

        try:
            data = self._post_folder("/api/v1/folder", payload)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409:
                message = ""
                try:
                    body = exc.response.json()
                    if isinstance(body, dict):
                        message = str(body.get("message") or "")
                except ValueError:
                    message = ""
                if nested_parent or parent_db_id is not None:
                    expected_parent = nested_parent or str(parent_db_id)
                    conflict = None
                    if nested_parent:
                        conflict = self._resolve_existing_nested_folder(
                            folder_name,
                            expected_parent,
                        )
                    if conflict is None and nested_parent:
                        conflict = self._find_folder_under_parent(
                            folder_name,
                            nested_parent,
                        )
                    if conflict:
                        logger.info(
                            "Folder %r already exists under %s -> %s (409)",
                            folder_name,
                            expected_parent,
                            conflict.identifier,
                        )
                        if nested_parent:
                            self._remember_nested_folder(conflict, nested_parent)
                        return conflict
                    if name_suffix is None and "exist" in message.lower():
                        return self.create_folder(
                            name,
                            parent_db_id=parent_db_id,
                            parent_identifier=parent_identifier,
                            public=public,
                            name_suffix="2",
                        )
                    if nested_parent:
                        for folder in self.folder_index(refresh=True).all_folders():
                            if folder.name != folder_name:
                                continue
                            if self._folder_is_at_account_root(folder.identifier):
                                raise RuntimeError(
                                    f"Cannot create nested folder {folder_name!r} under "
                                    f"{nested_parent}: a top-level folder with that name "
                                    f"already exists ({folder.identifier}). Delete it on "
                                    f"Filester and retry."
                                ) from exc
                    raise RuntimeError(
                        f"Cannot create nested folder {folder_name!r}: {exc}"
                    ) from exc
                conflict_id = self._identifier_from_error(exc)
                if conflict_id:
                    return self.resolve_folder(conflict_id, folder_name)
            raise RuntimeError(f"Failed to create folder {folder_name!r}: {exc}") from exc

        folder = self._parse_folder_from_create(data)
        if folder:
            if nested_parent or parent_db_id is not None:
                self.assert_nested_folder(
                    folder,
                    folder_name,
                    parent_identifier=nested_parent or None,
                    parent_db_id=parent_db_id,
                )
            if self._folder_index is not None:
                self._folder_index.add(folder)
            if nested_parent:
                self._remember_nested_folder(folder, nested_parent)
            logger.info(
                "Created Filester folder %r -> %s (parent=%s)",
                folder_name,
                folder.identifier,
                nested_parent or parent_db_id or "root",
            )
            return folder

        raise RuntimeError(f"Failed to create folder {folder_name!r}: {data}")

    @staticmethod
    def file_identifier_from_response(raw: dict[str, Any]) -> str:
        """Return slug or file id from a Filester upload JSON body."""
        slug = str(raw.get("slug") or "").strip()
        if slug:
            return slug
        file_id = raw.get("file_id")
        if file_id is not None and str(file_id).strip():
            return str(file_id).strip()
        data = raw.get("data")
        if isinstance(data, dict):
            slug = str(data.get("slug") or "").strip()
            if slug:
                return slug
            fid = data.get("id")
            if fid is not None and str(fid).strip():
                return str(fid).strip()
            uuid_val = str(data.get("uuid") or "").strip()
            if uuid_val:
                return uuid_val
        return ""

    def move_files(self, file_identifiers: list[str], folder_id: str) -> dict[str, Any]:
        """Move files into ``folder_id`` via POST /api/v1/files/move (bulk)."""
        ids = [str(item).strip() for item in file_identifiers if str(item).strip()]
        if not ids:
            raise ValueError("no file identifiers to move")
        dest = (folder_id or "").strip()
        if not dest:
            raise ValueError("destination folder id required")

        data = self._request(
            "POST",
            "/api/v1/files/move",
            json={"files": ids, "folder": dest},
        )
        if data.get("success") is False:
            raise RuntimeError(f"Filester move failed: {data}")
        block = data.get("data")
        return block if isinstance(block, dict) else data

    def list_folder_files(self, folder_id: str) -> list[dict[str, Any]]:
        """List files in a folder via GET /api/v1/folder/{identifier}/files."""
        fid = (folder_id or "").strip()
        if not fid:
            return []
        data = self._request("GET", f"/api/v1/folder/{fid}/files")
        if data.get("success") is False:
            return []
        rows = data.get("data")
        return rows if isinstance(rows, list) else []

    def _recover_upload_response(
        self,
        filename: str,
        expected_size: int,
        folder_id: str | None,
    ) -> dict[str, Any] | None:
        """Find a folder file matching name + size when the upload POST response was lost."""
        fid = (folder_id or "").strip()
        if not fid or not filename:
            return None
        for row in self.list_folder_files(fid):
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or row.get("filename") or "").strip()
            if name != filename:
                continue
            size = int(row.get("size") or row.get("filesize") or 0)
            if expected_size and size != expected_size:
                continue
            slug = str(
                row.get("slug")
                or row.get("identifier")
                or row.get("id")
                or ""
            ).strip()
            if slug:
                return {"slug": slug, "data": row}
        return None

    @staticmethod
    def _response_lost_after_full_upload(
        exc: BaseException,
        reader: Any,
        total_size: int,
    ) -> bool:
        if reader is None or getattr(reader, "bytes_sent", 0) < total_size:
            return False
        if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
            return True
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500:
            return True
        return False

    @staticmethod
    def _parse_folder_from_create(data: dict[str, Any]) -> FilesterFolder | None:
        block = data.get("data")
        if not isinstance(block, dict):
            return None
        nested = block.get("folder")
        if isinstance(nested, dict):
            folder = FilesterClient._parse_folder(nested)
            if folder:
                return folder
        identifier = str(block.get("identifier") or "")
        if identifier:
            nested_name = nested.get("name") if isinstance(nested, dict) else ""
            folder_name = str(block.get("name") or nested_name or "")
            return FilesterFolder(
                identifier=identifier,
                name=folder_name or identifier,
                parent_identifier=FilesterClient._parse_parent_identifier(block),
            )
        return FilesterClient._parse_folder(block)

    def upload_file(
        self,
        file_path: str | Path,
        folder_id: str | None = None,
        *,
        on_progress: Callable[[int, int], None] | None = None,
        on_log: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(str(file_path))

        headers: dict[str, str] = {}
        if folder_id:
            headers["X-Folder-ID"] = folder_id

        total_size = file_path.stat().st_size
        upload_url = f"{self.api_base}/api/v1/upload"

        def emit(line: str) -> None:
            logger.info("%s", line)
            if on_log:
                on_log(line)

        emit(
            f"[Filester] upload {file_path.name} ({format_size(total_size)}) -> {upload_url}"
        )
        last_log_at = 0.0
        started_at = time.time()

        def report_progress(done: int, total: int) -> None:
            nonlocal last_log_at
            now = time.time()
            if now - last_log_at < 1.0:
                return
            last_log_at = now
            elapsed = now - started_at
            speed = done / elapsed if elapsed > 0 else 0.0
            pct = (done / total) * 100 if total else 0.0
            remaining = ((total - done) / speed) if speed > 0 else 0.0
            logger.info(
                "%s: %.1f%%  %s/%s  %s/s  ETA %ds",
                file_path.name,
                pct,
                format_size(done),
                format_size(total),
                format_size(speed),
                int(remaining),
            )

        for attempt in range(self.max_retries + 1):
            reader: _ProgressReader | None = None
            try:
                with open(file_path, "rb") as raw_fh:
                    fh: Any = raw_fh
                    if on_progress or on_log or total_size > 0:
                        def progress_hook(done: int, total: int) -> None:
                            report_progress(done, total)
                            if on_progress:
                                on_progress(done, total)

                        reader = _ProgressReader(
                            raw_fh,
                            total_size,
                            progress_hook,
                            chunk_bytes=self._upload_chunk_bytes,
                            throttle_kbps=self._upload_throttle_kbps,
                        )
                        fh = reader
                    files = {"file": (file_path.name, fh, "application/octet-stream")}
                    resp = self._client.post("/api/v1/upload", files=files, headers=headers)
                if resp.status_code == 429:
                    emit(
                        f"[Filester] attempt {attempt + 1} rate limited for {file_path.name}"
                    )
                    time.sleep(self.retry_delay * (2 ** attempt))
                    continue
                resp.raise_for_status()
                result = resp.json()
                slug = result.get("slug")
                if not slug:
                    data = result.get("data")
                    if isinstance(data, dict):
                        slug = data.get("slug")
                if slug:
                    emit(f"[Filester] {file_path.name} DONE -> https://filester.me/d/{slug}")
                else:
                    emit(f"[Filester] {file_path.name} DONE")
                return result
            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.NetworkError) as exc:
                if self._response_lost_after_full_upload(exc, reader, total_size):
                    emit(
                        f"[Filester] {file_path.name}: all {format_size(total_size)} sent "
                        f"but API response failed ({exc}); not re-uploading"
                    )
                    recovered = self._recover_upload_response(
                        file_path.name,
                        total_size,
                        folder_id,
                    )
                    if recovered:
                        slug = self.file_identifier_from_response(recovered)
                        if slug:
                            emit(
                                f"[Filester] {file_path.name} recovered -> "
                                f"https://filester.me/d/{slug}"
                            )
                        else:
                            emit(f"[Filester] {file_path.name} recovered from folder listing")
                        return recovered
                    raise RuntimeError(
                        f"Upload of {file_path.name} may exist on Filester but the API "
                        f"response was lost; refusing to re-upload (would duplicate). "
                        f"Check folder {folder_id or 'root'} manually."
                    ) from exc
                if attempt < self.max_retries:
                    emit(f"[Filester] attempt {attempt + 1} failed: {exc}")
                    self.reset_connections()
                    time.sleep(self.retry_delay)
                    continue
                raise
        raise RuntimeError(f"Upload failed after retries: {file_path}")

    def upload_folder_thumbnail(
        self,
        folder_identifier: str,
        image_path: str | Path,
    ) -> dict[str, Any]:
        """Set a folder thumbnail via POST /api/v1/folder/thumbnail."""
        path = Path(image_path)
        if not path.is_file():
            raise FileNotFoundError(str(path))
        size = path.stat().st_size
        if size > 5 * 1024 * 1024:
            raise ValueError(f"Thumbnail too large ({size} bytes; max 5 MB)")

        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        folder_id = (folder_identifier or "").strip()
        if not folder_id:
            raise ValueError("folder identifier is required")

        with path.open("rb") as fh:
            files = {
                "thumbnail": (path.name, fh, content_type),
            }
            data = {"folder": folder_id}
            resp = self._client.post("/api/v1/folder/thumbnail", files=files, data=data)
        if resp.status_code == 429:
            raise httpx.HTTPStatusError(
                "rate limited",
                request=resp.request,
                response=resp,
            )
        resp.raise_for_status()
        if resp.content:
            result = resp.json()
            return result if isinstance(result, dict) else {}
        return {}

    @staticmethod
    def _response_api_error(body: dict[str, Any]) -> str:
        err = body.get("error")
        if isinstance(err, str) and err.strip():
            return err.strip()
        if body.get("success") is False:
            msg = body.get("message")
            if isinstance(msg, str) and msg.strip():
                return msg.strip()
        return ""

    @staticmethod
    def _response_status(body: dict[str, Any]) -> str:
        raw = body.get("status")
        if isinstance(raw, str) and raw.strip():
            return raw.strip().lower()
        data = body.get("data")
        if isinstance(data, dict):
            nested = data.get("status")
            if isinstance(nested, str) and nested.strip():
                return nested.strip().lower()
        return ""

    @staticmethod
    def _response_file_block(body: dict[str, Any]) -> dict[str, Any]:
        data = body.get("data")
        if isinstance(data, dict):
            nested = data.get("file")
            if isinstance(nested, dict):
                return nested
            return data
        return body if isinstance(body, dict) else {}

    def _verify_timeout(self) -> httpx.Timeout:
        sec = float(self._verify_upload_request_timeout_sec)
        return httpx.Timeout(sec, connect=10.0, write=sec)

    def _verify_file_detail(
        self,
        slug: str,
        expected_size: int,
        *,
        verify_timeout: httpx.Timeout,
        on_log: Callable[[str], None] | None = None,
    ) -> tuple[bool, int]:
        """Return (ok, actual_size) using GET /api/v1/file/{slug}."""
        detail = self._api_request_once(
            "GET",
            f"/api/v1/file/{slug}",
            timeout=verify_timeout,
        )
        file_data = self._response_file_block(detail)
        actual_size = int(file_data.get("size") or file_data.get("filesize") or 0)
        if actual_size <= 0:
            return False, 0
        if expected_size and actual_size != expected_size:
            if self._verify_upload_strict:
                return False, actual_size
            return True, actual_size
        return True, actual_size

    def verify_upload(
        self,
        slug: str,
        expected_size: int,
        *,
        on_log: Callable[[str], None] | None = None,
    ) -> bool:
        """Confirm the uploaded file is visible on Filester before advancing to the next part.

        Uses ``GET /api/v1/file/{slug}`` (reliable). The ``/upload/status`` endpoint is
        optional and skipped when it errors — Filester has been returning ``Database error``
        there while the file API still works.

        When ``verify_upload_strict`` is false (default), unreachable APIs do not block
        the pipeline — the successful upload POST + slug is trusted instead.
        """
        slug = (slug or "").strip()
        if not slug:
            return False

        def emit(line: str) -> None:
            logger.info("%s", line)
            if on_log:
                on_log(line)

        if not self._verify_upload_enabled:
            emit(f"[Filester] verify {slug}: skipped (FILESTER_VERIFY_UPLOAD=false)")
            return True

        verify_timeout = self._verify_timeout()

        def trust_upload(reason: str) -> bool:
            if self._verify_upload_strict:
                emit(f"[Filester] verify {slug}: failed ({reason})")
                return False
            emit(
                f"[Filester] verify {slug}: skipped ({reason}); "
                "trust upload response and continuing"
            )
            return True

        # Optional status probe — fall back to file API when broken (common during outages).
        try:
            status_body = self._api_request_once(
                "GET",
                "/api/v1/upload/status",
                params={"slug": slug},
                timeout=verify_timeout,
            )
            api_err = self._response_api_error(status_body)
            if api_err:
                emit(
                    f"[Filester] verify {slug}: upload/status unavailable ({api_err}); "
                    "using file API"
                )
            elif self._response_status(status_body) in ("failed", "error", "cancelled"):
                emit(
                    f"[Filester] verify {slug}: "
                    f"status={self._response_status(status_body)}"
                )
                return False
            elif self._response_status(status_body) == "completed":
                ok, actual_size = self._verify_file_detail(
                    slug,
                    expected_size,
                    verify_timeout=verify_timeout,
                )
                if ok:
                    emit(f"[Filester] verify {slug}: OK ({format_size(actual_size)})")
                    return True
        except httpx.TimeoutException as exc:
            emit(
                f"[Filester] verify {slug}: upload/status timed out ({exc}); using file API"
            )
        except httpx.HTTPError as exc:
            emit(f"[Filester] verify {slug}: upload/status error ({exc}); using file API")
        except Exception as exc:
            logger.warning("Upload status check failed for %s: %s", slug, exc)

        deadline = time.time() + self._verify_upload_max_wait_sec
        while time.time() < deadline:
            try:
                ok, actual_size = self._verify_file_detail(
                    slug,
                    expected_size,
                    verify_timeout=verify_timeout,
                )
                if ok and actual_size > 0:
                    if expected_size and actual_size != expected_size:
                        emit(
                            f"[Filester] verify {slug}: size mismatch "
                            f"(expected {expected_size}, got {actual_size}); trusting upload"
                        )
                    else:
                        emit(f"[Filester] verify {slug}: OK ({format_size(actual_size)})")
                    return True
            except httpx.TimeoutException:
                emit(f"[Filester] verify {slug}: file API slow, retrying…")
            except httpx.HTTPError as exc:
                emit(f"[Filester] verify {slug}: file API error ({exc}), retrying…")
            except Exception as exc:
                logger.warning("File detail check failed for %s: %s", slug, exc)
            emit(f"[Filester] verify {slug}: waiting for file to appear on Filester…")
            time.sleep(self._verify_upload_poll_sec)

        return trust_upload(
            f"file not confirmed after {self._verify_upload_max_wait_sec}s"
        )


class _ProgressReader:
    """File-like wrapper that reports bytes read during upload."""

    def __init__(
        self,
        file_obj: Any,
        total_size: int,
        on_progress: Callable[[int, int], None],
        *,
        chunk_bytes: int = 1024 * 1024,
        throttle_kbps: int = 0,
    ) -> None:
        self._file_obj = file_obj
        self._total_size = total_size
        self._on_progress = on_progress
        self._chunk_bytes = max(64 * 1024, chunk_bytes)
        self._throttle_kbps = max(0, throttle_kbps)
        self._done = 0

    @property
    def bytes_sent(self) -> int:
        return self._done

    def read(self, size: int = -1) -> bytes:
        if size < 0 or size > self._chunk_bytes:
            size = self._chunk_bytes
        chunk = self._file_obj.read(size)
        if chunk:
            self._done += len(chunk)
            self._on_progress(self._done, self._total_size)
            if self._throttle_kbps > 0:
                time.sleep(len(chunk) / (self._throttle_kbps * 1024))
        return chunk

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        chunk = self.read(self._chunk_bytes)
        if not chunk:
            raise StopIteration
        return chunk
