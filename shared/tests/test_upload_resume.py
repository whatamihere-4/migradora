"""Tests for upload resume state persistence."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from migradora.upload_resume import (
    UploadedPart,
    UploadResumeState,
    delete_upload_resume_state,
    load_upload_resume_state,
    save_upload_resume_state,
)


class UploadResumeStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.job_dir = Path(self._tmp.name) / "job-1"

    def test_round_trip_and_skip_indices(self) -> None:
        state = UploadResumeState(
            oshash="abc",
            source_path="/data/movie.mp4",
            was_split=True,
            total_parts=3,
            parts=[
                UploadedPart(
                    part_index=1,
                    filename="movie.part001",
                    size_bytes=1000,
                    slug="slug1",
                    upload_response={"slug": "slug1"},
                ),
            ],
        )
        save_upload_resume_state(self.job_dir, state)
        loaded = load_upload_resume_state(self.job_dir)
        assert loaded is not None
        self.assertEqual(loaded.oshash, "abc")
        self.assertEqual(loaded.total_parts, 3)
        self.assertEqual(loaded.skip_part_indices(), frozenset({1}))
        self.assertFalse(loaded.upload_complete())

    def test_upload_complete(self) -> None:
        state = UploadResumeState(
            total_parts=2,
            parts=[
                UploadedPart(1, "a", 10, "s1"),
                UploadedPart(2, "b", 20, "s2"),
            ],
        )
        self.assertTrue(state.upload_complete())

    def test_delete_state(self) -> None:
        save_upload_resume_state(self.job_dir, UploadResumeState())
        delete_upload_resume_state(self.job_dir)
        self.assertIsNone(load_upload_resume_state(self.job_dir))


if __name__ == "__main__":
    unittest.main()
