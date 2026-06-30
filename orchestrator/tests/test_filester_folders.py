"""Tests for Filester folder helpers."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from migradora.filester_client import FilesterFolder
from migradora.filester_folders import ensure_split_parts_folder, sanitize_folder_name


class SanitizeFolderNameTests(unittest.TestCase):
    def test_strips_unsafe_chars(self) -> None:
        self.assertEqual(sanitize_folder_name('foo/bar:bad?.mp4'), "foobad.mp4")

    def test_collapses_whitespace(self) -> None:
        self.assertEqual(sanitize_folder_name("  My   Scene.mp4  "), "My Scene.mp4")

    def test_empty_fallback(self) -> None:
        self.assertEqual(sanitize_folder_name("   "), "upload")


class EnsureSplitPartsFolderTests(unittest.TestCase):
    def test_creates_subfolder_under_parent(self) -> None:
        client = MagicMock()
        client.create_folder.return_value = FilesterFolder(
            identifier="split-folder",
            name="My Scene.mp4",
        )
        folder_id = ensure_split_parts_folder(client, "studio-id", "My Scene.mp4")
        self.assertEqual(folder_id, "split-folder")
        client.create_folder.assert_called_once_with(
            "My Scene.mp4",
            parent_identifier="studio-id",
        )

    def test_requires_parent(self) -> None:
        client = MagicMock()
        with self.assertRaises(RuntimeError):
            ensure_split_parts_folder(client, "", "video.mp4")


if __name__ == "__main__":
    unittest.main()
