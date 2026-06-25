"""Remove on-disk files left behind by a queue job."""

from __future__ import annotations

import shutil
from pathlib import Path

from migradora.config import Settings


def cleanup_job_files(
    settings: Settings,
    job_id: int,
    local_path: str | None = None,
) -> list[str]:
    """Delete job download dir and any recorded local_path. Returns removed paths."""
    removed: list[str] = []
    job_dir = Path(settings.download_dir) / f"job-{job_id}"
    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)
        removed.append(str(job_dir))

    if local_path:
        path = Path(local_path)
        if path.exists():
            if path.is_file():
                path.unlink(missing_ok=True)
            else:
                shutil.rmtree(path, ignore_errors=True)
            removed.append(str(path))

    return removed


def clear_all_downloads(download_dir: str) -> list[str]:
    """Remove all job-* directories under the download root."""
    removed: list[str] = []
    base = Path(download_dir)
    if not base.is_dir():
        return removed
    for path in base.iterdir():
        if path.is_dir() and path.name.startswith("job-"):
            shutil.rmtree(path, ignore_errors=True)
            removed.append(str(path))
    return removed
