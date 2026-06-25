"""Tests for VPS disk / source file size limits."""

from __future__ import annotations

import unittest

from migradora.config import Settings
from migradora.size_limits import max_processable_source_bytes, oversize_skip_reason


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


if __name__ == "__main__":
    unittest.main()
