"""Tests for job download cleanup helpers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from migradora.job_cleanup import cleanup_job_files, purge_stale_job_dirs, release_job_downloads
from migradora.models import FileStatus
from migradora.queue.manager import QueueManager


class JobCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.download_dir = Path(self._tmp.name) / "downloads"
        self.download_dir.mkdir()
        self.db_path = str(Path(self._tmp.name) / "queue.db")
        self.queue = QueueManager(self.db_path)

        class Settings:
            download_dir = str(self.download_dir)

        self.settings = Settings()

    def _enqueue(self, name: str) -> int:
        job_id = self.queue.enqueue_file(
            gofile_content_id=f"gf-{name}",
            gofile_path=f"VR/Studio/{name}",
            filename=name,
            size_bytes=1024,
            gofile_url=f"https://gofile.io/d/x#file={name}",
        )
        assert job_id is not None
        return job_id

    def test_release_job_downloads_removes_dir_and_clears_local_path(self) -> None:
        job_id = self._enqueue("a.mp4")
        job_dir = self.download_dir / f"job-{job_id}"
        job_dir.mkdir()
        (job_dir / "a.mp4").write_bytes(b"x" * 1024)
        self.queue.update_file(job_id, local_path=str(job_dir / "a.mp4"))

        removed = release_job_downloads(self.settings, self.queue, job_id, str(job_dir / "a.mp4"))

        self.assertFalse(job_dir.exists())
        self.assertIn(str(job_dir), removed)
        record = self.queue.get_file(job_id)
        assert record is not None
        self.assertIsNone(record.local_path)

    def test_purge_stale_job_dirs_keeps_active_job(self) -> None:
        active_id = self._enqueue("active.mp4")
        stale_id = self._enqueue("stale.mp4")
        self.queue.update_file(active_id, status=FileStatus.DOWNLOADING)
        self.queue.update_file(stale_id, status=FileStatus.PENDING)

        active_dir = self.download_dir / f"job-{active_id}"
        stale_dir = self.download_dir / f"job-{stale_id}"
        active_dir.mkdir()
        stale_dir.mkdir()
        (stale_dir / "leftover.mp4").write_bytes(b"data")

        removed = purge_stale_job_dirs(
            self.settings, self.queue, keep_job_id=active_id
        )

        self.assertTrue(active_dir.exists())
        self.assertFalse(stale_dir.exists())
        self.assertEqual(removed, [str(stale_dir)])

    def test_purge_removes_failed_job_dir(self) -> None:
        job_id = self._enqueue("failed.mp4")
        self.queue.mark_failed(job_id, "boom", retry=False)
        job_dir = self.download_dir / f"job-{job_id}"
        job_dir.mkdir()
        (job_dir / "failed.mp4").write_bytes(b"data")

        removed = purge_stale_job_dirs(self.settings, self.queue)

        self.assertFalse(job_dir.exists())
        self.assertEqual(removed, [str(job_dir)])

    def test_purge_keeps_pending_job_with_local_path(self) -> None:
        job_id = self._enqueue("resume.mp4")
        job_dir = self.download_dir / f"job-{job_id}"
        job_dir.mkdir()
        (job_dir / "resume.mp4").write_bytes(b"data")
        self.queue.update_file(
            job_id,
            status=FileStatus.PENDING,
            local_path=str(job_dir / "resume.mp4"),
        )

        removed = purge_stale_job_dirs(self.settings, self.queue)

        self.assertTrue(job_dir.exists())
        self.assertEqual(removed, [])

    def test_cleanup_job_files_only(self) -> None:
        job_id = self._enqueue("only.mp4")
        job_dir = self.download_dir / f"job-{job_id}"
        job_dir.mkdir()
        removed = cleanup_job_files(self.settings, job_id)
        self.assertFalse(job_dir.exists())
        self.assertEqual(len(removed), 1)


if __name__ == "__main__":
    unittest.main()
