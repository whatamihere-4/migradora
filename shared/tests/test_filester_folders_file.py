"""Tests for filester-folders.json loading and name resolution."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from migradora.filester_folders_file import (
    folder_match_key,
    load_filester_folders,
    parse_html_folder_cards,
    resolve_folder_id,
    save_filester_folders,
)


class FolderMatchTests(unittest.TestCase):
    def test_whitespace_insensitive(self) -> None:
        self.assertEqual(folder_match_key("VR Bangers"), folder_match_key("VRBangers"))

    def test_resolve_exact(self) -> None:
        folders = {"abc": "CzechVR"}
        self.assertEqual(resolve_folder_id(folders, "CzechVR"), "abc")

    def test_resolve_normalized(self) -> None:
        folders = {"abc": "VR Bangers"}
        self.assertEqual(resolve_folder_id(folders, "VRBangers"), "abc")


class HtmlParseTests(unittest.TestCase):
    def test_delete_modal_pattern(self) -> None:
        html = """
        showDeleteFolderModal(
            '10e5627dccae2818',
            `18VR`,
            ``,
            0,
            ``,
            0
        )
        """
        parsed = parse_html_folder_cards(html)
        self.assertEqual(parsed, {"10e5627dccae2818": "18VR"})


class JsonRoundTripTests(unittest.TestCase):
    def test_save_and_load(self) -> None:
        data = {"bcc058337623e096": "CzechVR", "558b65a42fdad1f6": "VR"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "filester-folders.json"
            save_filester_folders(path, data)
            loaded = load_filester_folders(path)
            self.assertEqual(loaded, data)
            text = path.read_text(encoding="utf-8")
            self.assertIn('"bcc058337623e096": "CzechVR"', text)


if __name__ == "__main__":
    unittest.main()
