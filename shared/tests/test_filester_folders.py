"""Tests for nested Filester folder path mapping."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from migradora.config import Settings
from migradora.filester_folders import (
    _full_path,
    _mapped_folder_id,
    ensure_filester_folder_path,
)


class FullPathTests(unittest.TestCase):
    def test_keeps_vr_prefix(self) -> None:
        settings = Settings(filester_root_folder_name="VR")
        self.assertEqual(_full_path(settings, "VR/CzechVR"), "VR/CzechVR")

    def test_adds_root_when_missing(self) -> None:
        settings = Settings(filester_root_folder_name="VR")
        self.assertEqual(_full_path(settings, "CzechVR"), "VR/CzechVR")

    def test_no_double_vr(self) -> None:
        settings = Settings(filester_root_folder_name="VR")
        self.assertEqual(_full_path(settings, "VR/18VR"), "VR/18VR")


class MappedFolderTests(unittest.TestCase):
    def test_leaf_name_lookup(self) -> None:
        settings = Settings(
            filester_folders={"abc123": "CzechVR"},
        )
        self.assertEqual(_mapped_folder_id(settings, "VR/CzechVR"), "abc123")

    def test_normalized_name_lookup(self) -> None:
        settings = Settings(
            filester_folders={"abc123": "VR Bangers"},
        )
        self.assertEqual(_mapped_folder_id(settings, "VR/VRBangers"), "abc123")


class EnsureMappedFolderTests(unittest.TestCase):
    def test_uses_map_without_api_create(self) -> None:
        settings = Settings(
            filester_root_folder_name="VR",
            filester_root_folder_id="vr-root",
            filester_folders={"czech-nested": "CzechVR"},
            filester_auto_create_folders=False,
        )
        client = MagicMock()
        queue = MagicMock()
        queue.get_folder_mapping_record.return_value = None

        folder_id = ensure_filester_folder_path(
            client,
            queue,
            settings,
            "CzechVR",
            {},
        )

        self.assertEqual(folder_id, "czech-nested")
        client.create_folder.assert_not_called()
        client.find_folder.assert_not_called()


if __name__ == "__main__":
    unittest.main()
