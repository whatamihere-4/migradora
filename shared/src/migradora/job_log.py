"""In-memory per-job log lines for the WebUI (APU-style job_logs)."""

from __future__ import annotations

import threading
from collections import OrderedDict


class JobLogBuffer:
    """Thread-safe ring buffer of recent log lines for one job."""

    def __init__(self, limit: int = 200) -> None:
        self._limit = max(1, limit)
        self._lines: list[str] = []
        self._lock = threading.Lock()

    def append(self, line: str) -> None:
        line = line.rstrip()
        if not line:
            return
        with self._lock:
            self._lines.append(line)
            if len(self._lines) > self._limit:
                del self._lines[:-self._limit]

    def snapshot(self, tail: int = 22) -> list[str]:
        with self._lock:
            return list(self._lines[-tail:])

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()


class JobLogStore:
    """Retain recent job log buffers for the dashboard."""

    def __init__(self, *, per_job_limit: int = 200, max_jobs: int = 10) -> None:
        self._per_job_limit = per_job_limit
        self._max_jobs = max(1, max_jobs)
        self._buffers: OrderedDict[int, JobLogBuffer] = OrderedDict()
        self._lock = threading.Lock()

    def for_job(self, job_id: int) -> JobLogBuffer:
        with self._lock:
            buf = self._buffers.get(job_id)
            if buf is None:
                buf = JobLogBuffer(limit=self._per_job_limit)
                self._buffers[job_id] = buf
                while len(self._buffers) > self._max_jobs:
                    self._buffers.popitem(last=False)
            else:
                self._buffers.move_to_end(job_id)
            return buf

    def append(self, job_id: int, line: str) -> None:
        self.for_job(job_id).append(line)

    def get(self, job_id: int, *, tail: int = 22) -> list[str]:
        with self._lock:
            buf = self._buffers.get(job_id)
            if not buf:
                return []
            return buf.snapshot(tail=tail)
