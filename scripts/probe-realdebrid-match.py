#!/usr/bin/env python3
"""
Probe Real-Debrid downloads + torrents for filename matches against migradora jobs.

Usage (on VPS host — recommended; container often lacks outbound API access):
  export REAL_DEBRID_API_TOKEN=your_token
  ./scripts/probe-realdebrid-match.sh

  # Hunt one file:
  ./scripts/probe-realdebrid-match.sh --search 'VRCONK_barbie_a_porn_parody_8K_180x180_3dh.mp4'

  # Force running inside orchestrator container:
  PROBE_RD_IN_CONTAINER=1 ./scripts/probe-realdebrid-match.sh
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
    """Prefer IPv4 — many VPS containers have broken IPv6 routes to RD API."""
    if os.environ.get("PROBE_RD_IPV4", "1").strip().lower() in ("0", "false", "no"):
        return
    _orig = socket.getaddrinfo

    def _ipv4(host: str, port: Any, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0):
        return _orig(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = _ipv4  # type: ignore[method-assign]


def norm_name(name: str) -> str:
    """Case-insensitive match key; strip path and normalize unicode."""
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
                    "  Try from the VPS host (not docker): ./scripts/probe-realdebrid-match.sh\n"
                    "  Or: curl -4 -s -o /dev/null -w '%{http_code}' "
                    f"-H 'Authorization: Bearer $REAL_DEBRID_API_TOKEN' {API_BASE}/user"
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


def add_entry(
    store: dict[str, list[dict]],
    key: str,
    source: str,
    label: str,
    link: str,
    extra: str = "",
) -> None:
    if not key:
        return
    store.setdefault(key, []).append(
        {"source": source, "label": label, "link": link, "extra": extra}
    )


def build_indexes(
    rd: RDClient,
    *,
    torrent_info: bool,
    use_downloads: bool,
    use_torrents: bool,
) -> tuple[dict[str, list[dict]], dict[str, list[dict]], dict[str, list[dict]]]:
    """Returns (downloads_by_name, torrent_name_by_name, torrent_file_by_name)."""
    downloads_by: dict[str, list[dict]] = {}
    torrent_name_by: dict[str, list[dict]] = {}
    torrent_file_by: dict[str, list[dict]] = {}

    if use_downloads:
        print("Fetching GET /downloads …")
        downloads = rd.list_downloads()
        print(f"  {len(downloads)} download history entries")
        for item in downloads:
            fn = (item.get("filename") or "").strip()
            key = norm_name(fn)
            link = (item.get("download") or item.get("link") or "").strip()
            add_entry(
                downloads_by,
                key,
                "downloads",
                fn,
                link,
                extra=f"{item.get('filesize', 0)} B",
            )
    else:
        print("Skipping GET /downloads")

    downloaded: list[dict] = []
    if use_torrents:
        print("Fetching GET /torrents …")
        torrents = rd.list_torrents()
        print(f"  {len(torrents)} torrent list entries")

        downloaded = [t for t in torrents if (t.get("status") or "") == "downloaded"]
        print(f"  {len(downloaded)} with status=downloaded")

        for t in torrents:
            tid = (t.get("id") or "").strip()
            tname = (t.get("filename") or "").strip()
            key = norm_name(tname)
            links = t.get("links") or []
            link = links[0] if links else ""
            add_entry(
                torrent_name_by,
                key,
                "torrents.name",
                tname,
                link,
                extra=f"id={tid} status={t.get('status')}",
            )
    else:
        print("Skipping GET /torrents")

    if torrent_info and downloaded:
        print(f"Fetching GET /torrents/info/{{id}} for {len(downloaded)} downloaded torrents …")
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
                link = ""
                if idx < len(links):
                    link = (links[idx] or "").strip()
                add_entry(
                    torrent_file_by,
                    key,
                    "torrents.file",
                    path.lstrip("/"),
                    link,
                    extra=f"torrent={info.get('filename', t.get('filename', ''))} id={tid}",
                )
            if i % 25 == 0 or i == len(downloaded):
                print(f"  … {i}/{len(downloaded)} torrent infos fetched")
            time.sleep(0.05)

    return downloads_by, torrent_name_by, torrent_file_by


def pick_best(matches: list[dict]) -> dict | None:
    if not matches:
        return None
    with_link = [m for m in matches if m.get("link")]
    return with_link[0] if with_link else matches[0]


def collect_substring_matches(
    query: str,
    downloads_by: dict[str, list[dict]],
    torrent_name_by: dict[str, list[dict]],
    torrent_file_by: dict[str, list[dict]],
) -> list[dict]:
    q = norm_name(query)
    if not q:
        return []

    hits: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    def scan(store: dict[str, list[dict]], source_label: str) -> None:
        for key, entries in store.items():
            if q == key or q in key or key in q:
                for entry in entries:
                    sig = (source_label, entry.get("label", ""), entry.get("link", ""))
                    if sig in seen:
                        continue
                    seen.add(sig)
                    hits.append({**entry, "match_key": key, "source_label": source_label})

    scan(downloads_by, "downloads")
    scan(torrent_name_by, "torrents.name")
    scan(torrent_file_by, "torrents.file")
    return hits


def print_search_results(query: str, hits: list[dict]) -> None:
    print()
    print("=" * 60)
    print(f"SEARCH: {query}")
    print(f"Normalized: {norm_name(query)}")
    print("=" * 60)
    if not hits:
        print("No matches (exact or substring) in indexed RD data.")
        return
    print(f"Found {len(hits)} match(es):")
    for i, hit in enumerate(hits, 1):
        link = hit.get("link") or "(no link)"
        print(f"\n[{i}] {hit.get('source_label')} — {hit.get('label')}")
        if hit.get("extra"):
            print(f"     {hit['extra']}")
        print(f"     link: {link}")


def report(
    jobs: list[dict],
    downloads_by: dict[str, list[dict]],
    torrent_name_by: dict[str, list[dict]],
    torrent_file_by: dict[str, list[dict]],
    *,
    show_samples: int,
) -> None:
    stats = {
        "downloads_exact": 0,
        "torrent_name_exact": 0,
        "torrent_file_exact": 0,
        "any_exact": 0,
        "none": 0,
    }
    unmatched: list[dict] = []
    matched_samples: list[dict] = []

    for job in jobs:
        fn = (job.get("filename") or "").strip()
        key = norm_name(fn)
        d = pick_best(downloads_by.get(key, []))
        tn = pick_best(torrent_name_by.get(key, []))
        tf = pick_best(torrent_file_by.get(key, []))
        if d:
            stats["downloads_exact"] += 1
        if tn:
            stats["torrent_name_exact"] += 1
        if tf:
            stats["torrent_file_exact"] += 1
        if d or tn or tf:
            stats["any_exact"] += 1
            if len(matched_samples) < show_samples:
                matched_samples.append(
                    {
                        "job_id": job.get("id"),
                        "filename": fn,
                        "downloads": d,
                        "torrent_name": tn,
                        "torrent_file": tf,
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
    print(f"Jobs tested:              {total}")
    if total:
        pct = lambda n: f"{100.0 * n / total:.1f}%"
        print(f"Matched via /downloads:   {stats['downloads_exact']} ({pct(stats['downloads_exact'])})")
        print(f"Matched torrent name:     {stats['torrent_name_exact']} ({pct(stats['torrent_name_exact'])})")
        print(f"Matched torrent file path:{stats['torrent_file_exact']} ({pct(stats['torrent_file_exact'])})")
        print(f"Matched any source:       {stats['any_exact']} ({pct(stats['any_exact'])})")
        print(f"No exact match:           {stats['none']} ({pct(stats['none'])})")

    if matched_samples:
        print()
        print(f"Sample matches (first {len(matched_samples)}):")
        for m in matched_samples:
            print(f"  #{m['job_id']} {m['filename']}")
            for label, hit in (
                ("downloads", m["downloads"]),
                ("torrent_name", m["torrent_name"]),
                ("torrent_file", m["torrent_file"]),
            ):
                if hit:
                    link = hit.get("link") or "(no link in API response)"
                    short = link[:80] + ("…" if len(link) > 80 else "")
                    print(f"    {label}: {hit['source']} — {short}")

    if unmatched and show_samples:
        print()
        print(f"Unmatched sample (first {min(show_samples, len(unmatched))}):")
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
        help="SQLite queue path (default: DB_PATH or /data/state/queue.db)",
    )
    parser.add_argument(
        "--filenames",
        type=Path,
        help="Text file with one filename per line (instead of queue.db)",
    )
    parser.add_argument(
        "--search",
        metavar="FILENAME",
        help="Search RD indexes for this filename (exact + substring); skips queue report",
    )
    parser.add_argument(
        "--no-torrent-info",
        action="store_true",
        help="Skip GET /torrents/info (faster; no torrent file path matching)",
    )
    parser.add_argument(
        "--no-downloads",
        action="store_true",
        help="Skip GET /downloads (use torrent list only)",
    )
    parser.add_argument(
        "--torrents-only",
        action="store_true",
        help="Only fetch GET /torrents (skip downloads history)",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=15,
        help="How many match/unmatch examples to print",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print full indexes as JSON (large)",
    )
    args = parser.parse_args()

    use_downloads = not args.no_downloads and not args.torrents_only
    use_torrents = True

    jobs: list[dict] = []
    if not args.search:
        if args.filenames:
            jobs = load_filename_list(args.filenames)
            print(f"Loaded {len(jobs)} filenames from {args.filenames}")
        else:
            db_path = Path(args.db)
            jobs = load_queue_filenames(db_path)
            if jobs:
                print(f"Loaded {len(jobs)} failed/pending jobs from {db_path}")
            else:
                print(f"No failed/pending jobs in {db_path} — pass --filenames or fix --db")

    rd = RDClient()
    try:
        downloads_by, torrent_name_by, torrent_file_by = build_indexes(
            rd,
            torrent_info=not args.no_torrent_info,
            use_downloads=use_downloads,
            use_torrents=use_torrents,
        )
    finally:
        rd.close()

    if args.search:
        hits = collect_substring_matches(
            args.search,
            downloads_by,
            torrent_name_by,
            torrent_file_by,
        )
        print_search_results(args.search, hits)
        return

    if args.json:
        print(
            json.dumps(
                {
                    "downloads": downloads_by,
                    "torrent_names": torrent_name_by,
                    "torrent_files": torrent_file_by,
                },
                indent=2,
            )
        )
        return

    report(
        jobs,
        downloads_by,
        torrent_name_by,
        torrent_file_by,
        show_samples=args.samples,
    )


if __name__ == "__main__":
    main()
