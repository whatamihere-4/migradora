"""Load Filester folder ids from JSON (same ``{id: name}`` shape as apu ``folders.json``)."""

from __future__ import annotations

import json
import re
from pathlib import Path

_FOLDER_MATCH_WS = re.compile(r"\s+")
_DELETE_MODAL_RE = re.compile(
    r"showDeleteFolderModal\(\s*'([a-f0-9]+)',\s*`([^`]+)`",
    re.IGNORECASE,
)
_CARD_NAME_RE = re.compile(
    r'data-identifier="([a-f0-9]+)"[\s\S]*?'
    r'class="folder-name-display[^"]*"[^>]*>([^<]+)<',
    re.IGNORECASE,
)


def folder_match_key(label: str) -> str:
    """Case-insensitive key with all whitespace removed (apu-style fuzzy match)."""
    return _FOLDER_MATCH_WS.sub("", str(label or "").strip().casefold())


def load_filester_folders(path: str | Path) -> dict[str, str]:
    """Read ``{folder_id: display_name, ...}`` from disk."""
    file_path = Path(path)
    if not file_path.is_file():
        return {}
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    result: dict[str, str] = {}
    for folder_id, name in data.items():
        if not isinstance(folder_id, str) or not isinstance(name, str):
            continue
        folder_id = folder_id.strip()
        name = name.strip()
        if folder_id and name:
            result[folder_id] = name
    return result


def save_filester_folders(path: str | Path, folders: dict[str, str]) -> None:
    """Write ``{folder_id: display_name, ...}`` with apu-style formatting."""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open("w", encoding="utf-8") as handle:
        json.dump(folders, handle, indent=2, sort_keys=True)
        handle.write("\n")


def resolve_folder_id(folders: dict[str, str], label: str) -> str | None:
    """Match a studio label against ``{id: name}`` (exact, then whitespace-normalized)."""
    needle = str(label or "").strip()
    if not needle or not folders:
        return None

    needle_fold = needle.casefold()
    for folder_id, folder_label in folders.items():
        cand = str(folder_label or "").strip()
        if cand.casefold() == needle_fold:
            return folder_id

    norm_needle = folder_match_key(needle)
    if not norm_needle:
        return None
    for folder_id, folder_label in folders.items():
        if folder_match_key(folder_label) == norm_needle:
            return folder_id
    return None


def parse_html_folder_cards(html: str) -> dict[str, str]:
    """Extract ``{id: name}`` from Filester manager folder-card HTML."""
    folders: dict[str, str] = {}
    for match in _DELETE_MODAL_RE.finditer(html):
        folder_id, name = match.group(1).strip(), match.group(2).strip()
        if folder_id and name:
            folders[folder_id] = name
    if folders:
        return folders
    for match in _CARD_NAME_RE.finditer(html):
        folder_id, name = match.group(1).strip(), match.group(2).strip()
        if folder_id and name:
            folders[folder_id] = name
    return folders
