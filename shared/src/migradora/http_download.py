"""Resumable HTTP download helper (shared by Gofile CDN and Real-Debrid)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

import httpx

from migradora.interrupt import interruptible_sleep

logger = logging.getLogger("migradora.http_download")


def _content_length(resp: httpx.Response, fallback: int | None = None) -> int | None:
    raw = resp.headers.get("content-length")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    content_range = resp.headers.get("content-range")
    if content_range and "/" in content_range:
        total = content_range.rsplit("/", 1)[-1].strip()
        if total != "*":
            try:
                return int(total)
            except ValueError:
                pass
    return fallback


def download_url(
    client: httpx.Client,
    url: str,
    dest_path: str | Path,
    *,
    expected_size: int | None = None,
    throttle_kbps: int = 0,
    on_progress: Callable[[int, int | None], None] | None = None,
    skip_check: Callable[[], None] | None = None,
) -> str:
    """Download with resume support (.part temp file)."""
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    offset = part.stat().st_size if part.exists() else 0
    if offset and expected_size and offset == expected_size:
        part.rename(dest)
        return str(dest)

    headers: dict[str, str] = {}
    if offset:
        headers["Range"] = f"bytes={offset}-"
        logger.info("Resuming download at byte %d -> %s", offset, part.stem)
    mode = "ab" if offset else "wb"

    if skip_check:
        skip_check()
    with client.stream("GET", url, headers=headers, follow_redirects=True) as resp:
        if resp.status_code == 416:
            if expected_size and offset == expected_size:
                return str(dest)
            raise RuntimeError(f"Download range not satisfiable at offset {offset}")
        if resp.status_code not in (200, 206):
            resp.raise_for_status()
        total_bytes = expected_size or _content_length(resp)
        if on_progress:
            on_progress(offset, total_bytes)
        with part.open(mode) as fh:
            for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                if skip_check:
                    skip_check()
                fh.write(chunk)
                if on_progress:
                    on_progress(part.stat().st_size, total_bytes)
                if throttle_kbps > 0:
                    interruptible_sleep(
                        len(chunk) / (throttle_kbps * 1024),
                        skip_check=skip_check,
                    )

    part.rename(dest)
    size = dest.stat().st_size
    if expected_size and size != expected_size:
        logger.warning(
            "Download size %d != expected %d for %s",
            size,
            expected_size,
            dest.name,
        )
    return str(dest)
