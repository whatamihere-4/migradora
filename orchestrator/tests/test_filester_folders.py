"""Tests for Filester folder helpers."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from migradora.filester_client import FilesterFolder
from migradora.filester_folders import (
    ensure_split_parts_folder,
    organize_split_parts_into_folder,
    sanitize_folder_name,
)


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
            parent_identifier="studio-id",
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


class OrganizeSplitPartsTests(unittest.TestCase):
    def test_moves_uploaded_parts_into_subfolder(self) -> None:
        client = MagicMock()
        client.file_identifier_from_response.side_effect = ["slug1", "slug2"]
        client.create_folder.return_value = FilesterFolder(
            identifier="split-folder",
            name="movie.mp4",
            parent_identifier="studio-id",
        )
        client.folder_is_under_parent.return_value = True
        client.move_files.return_value = {"moved": 2, "failed": 0}

        dest = organize_split_parts_into_folder(
            client,
            parent_folder_id="studio-id",
            folder_name="movie.mp4",
            upload_responses=[{"slug": "slug1"}, {"slug": "slug2"}],
        )
        self.assertEqual(dest, "split-folder")
        client.move_files.assert_called_once_with(["slug1", "slug2"], "split-folder")

    def test_skips_when_no_file_ids(self) -> None:
        client = MagicMock()
        client.file_identifier_from_response.return_value = ""
        dest = organize_split_parts_into_folder(
            client,
            parent_folder_id="studio-id",
            folder_name="movie.mp4",
            upload_responses=[{}],
        )
        self.assertEqual(dest, "studio-id")
        client.move_files.assert_not_called()


if __name__ == "__main__":
    unittest.main()
