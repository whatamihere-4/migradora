"""Resolve Real-Debrid panel links to CDN download URLs."""

from __future__ import annotations

import json
import re
import time
import unicodedata
from typing import Callable
from urllib.parse import urlparse, urlunparse

import httpx

from migradora.config import Settings
from migradora.interrupt import interruptible_sleep

_PAGE_LIMIT = 5000

_PANEL_LINK_RE = re.compile(
    r"^https?://(?:www\.)?real-debrid\.com/d/([A-Za-z0-9]+)/?(?:\?.*)?$",
    re.IGNORECASE,
)

_CDN_HOST_RE = re.compile(
    r"(?:^|\.)("
    r"download\.real-debrid\.(?:com|cloud)"
    r"|(?:f)?cdn\.real-debrid\.com"
    r"|rdeb\.io"
    r")$",
    re.IGNORECASE,
)


class RealDebridError(RuntimeError):
    """Real-Debrid API or link-resolution failure."""


def is_panel_link(url: str) -> bool:
    return bool(_PANEL_LINK_RE.match((url or "").strip()))


def is_cdn_link(url: str) -> bool:
    raw = (url or "").strip()
    if not raw:
        return False
    host = (urlparse(raw).hostname or "").lower()
    if not host:
        return False
    return bool(_CDN_HOST_RE.search(host))


def is_realdebrid_url(url: str | None) -> bool:
    raw = (url or "").strip()
    if not raw:
        return False
    return is_panel_link(raw) or is_cdn_link(raw)


def realdebrid_jobs_sql_clause() -> str:
    """SQL WHERE fragment: job has a Real-Debrid panel or CDN URL on download_link or gofile_url."""
    link_match = (
        "(download_link LIKE '%real-debrid.%' OR download_link LIKE '%rdeb.io%'"
        " OR gofile_url LIKE '%real-debrid.%' OR gofile_url LIKE '%rdeb.io%')"
    )
    return link_match


def needs_resolution(url: str) -> bool:
    return is_panel_link(url) and not is_cdn_link(url)


def _normalize_cdn_host(host: str) -> str:
    h = (host or "").strip().lower()
    if not h:
        return ""
    if "real-debrid" in h or h.endswith(".rdeb.io"):
        return h
    return f"{h}.download.real-debrid.com"


def preferred_cdn_hosts(settings: Settings) -> list[str]:
    raw = (settings.real_debrid_preferred_cdn or "").strip()
    if not raw:
        return []
    parts = re.split(r"[,;\s]+", raw)
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        norm = _normalize_cdn_host(part)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def apply_preferred_cdn(
    url: str,
    settings: Settings,
    *,
    on_log: Callable[[str], None] | None = None,
) -> str:
    raw = (url or "").strip()
    if not raw or not is_cdn_link(raw):
        return raw

    prefs = preferred_cdn_hosts(settings)
    if not prefs:
        return raw

    parsed = urlparse(raw)
    orig = (parsed.hostname or "").lower()
    pref = prefs[0]
    if orig == pref:
        return raw

    netloc = pref
    if parsed.port:
        netloc = f"{pref}:{parsed.port}"
    rewritten = urlunparse(parsed._replace(netloc=netloc))
    _log(f"[RD] CDN host {orig} → {pref}", on_log)
    return rewritten


def _log(msg: str, on_log: Callable[[str], None] | None) -> None:
    if on_log:
        on_log(msg)


class RealDebridClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = httpx.Client(
            timeout=httpx.Timeout(
                connect=settings.real_debrid_connect_timeout_sec,
                read=settings.real_debrid_read_timeout_sec,
                write=settings.real_debrid_read_timeout_sec,
                pool=settings.real_debrid_connect_timeout_sec,
            ),
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> RealDebridClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _api_get_list(self, path: str) -> list[dict]:
        token = (self.settings.real_debrid_api_token or "").strip()
        if not token:
            raise RealDebridError("REAL_DEBRID_API_TOKEN is not set")

        base = self.settings.real_debrid_api_base.rstrip("/")
        out: list[dict] = []
        page = 1
        while True:
            try:
                resp = self._client.get(
                    f"{base}{path}",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"page": page, "limit": _PAGE_LIMIT},
                )
            except httpx.HTTPError as exc:
                raise RealDebridError(f"GET {path} failed: {exc}") from exc

            if resp.status_code == 401:
                raise RealDebridError("Real-Debrid API rejected the token (401)")
            if resp.status_code == 403:
                raise RealDebridError("Real-Debrid API forbidden (403)")
            if not resp.is_success:
                raise RealDebridError(f"GET {path} HTTP {resp.status_code}: {(resp.text or '')[:200]}")

            if not resp.content:
                break
            try:
                batch = resp.json()
            except json.JSONDecodeError as exc:
                raise RealDebridError(f"GET {path} returned non-JSON") from exc
            if not isinstance(batch, list):
                raise RealDebridError(f"GET {path} expected JSON array")
            if not batch:
                break
            out.extend([x for x in batch if isinstance(x, dict)])
            total = resp.headers.get("X-Total-Count")
            if total is not None:
                try:
                    if len(out) >= int(total):
                        break
                except ValueError:
                    pass
            if len(batch) < _PAGE_LIMIT:
                break
            page += 1
            time.sleep(0.15)
        return out

    def list_torrents(self) -> list[dict]:
        return self._api_get_list("/torrents")

    def torrent_info(self, torrent_id: str) -> dict:
        token = (self.settings.real_debrid_api_token or "").strip()
        if not token:
            raise RealDebridError("REAL_DEBRID_API_TOKEN is not set")
        base = self.settings.real_debrid_api_base.rstrip("/")
        try:
            resp = self._client.get(
                f"{base}/torrents/info/{torrent_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError:
            return {}
        if not resp.is_success or not resp.content:
            return {}
        try:
            data = resp.json()
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def unrestrict_link(
        self,
        link: str,
        skip_check: Callable[[], None] | None = None,
    ) -> dict:
        token = (self.settings.real_debrid_api_token or "").strip()
        if not token:
            raise RealDebridError("REAL_DEBRID_API_TOKEN is not set")

        data: dict[str, str] = {"link": link.strip()}
        if self.settings.real_debrid_remote:
            data["remote"] = "1"

        base = self.settings.real_debrid_api_base.rstrip("/")
        max_retries = max(1, self.settings.real_debrid_api_max_retries)
        retry_delay = max(5, self.settings.real_debrid_api_retry_delay_sec)
        retriable = {429, 502, 503}

        last_detail = ""
        for attempt in range(max_retries):
            try:
                resp = self._client.post(
                    f"{base}/unrestrict/link",
                    headers={"Authorization": f"Bearer {token}"},
                    data=data,
                )
            except httpx.HTTPError as exc:
                if attempt + 1 >= max_retries:
                    raise RealDebridError(f"Real-Debrid API request failed: {exc}") from exc
                interruptible_sleep(retry_delay * (attempt + 1), skip_check=skip_check)
                continue

            if resp.status_code == 401:
                raise RealDebridError("Real-Debrid API rejected the token (401)")
            if resp.status_code == 403:
                raise RealDebridError("Real-Debrid API forbidden (403) — check account status")
            if resp.status_code in retriable:
                last_detail = ""
                try:
                    body = resp.json()
                    if isinstance(body, dict):
                        last_detail = str(body.get("error") or "").strip()
                except ValueError:
                    last_detail = (resp.text or "").strip()[:300]
                if attempt + 1 < max_retries:
                    wait = retry_delay * (attempt + 1)
                    if last_detail == "hoster_unavailable":
                        wait = max(wait, 60)
                    interruptible_sleep(wait, skip_check=skip_check)
                    continue
            if not resp.is_success:
                detail = last_detail
                if not detail:
                    try:
                        body = resp.json()
                        if isinstance(body, dict):
                            detail = str(body.get("error") or "").strip()
                    except ValueError:
                        detail = (resp.text or "").strip()[:300]
                msg = f"Real-Debrid API HTTP {resp.status_code}"
                if detail:
                    msg = f"{msg}: {detail}"
                raise RealDebridError(msg)

            payload = resp.json()
            if not isinstance(payload, dict):
                raise RealDebridError("Real-Debrid API returned unexpected payload")
            return payload

        msg = "Real-Debrid API request failed after retries"
        if last_detail:
            msg = f"{msg}: {last_detail}"
        raise RealDebridError(msg)

    def resolve_download_url(
        self,
        url: str,
        *,
        on_log: Callable[[str], None] | None = None,
        skip_check: Callable[[], None] | None = None,
    ) -> str:
        raw = (url or "").strip()
        if not raw:
            return raw

        if needs_resolution(raw):
            token = (self.settings.real_debrid_api_token or "").strip()
            if not token:
                _log(
                    "[RD] Panel link detected but REAL_DEBRID_API_TOKEN is not set; "
                    "using URL as-is (CDN resolution skipped)",
                    on_log,
                )
                return apply_preferred_cdn(raw, self.settings, on_log=on_log)

            _log(f"[RD] Resolving panel link via API: {raw}", on_log)
            payload = self.unrestrict_link(raw, skip_check=skip_check)
            download = (payload.get("download") or "").strip()
            if not download:
                raise RealDebridError("Real-Debrid API returned no download URL")

            filename = (payload.get("filename") or "").strip()
            host = urlparse(download).hostname or download
            label = f"{host}/…/{filename}" if filename else host
            _log(f"[RD] CDN link: {label}", on_log)
            return apply_preferred_cdn(download, self.settings, on_log=on_log)

        return apply_preferred_cdn(raw, self.settings, on_log=on_log)

    def resolve_metadata(
        self,
        url: str,
        *,
        on_log: Callable[[str], None] | None = None,
        skip_check: Callable[[], None] | None = None,
    ) -> dict:
        """Resolve link and return download URL plus filename/size when available."""
        raw = (url or "").strip()
        filename = ""
        filesize = 0

        if needs_resolution(raw):
            if skip_check:
                skip_check()
            payload = self.unrestrict_link(raw, skip_check=skip_check)
            download = (payload.get("download") or "").strip()
            if not download:
                raise RealDebridError("Real-Debrid API returned no download URL")
            filename = (payload.get("filename") or "").strip()
            filesize = int(payload.get("filesize") or 0)
            download = apply_preferred_cdn(download, self.settings)
            return {
                "download_url": download,
                "filename": filename,
                "filesize": filesize,
                "resolved": True,
            }

        download = apply_preferred_cdn(raw, self.settings)
        return {
            "download_url": download,
            "filename": filename,
            "filesize": filesize,
            "resolved": False,
        }
