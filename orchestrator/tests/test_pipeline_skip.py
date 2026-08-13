"""Tests for pipeline skip interrupting in-flight I/O."""

from __future__ import annotations

import tempfile
import unittest
from unittest.mock import MagicMock

from migradora.config import Settings
from migradora.pipeline import PipelineCoordinator
from migradora.queue.manager import QueueManager


class PipelineSkipTests(unittest.TestCase):
    def test_request_skip_closes_registered_io(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue = QueueManager(f"{tmp}/queue.db")
            settings = Settings()
            pipeline = PipelineCoordinator(settings, queue)
            closer = MagicMock()
            pipeline._active_io_closers.append(closer)

            pipeline.request_skip(42)

            closer.assert_called_once()
