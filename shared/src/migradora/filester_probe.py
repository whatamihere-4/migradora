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


def _try_request(
    client: FilesterClient,
    label: str,
    method: str,
    path: str,
    payload: dict[str, object] | None = None,
) -> tuple[int, dict[str, Any] | None, str]:
    print(f"\n== {method} {path} [{label}] ==")
    if payload is not None:
        print(f"  payload: {payload}")
    status, body, text = client._raw_request(method, path, json=payload)
    print(f"  status={status}")
    if body:
        print(f"  body: {_short(body, 600)}")
    elif text:
        print(f"  text: {text[:300]}")
    return status, body, text


def _try_create(
    client: FilesterClient,
    label: str,
    endpoint: str,
    payload: dict[str, object],
) -> None:
    _status, body, _text = _try_request(client, label, "POST", endpoint, payload)
    parsed = FilesterClient._parse_folder((body or {}).get("data", {}))
    if parsed:
        _print_folder("parsed", parsed)


def _probe_move_variants(
    client: FilesterClient,
    folder_identifier: str,
    *,
    parent_identifier: str | None,
    parent_db_id: int | None,
    dry_run: bool,
) -> None:
    if dry_run:
        print("\n(dry-run: skipping move probes)")
        return

    folder = client.resolve_folder(folder_identifier)
    _print_folder("target", folder)

    parent_payloads: list[tuple[str, dict[str, object]]] = []
    if parent_identifier:
        parent_payloads.extend([
            ("parent_id str", {"parent_id": parent_identifier}),
            ("parent_folder_id str", {"parent_folder_id": parent_identifier}),
            ("folder_id+parent_id", {
                "identifier": folder_identifier,
                "parent_id": parent_identifier,
            }),
            ("folder_id+parent_folder_id", {
                "folder_id": folder_identifier,
                "parent_id": parent_identifier,
            }),
        ])
    if parent_db_id is not None:
        parent_payloads.extend([
            ("parent_id int", {"parent_id": parent_db_id}),
            ("folder_id+parent_id int", {
                "identifier": folder_identifier,
                "parent_id": parent_db_id,
            }),
        ])
    if not parent_payloads:
        print("\n(move probes: pass --parent-identifier or --parent-db-id)")
        return

    path_ids: list[tuple[str, str]] = [("identifier", folder_identifier)]
    if folder.db_id is not None:
        path_ids.append(("db_id", str(folder.db_id)))

    for id_label, path_id in path_ids:
        for method in ("PATCH", "PUT", "POST"):
            for payload_label, payload in parent_payloads:
                _try_request(
                    client,
                    f"{id_label} {payload_label}",
                    method,
                    f"/api/v1/folder/{path_id}",
                    payload,
                )
                time.sleep(0.2)

    move_bodies: list[tuple[str, dict[str, object]]] = [
        ("identifiers+parent", {
            "identifiers": [folder_identifier],
            "parent_id": parent_identifier or parent_db_id,
        }),
        ("folder_id+parent", {
            "folder_id": folder_identifier,
            "parent_id": parent_identifier or parent_db_id,
        }),
    ]
    move_paths = [
        "/api/v1/folder/move",
        "/api/v1/folders/move",
        "/folder/move",
        "/folder/update",
        "/api/folder/move",
    ]
    for endpoint in move_paths:
        for label, payload in move_bodies:
            if payload.get("parent_id") is None:
                continue
            _try_request(client, label, "POST", endpoint, payload)
            time.sleep(0.2)


def _probe_child_list(client: FilesterClient, parent_identifier: str) -> None:
    print(f"\n== list_child_folders({parent_identifier}) ==")
    children = client.list_child_folders(parent_identifier)
    print(f"  count={len(children)}")
    for child in children:
        _print_folder("child", child)


def _probe_create_variants(
    client: FilesterClient,
    name: str,
    parent_db_id: int | None,
    parent_identifier: str | None,
    *,
    dry_run: bool,
    nested_only: bool,
) -> None:
    if dry_run:
        print("\n(dry-run: skipping create probes)")
        return

    base = {"name": name, "public": 1}
    variants: list[tuple[str, str, dict[str, object]]] = []
    if not nested_only:
        variants.append(("root v1", "/api/v1/folder", dict(base)))
    if parent_db_id is not None:
        variants.append(
            ("nested int parent v1", "/api/v1/folder", {**base, "parent_id": parent_db_id}),
        )
    if parent_identifier:
        variants.extend([
            ("nested str parent v1", "/api/v1/folder", {**base, "parent_id": parent_identifier}),
            ("parent_folder_id v1", "/api/v1/folder", {**base, "parent_folder_id": parent_identifier}),
        ])
    if nested_only and not variants:
        print("\n(nested-only: pass --parent-identifier)")
        return

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
    parser.add_argument(
        "--nested-only",
        action="store_true",
        help="Only test nested create (skip root create; use a unique --name)",
    )
    parser.add_argument(
        "--probe-move",
        action="store_true",
        help="Try PATCH/PUT/POST move endpoints for --folder-identifier",
    )
    parser.add_argument(
        "--folder-identifier",
        default=None,
        help="Folder to move (with --probe-move)",
    )
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

        if args.parent_identifier:
            _probe_child_list(client, args.parent_identifier)

        if args.probe_move:
            if not args.folder_identifier:
                print("--probe-move requires --folder-identifier", file=sys.stderr)
                return 1
            print("\n== GET folder detail probes ==")
            for path in (
                f"/api/v1/folder/{args.folder_identifier}",
                f"/api/v1/folders/{args.folder_identifier}",
            ):
                _try_request(client, "detail", "GET", path)
            _probe_move_variants(
                client,
                args.folder_identifier,
                parent_identifier=args.parent_identifier,
                parent_db_id=args.parent_db_id,
                dry_run=args.dry_run,
            )

        _probe_create_variants(
            client,
            args.name,
            args.parent_db_id,
            args.parent_identifier,
            dry_run=args.dry_run,
            nested_only=args.nested_only,
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

    print("\n== API notes ==")
    print("  Documented: GET /api/v1/folders, POST /api/v1/folder, GET /api/v1/folder/{id}/files")
    print("  No documented folder detail (GET /api/v1/folder/{id}) or move/reparent endpoints.")
    print("  Nesting: POST /api/v1/folder with parent_id=<parent folder identifier>.")
    print("  If 409 DUPLICATE_NAME: rename/delete root folder with that name first.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_probe())
