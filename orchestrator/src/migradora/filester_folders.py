"""Create Filester folder paths that mirror Gofile layout."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from migradora.config import Settings
from migradora.filester_client import FilesterClient, FolderIndex
from migradora.queue.manager import QueueManager

logger = logging.getLogger("migradora.filester_folders")


@dataclass
class CachedFolder:
    identifier: str
    db_id: int | None = None


def _full_path(settings: Settings, gofile_folder_path: str) -> str:
    path = gofile_folder_path.strip().strip("/")
    root = settings.filester_root_folder_name.strip()
    if not root:
        return path
    if not path:
        return root
    if path == root or path.startswith(f"{root}/"):
        return path
    return f"{root}/{path}"


def _is_flat_fallback_name(folder_name: str) -> bool:
    return " / " in folder_name


def _resolve_cached(
    identifier: str,
    index: FolderIndex,
) -> CachedFolder:
    folder = index.by_identifier(identifier)
    if folder:
        return CachedFolder(identifier=folder.identifier, db_id=folder.db_id)
    return CachedFolder(identifier=identifier)


def _seed_configured_root(
    settings: Settings,
    cache: dict[str, CachedFolder],
) -> None:
    root_name = settings.filester_root_folder_name.strip()
    root_id = settings.filester_root_folder_id.strip()
    if root_name and root_id and root_name not in cache:
        cache[root_name] = CachedFolder(identifier=root_id, db_id=None)
        logger.info("Using configured Filester root folder %r -> %s", root_name, root_id)


def ensure_filester_folder_path(
    client: FilesterClient,
    queue: QueueManager,
    settings: Settings,
    gofile_folder_path: str,
    cache: dict[str, CachedFolder],
) -> str:
    """Mirror a Gofile folder path like ``VR/Studio1`` as nested Filester folders."""
    _seed_configured_root(settings, cache)
    path = _full_path(settings, gofile_folder_path)
    if not path:
        folder_id = _ensure_root(client, queue, settings, cache)
        if not folder_id:
            raise RuntimeError("Failed to resolve Filester root folder")
        return folder_id

    if path in cache:
        return cache[path].identifier

    index = client.folder_index()
    parent_db_id: int | None = None
    parent_identifier: str | None = None
    last_identifier: str | None = None
    accumulated = ""

    for segment in path.split("/"):
        accumulated = f"{accumulated}/{segment}".lstrip("/")

        if accumulated in cache:
            cached = cache[accumulated]
            parent_db_id = cached.db_id
            parent_identifier = cached.identifier
            last_identifier = cached.identifier
            continue

        mapping = queue.get_folder_mapping_record(accumulated)
        if mapping and not _is_flat_fallback_name(mapping[1]):
            cached = _resolve_cached(mapping[0], index)
            cache[accumulated] = cached
            parent_db_id = cached.db_id
            parent_identifier = cached.identifier
            last_identifier = cached.identifier
            continue

        existing = index.find_child(
            segment,
            parent_db_id=parent_db_id,
            parent_identifier=parent_identifier,
        )
        if existing:
            cached = CachedFolder(identifier=existing.identifier, db_id=existing.db_id)
            cache[accumulated] = cached
            queue.save_folder_mapping(accumulated, existing.identifier, segment)
            parent_db_id = cached.db_id
            parent_identifier = cached.identifier
            last_identifier = cached.identifier
            continue

        created = client.create_folder(
            segment,
            parent_db_id=parent_db_id,
            parent_identifier=parent_identifier,
        )
        cached = CachedFolder(identifier=created.identifier, db_id=created.db_id)
        cache[accumulated] = cached
        queue.save_folder_mapping(accumulated, created.identifier, segment)
        parent_db_id = created.db_id
        parent_identifier = created.identifier
        last_identifier = created.identifier

    if not last_identifier:
        raise RuntimeError(f"Failed to resolve Filester folder path: {path}")
    logger.info("Resolved Filester upload folder for %r -> %s", path, last_identifier)
    return last_identifier


def _ensure_root(
    client: FilesterClient,
    queue: QueueManager,
    settings: Settings,
    cache: dict[str, CachedFolder],
) -> str | None:
    key = "__root__"
    if key in cache:
        return cache[key].identifier
    mapping = queue.get_folder_mapping_record(key)
    if mapping and not _is_flat_fallback_name(mapping[1]):
        cached = _resolve_cached(mapping[0], client.folder_index())
        cache[key] = cached
        return cached.identifier
    name = settings.filester_root_folder_name or "gofile-mirror"
    root_id = settings.filester_root_folder_id.strip()
    if root_id:
        cached = CachedFolder(identifier=root_id, db_id=None)
        cache[key] = cached
        queue.save_folder_mapping(key, root_id, name)
        return root_id
    created = client.create_folder(name)
    cached = CachedFolder(identifier=created.identifier, db_id=created.db_id)
    cache[key] = cached
    queue.save_folder_mapping(key, created.identifier, name)
    return created.identifier
