"""Create Filester folder paths that mirror Gofile layout."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from migradora.config import Settings
from migradora.filester_client import FilesterClient
from migradora.filester_folders_file import resolve_folder_id
from migradora.queue.manager import QueueManager

logger = logging.getLogger("migradora.filester_folders")


@dataclass
class CachedFolder:
    identifier: str


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


def _mapped_folder_id(settings: Settings, accumulated: str) -> str | None:
    """Look up a Filester folder id from ``filester-folders.json`` (apu-style ``{id: name}``)."""
    key = accumulated.strip().strip("/")
    if not key or not settings.filester_folders:
        return None
    folders = settings.filester_folders
    for label in (key.rsplit("/", 1)[-1], key):
        folder_id = resolve_folder_id(folders, label)
        if folder_id:
            return folder_id
    return None


def _apply_folder_id(
    *,
    accumulated: str,
    segment: str,
    folder_id: str,
    cache: dict[str, CachedFolder],
    queue: QueueManager,
    source: str,
) -> CachedFolder:
    cached = CachedFolder(identifier=folder_id)
    cache[accumulated] = cached
    queue.save_folder_mapping(accumulated, folder_id, segment)
    logger.info("Using %s folder %r -> %s", source, accumulated, folder_id)
    return cached


def _seed_root_folder(
    settings: Settings,
    cache: dict[str, CachedFolder],
) -> None:
    root_name = settings.filester_root_folder_name.strip()
    root_id = settings.filester_root_folder_id.strip()
    if root_name and root_id and root_name not in cache:
        cache[root_name] = CachedFolder(identifier=root_id)


def ensure_filester_folder_path(
    client: FilesterClient,
    queue: QueueManager,
    settings: Settings,
    gofile_folder_path: str,
    cache: dict[str, CachedFolder],
) -> str:
    """
    Mirror a Gofile path like ``VR/CzechVR`` as nested Filester folders.

    Set ``FILESTER_FOLDERS_FILE`` to a JSON map of ``{folder_id: name}`` (same as
    apu ``folders.json``). Set ``FILESTER_AUTO_CREATE_FOLDERS=false`` to disable
    API folder creation entirely.
    """
    _seed_root_folder(settings, cache)
    path = _full_path(settings, gofile_folder_path)
    if not path:
        return _ensure_root_folder(client, queue, settings, cache)

    if path in cache:
        return cache[path].identifier

    parent_identifier: str | None = None
    last_identifier: str | None = None
    accumulated = ""

    for segment in path.split("/"):
        accumulated = f"{accumulated}/{segment}".lstrip("/")

        if accumulated in cache:
            parent_identifier = cache[accumulated].identifier
            last_identifier = parent_identifier
            continue

        mapped_id = _mapped_folder_id(settings, accumulated)
        if mapped_id:
            cached = _apply_folder_id(
                accumulated=accumulated,
                segment=segment,
                folder_id=mapped_id,
                cache=cache,
                queue=queue,
                source="mapped",
            )
            parent_identifier = cached.identifier
            last_identifier = cached.identifier
            continue

        mapping = queue.get_folder_mapping_record(accumulated)
        if mapping and not _is_flat_fallback_name(mapping[1]):
            if parent_identifier and not client.folder_is_under_parent(
                mapping[0], parent_identifier=parent_identifier
            ):
                logger.warning(
                    "Ignoring stale folder mapping for %r -> %s (not under %s)",
                    accumulated,
                    mapping[0],
                    parent_identifier,
                )
            else:
                cached = CachedFolder(identifier=mapping[0])
                cache[accumulated] = cached
                parent_identifier = cached.identifier
                last_identifier = cached.identifier
                continue

        is_root_segment = (
            segment == settings.filester_root_folder_name.strip()
            and parent_identifier is None
        )
        if is_root_segment and settings.filester_root_folder_id.strip():
            folder_id = settings.filester_root_folder_id.strip()
            cached = CachedFolder(identifier=folder_id)
            cache[accumulated] = cached
            queue.save_folder_mapping(accumulated, folder_id, segment)
            parent_identifier = folder_id
            last_identifier = folder_id
            continue

        if not settings.filester_auto_create_folders:
            raise RuntimeError(
                f"No Filester folder mapping for gofile path {accumulated!r}. "
                f"Add it to {settings.filester_folders_file or 'filester-folders.json'}, "
                f"or set FILESTER_AUTO_CREATE_FOLDERS=true."
            )

        existing = client.find_folder(
            segment,
            parent_identifier=parent_identifier,
        )
        if existing:
            cached = CachedFolder(identifier=existing.identifier)
            cache[accumulated] = cached
            queue.save_folder_mapping(accumulated, existing.identifier, segment)
            logger.info(
                "Found Filester folder %r under parent %s -> %s",
                segment,
                parent_identifier or "root",
                existing.identifier,
            )
            parent_identifier = existing.identifier
            last_identifier = existing.identifier
            continue

        try:
            created = client.create_folder(
                segment,
                parent_identifier=parent_identifier,
            )
        except RuntimeError as exc:
            if parent_identifier and "top-level" in str(exc).lower():
                raise
            if parent_identifier and "409" in str(exc):
                root_dup = client.find_folder(segment)
                if root_dup:
                    raise RuntimeError(
                        f"Folder {segment!r} already exists at the Filester account root "
                        f"({root_dup.identifier}). Delete or move that top-level folder on "
                        f"Filester so migradora can create {segment!r} inside "
                        f"{settings.filester_root_folder_name or 'VR'}/."
                    ) from exc
            raise

        cached = CachedFolder(identifier=created.identifier)
        cache[accumulated] = cached
        queue.save_folder_mapping(accumulated, created.identifier, segment)
        parent_identifier = created.identifier
        last_identifier = created.identifier
        logger.info(
            "Ensured Filester folder %r -> %s (gofile path %r)",
            segment,
            created.identifier,
            accumulated,
        )

    if not last_identifier:
        raise RuntimeError(f"Failed to resolve Filester folder path: {path}")
    return last_identifier


def _ensure_root_folder(
    client: FilesterClient,
    queue: QueueManager,
    settings: Settings,
    cache: dict[str, CachedFolder],
) -> str:
    key = "__root__"
    if key in cache:
        return cache[key].identifier

    mapping = queue.get_folder_mapping_record(key)
    if mapping and not _is_flat_fallback_name(mapping[1]):
        cached = CachedFolder(identifier=mapping[0])
        cache[key] = cached
        return cached.identifier

    root_id = settings.filester_root_folder_id.strip()
    name = settings.filester_root_folder_name.strip() or "gofile-mirror"
    if root_id:
        cache[key] = CachedFolder(identifier=root_id)
        queue.save_folder_mapping(key, root_id, name)
        return root_id

    existing = client.find_folder(name)
    if existing:
        cache[key] = CachedFolder(identifier=existing.identifier)
        queue.save_folder_mapping(key, existing.identifier, name)
        return existing.identifier

    created = client.create_folder(name)
    cache[key] = CachedFolder(identifier=created.identifier)
    queue.save_folder_mapping(key, created.identifier, name)
    return created.identifier
