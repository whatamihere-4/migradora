"""Tests for pipeline retry behavior (preserve files, rebuild resume)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from migradora.models import FileRecord, FileStatus
from migradora.pipeline import (
    _rebuild_resume_from_slugs,
    should_preserve_job_files_on_failure,
)
from migradora.upload_resume import save_upload_resume_state, UploadResumeState


class PipelineRetryTests(unittest.TestCase):
    def test_should_preserve_when_retries_remain(self) -> None:
        self.assertTrue(should_preserve_job_files_on_failure(1, 3))
        self.assertFalse(should_preserve_job_files_on_failure(3, 3))

    def test_rebuild_resume_from_slugs_split_file(self) -> None:
        settings = type("S", (), {"filester_max_file_bytes": 10 * 1024**3})()
        job = FileRecord(
            id=1,
            gofile_content_id="x",
            gofile_path="VR/Studio/movie.mp4",
            filename="movie.mp4",
            size_bytes=17 * 1024**3,
            download_link=None,
            gofile_url=None,
            jd2_package_name=None,
            sha256=None,
            oshash="abc",
            status=FileStatus.PENDING,
            local_path="/data/job-1/movie.mp4",
            filester_slug=["slug-part-1"],
            parent_folder_path="VR/Studio",
            attempts=2,
            last_error="upload failed",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        state = _rebuild_resume_from_slugs(job, settings)
        assert state is not None
        self.assertEqual(state.skip_part_indices(), frozenset({1}))
        self.assertEqual(state.total_parts, 2)
        self.assertTrue(state.was_split)
        self.assertEqual(state.parts[0].slug, "slug-part-1")
        self.assertTrue(state.parts[0].verified)

    def test_rebuild_resume_round_trip_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp) / "job-1"
            job_dir.mkdir()
            settings = type("S", (), {"filester_max_file_bytes": 10 * 1024**3})()
            job = FileRecord(
                id=1,
                gofile_content_id="x",
                gofile_path="VR/Studio/big.mp4",
                filename="big.mp4",
                size_bytes=17 * 1024**3,
                download_link=None,
                gofile_url=None,
                jd2_package_name=None,
                sha256=None,
                oshash=None,
                status=FileStatus.PENDING,
                local_path=str(job_dir / "big.mp4"),
                filester_slug=["slug-1"],
                parent_folder_path="VR/Studio",
                attempts=2,
                last_error=None,
                created_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:00:00Z",
            )
            state = _rebuild_resume_from_slugs(job, settings)
            assert state is not None
            save_upload_resume_state(job_dir, state)
            loaded = UploadResumeState.from_dict(
                __import__("json").loads(
                    (job_dir / ".upload-resume.json").read_text(encoding="utf-8")
                )
            )
            self.assertEqual(loaded.skip_part_indices(), frozenset({1}))


if __name__ == "__main__":
    unittest.main()
