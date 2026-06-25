"""Tests for nested Filester folder path mapping."""

from __future__ import annotations

import unittest

from migradora.config import Settings
from migradora.filester_folders import _full_path


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


if __name__ == "__main__":
    unittest.main()
