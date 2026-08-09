"""Cooperative cancellation helpers."""

from __future__ import annotations

import time
from typing import Callable


def interruptible_sleep(
    seconds: float,
    skip_check: Callable[[], None] | None = None,
) -> None:
    """Sleep in short slices so ``skip_check`` can abort promptly."""
    if seconds <= 0:
        return
    if skip_check is None:
        time.sleep(seconds)
        return
    deadline = time.monotonic() + seconds
    while True:
        skip_check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(1.0, remaining))
