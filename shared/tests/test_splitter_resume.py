"""Tests for split/upload resume in iter_upload_parts."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from migradora.splitter import iter_upload_parts


class SplitterResumeTests(unittest.TestCase):
    def test_bytes_mode_skips_uploaded_parts_and_reuses_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "big.bin"
            source.write_bytes(b"x" * 5000)
            out = root / "parts"
            part_size = 2000
            # First run: extract part 1 only (leave source for resume)
            gen = iter_upload_parts(
                source,
                out,
                part_size,
                skip_part_indices=frozenset(),
                reuse_existing_parts=False,
            )
            first = next(gen)
            self.assertEqual(first["part_index"], 1)
            first_path = Path(first["path"])
            self.assertTrue(first_path.exists())

            # Resume: skip part 1, reuse part 2 if on disk
            resumed = list(
                iter_upload_parts(
                    source,
                    out,
                    part_size,
                    skip_part_indices=frozenset({1}),
                    reuse_existing_parts=True,
                )
            )
            self.assertEqual(len(resumed), 2)
            self.assertEqual(resumed[0]["part_index"], 2)


if __name__ == "__main__":
    unittest.main()
