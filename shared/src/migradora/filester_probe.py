"""Probe Filester folder API endpoints and document what works."""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from migradora.config import Settings
from migradora.filester_client import FilesterClient, FilesterFolder


def _short(obj: Any, limit: int = 400) -> str:
    text = json.dumps(obj, indent=2, default=str)
    if len(text) > limit:
        return text[:limit] + "\n  ..."
    return text


def _print_folder(label: str, folder: FilesterFolder | None) -> None:
    if not folder:
        print(f"  {label}: <not found>")
        return
    print(
        f"  {label}: name={folder.name!r} identifier={folder.identifier!r} "
        f"db_id={folder.db_id} parent_db_id={folder.parent_db_id}"
    )


def _list_sources(client: FilesterClient) -> None:
    print("\n== GET /api/v1/folders ==")
    status, body, text = client._raw_request("GET", "/api/v1/folders")
    print(f"  status={status}")
    rows = (body or {}).get("data", []) if body else []
    print(f"  count={len(rows)}")
    if rows:
        print(f"  sample keys: {sorted(rows[0].keys())}")
        print(f"  sample row: {_short(rows[0])}")
        for row in rows[:15]:
            folder = FilesterClient._parse_folder(row)
            if folder:
                _print_folder("row", folder)

    print("\n== GET /api/user/folders ==")
    status, body, text = client._raw_request("GET", "/api/user/folders")
    print(f"  status={status}")
    if status != 200:
        print(f"  body: {text[:300]}")
    else:
        flat = (body or {}).get("folders") or []
        hier = (body or {}).get("hierarchical") or []
        print(f"  folders={len(flat)} hierarchical_roots={len(hier)}")
        if flat:
            print(f"  sample keys: {sorted(flat[0].keys())}")
            print(f"  sample row: {_short(flat[0])}")
        if hier:
            print(f"  hierarchical sample: {_short(hier[0])}")


def _try_create(
    client: FilesterClient,
    label: str,
    endpoint: str,
    payload: dict[str, object],
) -> None:
    print(f"\n== POST {endpoint} [{label}] ==")
    print(f"  payload: {payload}")
    status, body, text = client._raw_request("POST", endpoint, json=payload)
    print(f"  status={status}")
    if body:
        print(f"  body: {_short(body, 600)}")
    elif text:
        print(f"  text: {text[:300]}")
    parsed = FilesterClient._parse_folder((body or {}).get("data", {}))
    if parsed:
        _print_folder("parsed", parsed)


def _probe_create_variants(
    client: FilesterClient,
    name: str,
    parent_db_id: int | None,
    parent_identifier: str | None,
    *,
    dry_run: bool,
) -> None:
    if dry_run:
        print("\n(dry-run: skipping create probes)")
        return

    base = {"name": name, "public": 1}
    variants: list[tuple[str, str, dict[str, object]]] = [
        ("root v1", "/api/v1/folder", dict(base)),
        ("root web", "/api/folder/create", dict(base)),
    ]
    if parent_db_id is not None:
        variants.extend([
            ("nested int parent v1", "/api/v1/folder", {**base, "parent_id": parent_db_id}),
            ("nested int parent web", "/api/folder/create", {**base, "parent_id": parent_db_id}),
        ])
    if parent_identifier:
        variants.extend([
            ("nested str parent v1", "/api/v1/folder", {**base, "parent_id": parent_identifier}),
            ("nested str parent web", "/api/folder/create", {**base, "parent_id": parent_identifier}),
            ("parent_folder_id v1", "/api/v1/folder", {**base, "parent_folder_id": parent_identifier}),
        ])

    for label, endpoint, payload in variants:
        _try_create(client, label, endpoint, payload)
        time.sleep(0.3)


def run_probe(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe Filester folder API behavior")
    parser.add_argument("--name", default="migradora-probe-test", help="Folder name for create probes")
    parser.add_argument("--parent-db-id", type=int, default=None, help="Numeric parent folder id")
    parser.add_argument("--parent-identifier", default=None, help="Parent folder identifier slug")
    parser.add_argument("--search", default=None, help="Find folder by name in current listings")
    parser.add_argument("--dry-run", action="store_true", help="List only; do not POST create probes")
    args = parser.parse_args(argv)

    settings = Settings.load()
    if not settings.filester_api_key:
        print("FILESTER_API_KEY is not set", file=sys.stderr)
        return 1

    print(f"API base: {settings.filester_api_base}")
    with FilesterClient(settings.filester_api_key, settings.filester_api_base) as client:
        try:
            acct = client.get_account()
            print(f"Account: {_short(acct, 200)}")
        except Exception as exc:
            print(f"Account check failed: {exc}")

        _list_sources(client)

        index = client.folder_index(refresh=True)
        print(f"\n== Parsed folder index ({len(index.all_folders())} folders) ==")
        for folder in sorted(index.all_folders(), key=lambda f: (f.parent_db_id or 0, f.name)):
            _print_folder("folder", folder)

        search_name = args.search or args.name
        print(f"\n== find_folder({search_name!r}) ==")
        _print_folder(
            "root",
            client.find_folder(search_name),
        )
        if args.parent_db_id is not None or args.parent_identifier:
            _print_folder(
                "under parent",
                client.find_folder(
                    search_name,
                    parent_db_id=args.parent_db_id,
                    parent_identifier=args.parent_identifier,
                ),
            )

        _probe_create_variants(
            client,
            args.name,
            args.parent_db_id,
            args.parent_identifier,
            dry_run=args.dry_run,
        )

        print("\n== client.create_folder() (production code path) ==")
        if args.dry_run:
            print("  skipped (dry-run)")
        else:
            try:
                folder = client.create_folder(
                    args.name,
                    parent_db_id=args.parent_db_id,
                    parent_identifier=args.parent_identifier,
                )
                _print_folder("result", folder)
            except Exception as exc:
                print(f"  ERROR: {exc}")

    print("\nDone. Use --search CzechVR to inspect an existing folder name.")
    print("Use --parent-identifier <VR slug> when probing nested creates.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_probe())
