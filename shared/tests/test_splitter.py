"""Tests for file splitting modes."""

from __future__ import annotations

import unittest

from migradora.splitter import parse_split_mode, required_disk_bytes


class SplitModeTests(unittest.TestCase):
    def test_parse_split_mode_aliases(self) -> None:
        self.assertEqual(parse_split_mode("bytes"), "bytes")
        self.assertEqual(parse_split_mode("cat"), "bytes")
        self.assertEqual(parse_split_mode("ffmpeg_slice"), "ffmpeg_slice")
        self.assertEqual(parse_split_mode("slice"), "ffmpeg_slice")
        self.assertEqual(parse_split_mode("unknown", default="bytes"), "bytes")

    def test_required_disk_bytes_modes(self) -> None:
        size = 30 * 1024**3
        part = 10 * 1024**3
        self.assertEqual(required_disk_bytes(size, part, split_mode="bytes"), size + part)
        self.assertEqual(
            required_disk_bytes(size, part, split_mode="ffmpeg_slice"),
            size + part,
        )
        self.assertEqual(required_disk_bytes(size, part, split_mode="ffmpeg"), size * 2)

    def test_required_disk_bytes_small_file(self) -> None:
        self.assertEqual(required_disk_bytes(100, 1024**3), 100)


if __name__ == "__main__":
    unittest.main()
