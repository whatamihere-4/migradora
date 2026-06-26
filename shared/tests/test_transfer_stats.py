"""Tests for transfer speed and ETA helpers."""

from __future__ import annotations

import unittest

from migradora.transfer_stats import (
    TransferTracker,
    compute_queue_eta,
    compute_remaining_bytes,
    eta_seconds,
    format_eta,
)


class FormatEtaTests(unittest.TestCase):
    def test_seconds(self) -> None:
        self.assertEqual(format_eta(45), "45s")

    def test_minutes(self) -> None:
        self.assertEqual(format_eta(125), "2m 5s")

    def test_none(self) -> None:
        self.assertEqual(format_eta(None), "—")


class RemainingBytesTests(unittest.TestCase):
    def test_all_pending(self) -> None:
        rem = compute_remaining_bytes(
            incomplete_bytes=1_000_000_000,
            current_job_id=None,
            current_job_size=0,
            phase="idle",
            progress_bytes=0,
            upload_bytes_done=0,
            upload_bytes_total=0,
        )
        self.assertEqual(rem.download_bytes, 1_000_000_000)
        self.assertEqual(rem.upload_bytes, 1_000_000_000)

    def test_mid_download(self) -> None:
        rem = compute_remaining_bytes(
            incomplete_bytes=1_000_000_000,
            current_job_id=1,
            current_job_size=400_000_000,
            phase="downloading",
            progress_bytes=100_000_000,
            upload_bytes_done=0,
            upload_bytes_total=0,
        )
        self.assertEqual(rem.download_bytes, 900_000_000)
        self.assertEqual(rem.upload_bytes, 1_000_000_000)

    def test_mid_upload(self) -> None:
        rem = compute_remaining_bytes(
            incomplete_bytes=1_000_000_000,
            current_job_id=1,
            current_job_size=400_000_000,
            phase="uploading",
            progress_bytes=50_000_000,
            upload_bytes_done=150_000_000,
            upload_bytes_total=400_000_000,
        )
        self.assertEqual(rem.download_bytes, 600_000_000)
        self.assertEqual(rem.upload_bytes, 850_000_000)


class QueueEtaTests(unittest.TestCase):
    def test_combined(self) -> None:
        from migradora.transfer_stats import TransferRemaining

        rem = TransferRemaining(download_bytes=1_000, upload_bytes=2_000)
        result = compute_queue_eta(rem, download_bps=100, upload_bps=200)
        self.assertEqual(result["download_sec"], 10.0)
        self.assertEqual(result["upload_sec"], 10.0)
        self.assertEqual(result["queue_sec"], 20.0)


class TransferTrackerTests(unittest.TestCase):
    def test_records_download_speed(self) -> None:
        tracker = TransferTracker(sample_interval_sec=0)
        tracker.begin_phase("download")
        tracker.complete_phase("download", 1_000_000)
        self.assertIsNotNone(tracker.download_bps)
        self.assertGreater(tracker.download_bps or 0, 0)


class EtaSecondsTests(unittest.TestCase):
    def test_basic(self) -> None:
        self.assertEqual(eta_seconds(1_000, 100), 10.0)

    def test_no_speed(self) -> None:
        self.assertIsNone(eta_seconds(1_000, None))


if __name__ == "__main__":
    unittest.main()
