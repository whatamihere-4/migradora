"""Tests for upload speed watchdog phase gating."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from migradora.upload_watchdog import run_watchdog_once


class UploadWatchdogPhaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = SimpleNamespace(
            state_dir="/tmp",
            upload_watchdog_enabled=True,
            upload_watchdog_min_mbps=5.0,
            upload_watchdog_sustain_sec=60,
            upload_watchdog_cooldown_sec=0,
        )

    @patch("migradora.upload_watchdog._fetch_status")
    def test_splitting_phase_is_ignored(self, fetch_status) -> None:
        fetch_status.return_value = {
            "pipeline": {
                "phase": "splitting",
                "current_job_id": 7,
                "speed_bps": 1024.0,
            }
        }
        result = run_watchdog_once(self.settings)
        self.assertEqual(result["action"], "idle")
        self.assertFalse(result["restart_required"])

    @patch("migradora.upload_watchdog._fetch_status")
    def test_slow_upload_phase_can_restart(self, fetch_status) -> None:
        fetch_status.return_value = {
            "pipeline": {
                "phase": "uploading",
                "current_job_id": 7,
                "speed_bps": 100 * 1024,
            }
        }
        with patch("migradora.upload_watchdog._load_state", return_value={"low_since": 0}):
            with patch("migradora.upload_watchdog._save_state"):
                with patch("migradora.upload_watchdog.prepare_job_restart", return_value=[]):
                    result = run_watchdog_once(self.settings)
        self.assertEqual(result["action"], "restart")
        self.assertTrue(result["restart_required"])


if __name__ == "__main__":
    unittest.main()
