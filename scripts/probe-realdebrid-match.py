#!/usr/bin/env python3
"""
Probe Real-Debrid downloads + torrents for filename matches against migradora jobs.

Usage (on VPS host — recommended):
  export REAL_DEBRID_API_TOKEN=your_token
  ./scripts/probe-realdebrid-match.sh

  # Hunt one file (fast — scans torrent list, fetches info only for hits):
  ./scripts/probe-realdebrid-match.sh --search 'VRCONK_barbie_a_porn_parody_8K_180x180_3dh.mp4'

  # Slow: index every downloaded torrent's file paths (1957+ API calls):
  ./scripts/probe-realdebrid-match.sh --full-torrent-info
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any

import httpx

API_BASE = (
    os.environ.get("REAL_DEBRID_API_BASE") or "https://api.real-debrid.com/rest/1.0"
).rstrip("/")
TOKEN = (os.environ.get("REAL_DEBRID_API_TOKEN") or "").strip()
CONNECT_TO = float(os.environ.get("REAL_DEBRID_CONNECT_TIMEOUT_SEC", "15"))
READ_TO = float(os.environ.get("REAL_DEBRID_READ_TIMEOUT_SEC", "120"))
PAGE_LIMIT = 5000


def die(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def enable_ipv4_only() -> None:
    if os.environ.get("PROBE_RD_IPV4", "1").strip().lower() in ("0", "false", "no"):
        return
    _orig = socket.getaddrinfo

    def _ipv4(host: str, port: Any, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0):
        return _orig(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = _ipv4  # type: ignore[method-assign]


def norm_name(name: str) -> str:
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = unicodedata.normalize("NFKC", base).strip().lower()
    base = re.sub(r"\s+", " ", base)
    return base


class RDClient:
    def __init__(self) -> None:
        if not TOKEN:
            die("Set REAL_DEBRID_API_TOKEN in the environment")
        enable_ipv4_only()
        self._client = httpx.Client(
            base_url=API_BASE,
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=httpx.Timeout(CONNECT_TO, read=READ_TO),
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def _parse_list_response(self, path: str, resp: httpx.Response) -> list[dict]:
        if resp.status_code == 401:
            die("API token rejected (401)")
        if resp.status_code == 403:
            die("API forbidden (403) — check account status")
        if not resp.is_success:
            body = (resp.text or "").strip()[:300]
            die(f"GET {path} failed: HTTP {resp.status_code} {body}")

        if not resp.content:
            return []

        try:
            data = resp.json()
        except json.JSONDecodeError:
            body = (resp.text or "").strip()[:300]
            die(f"GET {path} returned non-JSON (HTTP {resp.status_code}): {body}")

        if data is None:
            return []
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        die(f"GET {path} expected JSON array, got {type(data).__name__}")

    def _get_list(self, path: str) -> list[dict]:
        out: list[dict] = []
        page = 1
        while True:
            try:
                resp = self._client.get(path, params={"page": page, "limit": PAGE_LIMIT})
            except httpx.HTTPError as exc:
                die(
                    f"GET {path} network error: {exc}\n"
                    "  Run on VPS host: ./scripts/probe-realdebrid-match.sh"
                )
            batch = self._parse_list_response(path, resp)
            if not batch:
                break
            out.extend(batch)
            total = resp.headers.get("X-Total-Count")
            if total is not None:
                try:
                    if len(out) >= int(total):
                        break
                except ValueError:
                    pass
            if len(batch) < PAGE_LIMIT:
                break
            page += 1
            time.sleep(0.15)
        return out

    def list_downloads(self) -> list[dict]:
        return self._get_list("/downloads")

    def list_torrents(self) -> list[dict]:
        return self._get_list("/torrents")

    def torrent_info(self, torrent_id: str) -> dict:
        try:
            resp = self._client.get(f"/torrents/info/{torrent_id}")
        except httpx.HTTPError:
            return {}
        if not resp.is_success or not resp.content:
            return {}
        try:
            data = resp.json()
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}


class LinkResolver:
    """Fetch /torrents/info only on demand (cached per torrent id)."""

    def __init__(self, rd: RDClient) -> None:
        self._rd = rd
        self._info_cache: dict[str, dict] = {}
        self._fetch_count = 0

    @property
    def fetch_count(self) -> int:
        return self._fetch_count

    def get_info(self, torrent_id: str) -> dict:
        if not torrent_id:
            return {}
        if torrent_id not in self._info_cache:
            self._info_cache[torrent_id] = self._rd.torrent_info(torrent_id)
            self._fetch_count += 1
            time.sleep(0.05)
        return self._info_cache[torrent_id]

    def link_for_filename(
        self,
        torrent_id: str,
        filename: str,
        list_link: str = "",
    ) -> str:
        if list_link:
            return list_link.strip()
        info = self.get_info(torrent_id)
        return link_from_torrent_info(info, filename)


def link_from_torrent_info(info: dict, filename: str) -> str:
    if not info:
        return ""
    target = norm_name(filename)
    files = info.get("files") or []
    links = info.get("links") or []
    selected = [f for f in files if int(f.get("selected") or 0)]
    if not selected:
        selected = files

    for idx, f in enumerate(selected):
        path = (f.get("path") or "").strip()
        if norm_name(path) == target and idx < len(links):
            return (links[idx] or "").strip()

    if norm_name(info.get("filename") or "") == target and links:
        return (links[0] or "").strip()

    if len(selected) == 1 and links:
        return (links[0] or "").strip()

    return (links[0] or "").strip() if links else ""


def load_queue_filenames(db_path: Path) -> list[dict]:
    if not db_path.is_file():
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, filename, parent_folder_path, status, last_error
        FROM files
        WHERE is_part = 0
          AND status IN ('failed', 'pending')
        ORDER BY id ASC
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def load_filename_list(path: Path) -> list[dict]:
    names = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    return [
        {"id": i + 1, "filename": n, "parent_folder_path": "", "status": "?"}
        for i, n in enumerate(names)
        if n
    ]


def index_downloads(downloads: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for item in downloads:
        fn = (item.get("filename") or "").strip()
        key = norm_name(fn)
        if not key:
            continue
        link = (item.get("download") or item.get("link") or "").strip()
        out.setdefault(key, []).append(
            {
                "source": "downloads",
                "label": fn,
                "link": link,
                "extra": f"{item.get('filesize', 0)} B",
            }
        )
    return out


def index_torrent_names(torrents: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for t in torrents:
        tid = (t.get("id") or "").strip()
        tname = (t.get("filename") or "").strip()
        key = norm_name(tname)
        if not key:
            continue
        links = t.get("links") or []
        list_link = (links[0] or "").strip() if links else ""
        out.setdefault(key, []).append(
            {
                "source": "torrents.name",
                "torrent_id": tid,
                "label": tname,
                "link": list_link,
                "extra": f"id={tid} status={t.get('status')}",
            }
        )
    return out


def build_torrent_file_index(
    rd: RDClient,
    downloaded: list[dict],
) -> dict[str, list[dict]]:
    """Slow path: GET /torrents/info for every downloaded torrent."""
    out: dict[str, list[dict]] = {}
    total = len(downloaded)
    print(f"Fetching GET /torrents/info/{{id}} for {total} downloaded torrents …")
    for i, t in enumerate(downloaded, 1):
        tid = (t.get("id") or "").strip()
        if not tid:
            continue
        info = rd.torrent_info(tid)
        files = info.get("files") or []
        links = info.get("links") or []
        selected = [f for f in files if int(f.get("selected") or 0)]
        if not selected:
            selected = files
        for idx, f in enumerate(selected):
            path = (f.get("path") or "").strip()
            key = norm_name(path)
            if not key:
                continue
            link = ""
            if idx < len(links):
                link = (links[idx] or "").strip()
            out.setdefault(key, []).append(
                {
                    "source": "torrents.file",
                    "torrent_id": tid,
                    "label": path.lstrip("/"),
                    "link": link,
                    "extra": f"torrent={info.get('filename', t.get('filename', ''))} id={tid}",
                }
            )
        if i % 25 == 0 or i == total:
            print(f"  … {i}/{total} torrent infos fetched")
        time.sleep(0.05)
    return out


def torrent_candidates_for_query(
    query: str,
    torrent_name_by: dict[str, list[dict]],
    *,
    max_substring: int = 30,
) -> list[dict]:
    q = norm_name(query)
    if not q:
        return []

    seen: set[str] = set()
    hits: list[dict] = []

    def add(entries: list[dict]) -> None:
        for entry in entries:
            tid = entry.get("torrent_id") or ""
            if tid in seen:
                continue
            seen.add(tid)
            hits.append(entry)

    add(torrent_name_by.get(q, []))

    if len(hits) < max_substring:
        for key, entries in torrent_name_by.items():
            if key == q:
                continue
            if q in key or key in q:
                add(entries)
                if len(hits) >= max_substring:
                    break

    return hits


def run_search(rd: RDClient, query: str, use_downloads: bool) -> None:
    print(f"Search mode: {query}")
    print(f"Normalized:  {norm_name(query)}")

    downloads_by: dict[str, list[dict]] = {}
    if use_downloads:
        print("Fetching GET /downloads …")
        downloads_by = index_downloads(rd.list_downloads())
        print(f"  {sum(len(v) for v in downloads_by.values())} entries")

    print("Fetching GET /torrents …")
    torrents = rd.list_torrents()
    torrent_name_by = index_torrent_names(torrents)
    print(f"  {len(torrents)} torrents, {len(torrent_name_by)} unique names")

    q = norm_name(query)
    resolver = LinkResolver(rd)

    print()
    print("=" * 60)
    print(f"SEARCH: {query}")
    print("=" * 60)

    dl_hits = downloads_by.get(q, [])
    if dl_hits:
        print(f"\n/downloads exact ({len(dl_hits)}):")
        for h in dl_hits:
            print(f"  {h['label']}")
            print(f"  link: {h.get('link') or '(no link)'}")

    candidates = torrent_candidates_for_query(query, torrent_name_by)
    if candidates:
        print(f"\nTorrent list matches ({len(candidates)}):")
        for i, c in enumerate(candidates, 1):
            link = resolver.link_for_filename(
                c.get("torrent_id", ""),
                query,
                c.get("link", ""),
            )
            print(f"\n  [{i}] {c['label']}")
            print(f"       {c.get('extra', '')}")
            print(f"       link: {link or '(no link — torrent may not be downloaded)'}")
    elif not dl_hits:
        print("No exact torrent-name match. Try a shorter substring query (e.g. barbie_a_porn_parody).")

    if resolver.fetch_count:
        print(f"\n(API: {resolver.fetch_count} torrent info fetch(es) for links)")


def report(
    jobs: list[dict],
    downloads_by: dict[str, list[dict]],
    torrent_name_by: dict[str, list[dict]],
    torrent_file_by: dict[str, list[dict]],
    resolver: LinkResolver,
    *,
    show_samples: int,
) -> None:
    stats = {
        "downloads_exact": 0,
        "torrent_name_exact": 0,
        "torrent_name_with_link": 0,
        "torrent_file_exact": 0,
        "any_with_link": 0,
        "none": 0,
    }
    unmatched: list[dict] = []
    matched_samples: list[dict] = []

    for job in jobs:
        fn = (job.get("filename") or "").strip()
        key = norm_name(fn)

        d_entries = downloads_by.get(key, [])
        d = d_entries[0] if d_entries else None
        d_link = (d.get("link") or "").strip() if d else ""

        tn_entries = torrent_name_by.get(key, [])
        tn = tn_entries[0] if tn_entries else None
        tn_link = ""
        if tn:
            tn_link = resolver.link_for_filename(
                tn.get("torrent_id", ""),
                fn,
                tn.get("link", ""),
            )

        tf_entries = torrent_file_by.get(key, [])
        tf = tf_entries[0] if tf_entries else None
        tf_link = (tf.get("link") or "").strip() if tf else ""

        best_link = d_link or tn_link or tf_link

        if d:
            stats["downloads_exact"] += 1
        if tn:
            stats["torrent_name_exact"] += 1
            if tn_link:
                stats["torrent_name_with_link"] += 1
        if tf:
            stats["torrent_file_exact"] += 1
        if best_link:
            stats["any_with_link"] += 1
            if len(matched_samples) < show_samples:
                matched_samples.append(
                    {
                        "job_id": job.get("id"),
                        "filename": fn,
                        "link": best_link,
                        "via": (
                            "downloads" if d_link else
                            "torrent_name" if tn_link else "torrent_file"
                        ),
                    }
                )
        else:
            if tn or d or tf:
                if len(matched_samples) < show_samples:
                    matched_samples.append(
                        {
                            "job_id": job.get("id"),
                            "filename": fn,
                            "link": "",
                            "via": "matched but no link",
                        }
                    )
            else:
                stats["none"] += 1
                unmatched.append(job)

    total = len(jobs)
    print()
    print("=" * 60)
    print("MATCH REPORT (exact normalized filename)")
    print("=" * 60)
    print(f"Jobs tested:                    {total}")
    if total:
        pct = lambda n: f"{100.0 * n / total:.1f}%"
        print(f"Matched /downloads:             {stats['downloads_exact']} ({pct(stats['downloads_exact'])})")
        print(f"Matched torrent name (list):    {stats['torrent_name_exact']} ({pct(stats['torrent_name_exact'])})")
        print(f"Torrent name → resolved link:   {stats['torrent_name_with_link']} ({pct(stats['torrent_name_with_link'])})")
        print(f"Matched torrent file path:      {stats['torrent_file_exact']} ({pct(stats['torrent_file_exact'])})")
        print(f"Any source with RD link:        {stats['any_with_link']} ({pct(stats['any_with_link'])})")
        print(f"No match:                       {stats['none']} ({pct(stats['none'])})")
    print(f"Torrent info API calls:         {resolver.fetch_count}")

    if matched_samples:
        print()
        print(f"Sample matches (first {len(matched_samples)}):")
        for m in matched_samples:
            link = m.get("link") or "(no link)"
            short = link[:90] + ("…" if len(link) > 90 else "")
            print(f"  #{m['job_id']} {m['filename']}")
            print(f"    {m.get('via')}: {short}")

    if unmatched and show_samples:
        print()
        print(f"Unmatched (first {min(show_samples, len(unmatched))}):")
        for job in unmatched[:show_samples]:
            err = (job.get("last_error") or "")[:60]
            print(f"  #{job['id']} {job['filename']}  [{job.get('status')}] {err}")

    print()
    print("Index sizes (unique normalized names):")
    print(f"  downloads:     {len(downloads_by)}")
    print(f"  torrent names: {len(torrent_name_by)}")
    print(f"  torrent files: {len(torrent_file_by)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe RD API for filename automation potential")
    parser.add_argument(
        "--db",
        default=os.environ.get("DB_PATH", "/data/state/queue.db"),
        help="SQLite queue path",
    )
    parser.add_argument(
        "--filenames",
        type=Path,
        help="Text file with one filename per line",
    )
    parser.add_argument(
        "--search",
        metavar="FILENAME",
        help="Search torrent list for this filename; resolve links for hits only",
    )
    parser.add_argument(
        "--full-torrent-info",
        action="store_true",
        help="Index every downloaded torrent's files (slow; thousands of API calls)",
    )
    parser.add_argument(
        "--no-downloads",
        action="store_true",
        help="Skip GET /downloads",
    )
    parser.add_argument(
        "--torrents-only",
        action="store_true",
        help="Only use GET /torrents",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=15,
        help="Sample lines in report",
    )
    args = parser.parse_args()

    use_downloads = not args.no_downloads and not args.torrents_only

    rd = RDClient()
    try:
        if args.search:
            run_search(rd, args.search, use_downloads=use_downloads)
            return

        jobs: list[dict] = []
        if args.filenames:
            jobs = load_filename_list(args.filenames)
            print(f"Loaded {len(jobs)} filenames from {args.filenames}")
        else:
            db_path = Path(args.db)
            jobs = load_queue_filenames(db_path)
            if jobs:
                print(f"Loaded {len(jobs)} failed/pending jobs from {db_path}")
            else:
                print(f"No failed/pending jobs in {db_path}")

        downloads_by: dict[str, list[dict]] = {}
        if use_downloads:
            print("Fetching GET /downloads …")
            downloads_by = index_downloads(rd.list_downloads())
            print(f"  {sum(len(v) for v in downloads_by.values())} entries")
        else:
            print("Skipping GET /downloads")

        print("Fetching GET /torrents …")
        torrents = rd.list_torrents()
        torrent_name_by = index_torrent_names(torrents)
        downloaded = [t for t in torrents if (t.get("status") or "") == "downloaded"]
        print(f"  {len(torrents)} torrents, {len(downloaded)} downloaded, {len(torrent_name_by)} unique names")

        torrent_file_by: dict[str, list[dict]] = {}
        if args.full_torrent_info:
            torrent_file_by = build_torrent_file_index(rd, downloaded)
        else:
            print("Lazy mode: torrent info fetched only for matched jobs (not all 1957 torrents)")

        resolver = LinkResolver(rd)
        report(
            jobs,
            downloads_by,
            torrent_name_by,
            torrent_file_by,
            resolver,
            show_samples=args.samples,
        )
    finally:
        rd.close()


if __name__ == "__main__":
    main()
