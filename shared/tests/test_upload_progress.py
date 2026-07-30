"""Tests for upload progress reporter."""

from __future__ import annotations

import unittest

from migradora.upload_progress import UploadProgressReporter


class UploadProgressReporterTests(unittest.TestCase):
    def test_split_and_upload_parts(self) -> None:
        reporter = UploadProgressReporter(folder_name="VR")
        reporter.set_splitting(source_bytes=20_000_000_000)
        reporter.prepare_parts(2)
        reporter.set_split_part_progress(
            1,
            label="movie.PART1.mkv",
            done_bytes=500_000_000,
            total_bytes=9_000_000_000,
            part_count=2,
        )
        snap = reporter.snapshot()
        self.assertEqual(snap["phase"], "splitting")
        self.assertEqual(len(snap["parts"]), 2)
        self.assertIn("Splitting part 1/2", reporter.status_text)

        reporter.register_part(1, "movie.PART1.mkv", 9_000_000_000, 2)
        reporter.part_progress(1, 4_500_000_000, 9_000_000_000, speed_bps=5_000_000, eta_sec=900)
        snap = reporter.snapshot()
        self.assertEqual(snap["phase"], "uploading")
        self.assertEqual(snap["parts"][0]["status"], "uploading")
        self.assertIn("Uploading part 1/2", reporter.status_text)

        reporter.complete_part(1)
        self.assertEqual(reporter.snapshot()["parts"][0]["status"], "done")


if __name__ == "__main__":
    unittest.main()
