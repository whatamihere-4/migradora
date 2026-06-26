#!/usr/bin/env python3
"""Build filester-folders.json from Filester manager HTML (folder cards)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "src"))

from migradora.filester_folders_file import (  # noqa: E402
    parse_html_folder_cards,
    save_filester_folders,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract Filester folder ids from manager page HTML.",
    )
    parser.add_argument(
        "html",
        nargs="?",
        type=Path,
        help="HTML file (stdin if omitted)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=ROOT / "config" / "filester-folders.json",
        help="Output JSON path (default: config/filester-folders.json)",
    )
    parser.add_argument(
        "--root-id",
        help="Optional VR root folder id to add as name 'VR'",
    )
    args = parser.parse_args()

    if args.html:
        html = args.html.read_text(encoding="utf-8", errors="replace")
    else:
        html = sys.stdin.read()

    folders = parse_html_folder_cards(html)
    if not folders:
        print("No folder cards found in HTML.", file=sys.stderr)
        return 1

    if args.root_id:
        folders[args.root_id.strip()] = "VR"

    save_filester_folders(args.output, folders)
    print(f"Wrote {len(folders)} folder(s) to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
