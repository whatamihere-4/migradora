"""Match migradora job filenames to Real-Debrid torrent list entries."""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from migradora.realdebrid_client import RealDebridClient


def norm_filename(name: str) -> str:
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = unicodedata.normalize("NFKC", base).strip().lower()
    base = re.sub(r"\s+", " ", base)
    return base


def link_and_size_from_torrent_info(info: dict, filename: str) -> tuple[str, int]:
    if not info:
        return "", 0
    target = norm_filename(filename)
    files = info.get("files") or []
    links = info.get("links") or []
    selected = [f for f in files if int(f.get("selected") or 0)]
    if not selected:
        selected = files

    for idx, f in enumerate(selected):
        path = (f.get("path") or "").strip()
        if norm_filename(path) == target and idx < len(links):
            link = (links[idx] or "").strip()
            size = int(f.get("bytes") or 0)
            return link, size

    if norm_filename(info.get("filename") or "") == target and links:
        size = int(info.get("bytes") or 0)
        if len(selected) == 1:
            size = int(selected[0].get("bytes") or size)
        return (links[0] or "").strip(), size

    if len(selected) == 1 and links:
        return (links[0] or "").strip(), int(selected[0].get("bytes") or 0)

    if links:
        return (links[0] or "").strip(), int(info.get("bytes") or 0)
    return "", 0


@dataclass
class TorrentListEntry:
    torrent_id: str
    filename: str
    status: str
    list_link: str


def index_torrent_names(torrents: list[dict]) -> dict[str, list[TorrentListEntry]]:
    out: dict[str, list[TorrentListEntry]] = {}
    for t in torrents:
        tid = (t.get("id") or "").strip()
        name = (t.get("filename") or "").strip()
        key = norm_filename(name)
        if not key or not tid:
            continue
        links = t.get("links") or []
        list_link = (links[0] or "").strip() if links else ""
        out.setdefault(key, []).append(
            TorrentListEntry(
                torrent_id=tid,
                filename=name,
                status=(t.get("status") or ""),
                list_link=list_link,
            )
        )
    return out


@dataclass
class JobMatch:
    job_id: int
    filename: str
    url: str
    size_bytes: int
    torrent_id: str


@dataclass
class AutoMatchResult:
    matched: list[JobMatch] = field(default_factory=list)
    unmatched: list[dict[str, Any]] = field(default_factory=list)
    ambiguous: list[dict[str, Any]] = field(default_factory=list)
    torrent_info_fetches: int = 0


class TorrentLinkResolver:
    def __init__(self, client: RealDebridClient) -> None:
        self._client = client
        self._cache: dict[str, dict] = {}
        self.fetch_count = 0

    def resolve(self, entry: TorrentListEntry, job_filename: str) -> tuple[str, int]:
        if entry.list_link:
            return entry.list_link, 0
        tid = entry.torrent_id
        if tid not in self._cache:
            self._cache[tid] = self._client.torrent_info(tid)
            self.fetch_count += 1
            time.sleep(0.05)
        return link_and_size_from_torrent_info(self._cache[tid], job_filename)


def auto_match_jobs(
    client: RealDebridClient,
    jobs: list[dict[str, Any]],
) -> AutoMatchResult:
    """Match jobs to RD torrents by exact normalized torrent filename."""
    torrents = client.list_torrents()
    index = index_torrent_names(torrents)
    resolver = TorrentLinkResolver(client)
    result = AutoMatchResult()

    for job in jobs:
        job_id = int(job["id"])
        filename = (job.get("filename") or "").strip()
        key = norm_filename(filename)
        if not key:
            result.unmatched.append({"id": job_id, "filename": filename, "reason": "empty filename"})
            continue

        entries = index.get(key, [])
        if not entries:
            result.unmatched.append(
                {
                    "id": job_id,
                    "filename": filename,
                    "reason": "no torrent with matching name",
                }
            )
            continue

        downloaded = [e for e in entries if e.status == "downloaded"]
        candidates = downloaded or entries
        if len(candidates) > 1:
            result.ambiguous.append(
                {
                    "id": job_id,
                    "filename": filename,
                    "torrent_ids": [c.torrent_id for c in candidates],
                }
            )

        entry = candidates[0]
        url, size = resolver.resolve(entry, filename)
        if not url:
            result.unmatched.append(
                {
                    "id": job_id,
                    "filename": filename,
                    "reason": f"torrent {entry.torrent_id} has no link (status={entry.status})",
                }
            )
            continue

        job_size = int(job.get("size_bytes") or 0)
        result.matched.append(
            JobMatch(
                job_id=job_id,
                filename=filename,
                url=url,
                size_bytes=size or job_size,
                torrent_id=entry.torrent_id,
            )
        )

    result.torrent_info_fetches = resolver.fetch_count
    return result
