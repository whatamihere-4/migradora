"""Create Filester folder paths that mirror Gofile layout."""

from __future__ import annotations

import logging

from migradora.config import Settings
from migradora.filester_client import FilesterClient
from migradora.queue.manager import QueueManager

logger = logging.getLogger("migradora.filester_folders")


def _full_path(settings: Settings, gofile_folder_path: str) -> str:
    path = gofile_folder_path.strip().strip("/")
    root = settings.filester_root_folder_name.strip()
    if root and path:
        return f"{root}/{path}"
    if root:
        return root
    return path


def ensure_filester_folder_path(
    client: FilesterClient,
    queue: QueueManager,
    settings: Settings,
    gofile_folder_path: str,
    cache: dict[str, str],
) -> str | None:
    """
    Mirror a Gofile folder path like ``VR/Studio1`` on Filester.

    Tries nested folders (parent_id) when supported; otherwise one folder
    named ``VR / Studio1``.
    """
    path = _full_path(settings, gofile_folder_path)
    if not path:
        return _ensure_root(client, queue, settings, cache)

    if path in cache:
        return cache[path]
    existing = queue.get_folder_mapping(path)
    if existing:
        cache[path] = existing
        return existing

    segments = path.split("/")
    parent_id: str | None = None
    accumulated = ""
    try:
        for segment in segments:
            accumulated = f"{accumulated}/{segment}".lstrip("/")
            if accumulated in cache:
                parent_id = cache[accumulated]
                continue
            mapped = queue.get_folder_mapping(accumulated)
            if mapped:
                cache[accumulated] = mapped
                parent_id = mapped
                continue
            folder_id = client.create_folder(segment, parent_id=parent_id)
            queue.save_folder_mapping(accumulated, folder_id, segment)
            cache[accumulated] = folder_id
            parent_id = folder_id
        return parent_id
    except Exception as exc:
        logger.warning("Nested Filester folders failed (%s); using flat path name", exc)

    display = path.replace("/", " / ")[:100]
    folder_id = client.create_folder(display)
    queue.save_folder_mapping(path, folder_id, display)
    cache[path] = folder_id
    return folder_id


def _ensure_root(
    client: FilesterClient,
    queue: QueueManager,
    settings: Settings,
    cache: dict[str, str],
) -> str | None:
    key = "__root__"
    if key in cache:
        return cache[key]
    existing = queue.get_folder_mapping(key)
    if existing:
        cache[key] = existing
        return existing
    name = settings.filester_root_folder_name or "gofile-mirror"
    folder_id = client.create_folder(name)
    queue.save_folder_mapping(key, folder_id, name)
    cache[key] = folder_id
    return folder_id
