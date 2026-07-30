"""Tests for in-memory job log buffers."""

from __future__ import annotations

import unittest

from migradora.job_log import JobLogBuffer, JobLogStore


class JobLogBufferTests(unittest.TestCase):
    def test_ring_buffer(self) -> None:
        buf = JobLogBuffer(limit=3)
        buf.append("a")
        buf.append("b")
        buf.append("c")
        buf.append("d")
        self.assertEqual(buf.snapshot(), ["b", "c", "d"])

    def test_snapshot_tail(self) -> None:
        buf = JobLogBuffer()
        for i in range(10):
            buf.append(str(i))
        self.assertEqual(buf.snapshot(tail=3), ["7", "8", "9"])


class JobLogStoreTests(unittest.TestCase):
    def test_evicts_oldest_job(self) -> None:
        store = JobLogStore(max_jobs=2)
        store.append(1, "one")
        store.append(2, "two")
        store.append(3, "three")
        self.assertEqual(store.get(1), [])
        self.assertEqual(store.get(2), ["two"])
        self.assertEqual(store.get(3), ["three"])


if __name__ == "__main__":
    unittest.main()
