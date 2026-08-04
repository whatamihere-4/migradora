"""Tests for VPS disk / source file size limits."""

from __future__ import annotations

import unittest

from migradora.config import Settings
from migradora.size_limits import (
    disk_insufficient_skip_reason,
    incremental_disk_gb,
    is_disk_insufficient_skip_reason,
    is_oversize_budget_skip_reason,
    max_processable_source_bytes,
    oversize_skip_reason,
    required_disk_gb,
)


class SizeLimitTests(unittest.TestCase):
    def test_budget_45gb_defaults(self) -> None:
        settings = Settings(
            disk_budget_gb=45,
            min_free_disk_gb=5,
            filester_max_file_bytes=10_200_547_328,
            auto_skip_oversized=True,
        )
        max_bytes = max_processable_source_bytes(settings)
        # 45 - 5 - ~9.5 ≈ 30.5 GiB
        self.assertGreater(max_bytes, 30 * 1024**3)
        self.assertLess(max_bytes, 31 * 1024**3)

    def test_explicit_max_source_gb(self) -> None:
        settings = Settings(max_source_file_bytes=34 * 1024**3)
        self.assertEqual(max_processable_source_bytes(settings), 34 * 1024**3)

    def test_oversize_skip_reason(self) -> None:
        settings = Settings(
            disk_budget_gb=45,
            min_free_disk_gb=5,
            filester_max_file_bytes=10_200_547_328,
            auto_skip_oversized=True,
        )
        limit = max_processable_source_bytes(settings)
        self.assertIsNone(oversize_skip_reason(limit, settings))
        reason = oversize_skip_reason(limit + 1, settings)
        self.assertIsNotNone(reason)
        self.assertIn("too large", reason.lower())
        self.assertTrue(is_oversize_budget_skip_reason(reason))

    def test_disk_skip_reason_matching(self) -> None:
        disk_reason = disk_insufficient_skip_reason("movie.mp4", 34.0, 21.9)
        self.assertTrue(is_disk_insufficient_skip_reason(disk_reason))
        self.assertFalse(is_oversize_budget_skip_reason(disk_reason))
        budget_reason = oversize_skip_reason(50 * 1024**3, Settings(
            disk_budget_gb=45,
            min_free_disk_gb=5,
            filester_max_file_bytes=10_200_547_328,
            auto_skip_oversized=True,
        ))
        self.assertIsNotNone(budget_reason)
        self.assertTrue(is_oversize_budget_skip_reason(budget_reason))
        self.assertFalse(is_disk_insufficient_skip_reason(budget_reason))

    def test_incremental_disk_gb_resume(self) -> None:
        settings = Settings(
            min_free_disk_gb=5,
            filester_max_file_bytes=10_200_547_328,
            filester_split_mode="bytes",
        )
        file_size = 13 * 1024**3
        full_need = required_disk_gb(file_size, settings)
        self.assertGreater(full_need, 27.0)
        resume_need = incremental_disk_gb(
            file_size,
            settings,
            bytes_already_on_disk=file_size,
        )
        # Only one upload part + headroom once source is already on disk.
        self.assertLess(resume_need, 16.0)
        self.assertGreater(resume_need, 14.0)

    def test_incremental_disk_gb_fresh_download(self) -> None:
        settings = Settings(
            min_free_disk_gb=5,
            filester_max_file_bytes=10_200_547_328,
            filester_split_mode="bytes",
        )
        file_size = 13 * 1024**3
        self.assertEqual(
            incremental_disk_gb(file_size, settings, bytes_already_on_disk=0),
            required_disk_gb(file_size, settings),
        )


if __name__ == "__main__":
    unittest.main()
