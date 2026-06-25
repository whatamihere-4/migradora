"""Unit tests for Filester folder lookup and create conflict handling."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import httpx

from migradora.filester_client import FilesterClient, FilesterFolder, FolderIndex


class FolderIndexTests(unittest.TestCase):
    def test_find_child_by_parent_db_id(self) -> None:
        index = FolderIndex([
            FilesterFolder(identifier="vr", name="VR", db_id=10, parent_db_id=None),
            FilesterFolder(identifier="czech", name="CzechVR", db_id=11, parent_db_id=10),
        ])
        hit = index.find_child("CzechVR", parent_db_id=10)
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.identifier, "czech")

    def test_find_child_by_parent_identifier(self) -> None:
        index = FolderIndex([
            FilesterFolder(identifier="vr", name="VR", db_id=10, parent_db_id=None),
            FilesterFolder(identifier="czech", name="CzechVR", db_id=11, parent_db_id=10),
        ])
        hit = index.find_child("CzechVR", parent_identifier="vr")
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.identifier, "czech")

    def test_find_child_name_fallback_root(self) -> None:
        index = FolderIndex([
            FilesterFolder(identifier="czech", name="CzechVR", db_id=11, parent_db_id=None),
        ])
        hit = index.find_child("CzechVR")
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.identifier, "czech")

    def test_find_child_ambiguous_same_name(self) -> None:
        index = FolderIndex([
            FilesterFolder(identifier="a", name="Clips", db_id=1, parent_db_id=None),
            FilesterFolder(identifier="b", name="Clips", db_id=2, parent_db_id=5),
        ])
        self.assertIsNone(index.find_child("Clips"))


class CreateFolderConflictTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FilesterClient("test-key")

    def tearDown(self) -> None:
        self.client.close()

    def test_create_returns_existing_on_409(self) -> None:
        existing = FilesterFolder(identifier="czech-id", name="CzechVR")

        conflict = httpx.Response(
            409,
            request=httpx.Request("POST", "https://u1.filester.me/api/v1/folder"),
            json={"success": False, "message": "You already have a folder with this name"},
        )

        with patch.object(self.client, "find_folder", return_value=existing):
            with patch.object(self.client, "_post_folder", side_effect=httpx.HTTPStatusError(
                "conflict", request=conflict.request, response=conflict
            )):
                folder = self.client.create_folder("CzechVR")
        self.assertEqual(folder.identifier, "czech-id")

    def test_parse_create_response_nested_folder(self) -> None:
        folder = FilesterClient._parse_folder_from_create({
            "success": True,
            "data": {
                "folder": {
                    "id": "6a648cb787ef0f18",
                    "name": "migradora-probe-test",
                },
                "identifier": "6a648cb787ef0f18",
            },
        })
        self.assertIsNotNone(folder)
        assert folder is not None
        self.assertEqual(folder.identifier, "6a648cb787ef0f18")
        self.assertEqual(folder.name, "migradora-probe-test")

    def test_parse_folder_keeps_hex_identifier_separate_from_db_id(self) -> None:
        folder = FilesterClient._parse_folder({
            "id": "a1b2c3d4e5f6",
            "name": "VR",
            "parent_id": 0,
        })
        self.assertIsNotNone(folder)
        assert folder is not None
        self.assertEqual(folder.identifier, "a1b2c3d4e5f6")
        self.assertIsNone(folder.db_id)

    def test_parse_folder_numeric_id(self) -> None:
        folder = FilesterClient._parse_folder({
            "id": 42,
            "identifier": "hexslug",
            "name": "CzechVR",
            "parent_id": 10,
        })
        self.assertIsNotNone(folder)
        assert folder is not None
        self.assertEqual(folder.db_id, 42)
        self.assertEqual(folder.identifier, "hexslug")
        self.assertEqual(folder.parent_db_id, 10)


if __name__ == "__main__":
    unittest.main()
