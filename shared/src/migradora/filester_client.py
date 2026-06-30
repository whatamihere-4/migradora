"""Filester REST API client."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

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
        self._nested_folder_cache: dict[tuple[str, str], FilesterFolder] = {}

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
    ) -> dict[str, Any]:
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(str(file_path))

        headers: dict[str, str] = {}
        if folder_id:
            headers["X-Folder-ID"] = folder_id

        total_size = file_path.stat().st_size

        for attempt in range(self.max_retries + 1):
            try:
                with open(file_path, "rb") as raw_fh:
                    fh: Any = raw_fh
                    if on_progress:
                        fh = _ProgressReader(raw_fh, total_size, on_progress)
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


class _ProgressReader:
    """File-like wrapper that reports bytes read during upload."""

    def __init__(
        self,
        file_obj: Any,
        total_size: int,
        on_progress: Callable[[int, int], None],
    ) -> None:
        self._file_obj = file_obj
        self._total_size = total_size
        self._on_progress = on_progress
        self._done = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._file_obj.read(size)
        if chunk:
            self._done += len(chunk)
            self._on_progress(self._done, self._total_size)
        return chunk

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        chunk = self.read(65536)
        if not chunk:
            raise StopIteration
        return chunk
