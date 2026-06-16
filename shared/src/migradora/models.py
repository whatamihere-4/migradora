"""Data models for the mirroring pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class FileStatus(str, Enum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    SPLITTING = "splitting"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    FAILED = "failed"
    SKIPPED = "skipped"


class QueueState(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"
    PAUSED_TRAFFIC = "paused_traffic"
    PAUSED_STORAGE = "paused_storage"
    PAUSED_DISK = "paused_disk"


@dataclass
class FileRecord:
    id: int
    gofile_content_id: str
    gofile_path: str
    filename: str
    size_bytes: int
    download_link: str | None
    sha256: str | None
    status: FileStatus
    local_path: str | None
    filester_slug: list[str]
    parent_folder_path: str
    attempts: int
    last_error: str | None
    created_at: str
    updated_at: str
    is_part: bool = False
    parent_file_id: int | None = None
    part_index: int | None = None

    @classmethod
    def from_row(cls, row: Any) -> FileRecord:
        import json

        slugs = row["filester_slug"]
        if isinstance(slugs, str):
            slugs = json.loads(slugs) if slugs else []
        return cls(
            id=row["id"],
            gofile_content_id=row["gofile_content_id"],
            gofile_path=row["gofile_path"],
            filename=row["filename"],
            size_bytes=row["size_bytes"],
            download_link=row["download_link"],
            sha256=row["sha256"],
            status=FileStatus(row["status"]),
            local_path=row["local_path"],
            filester_slug=slugs or [],
            parent_folder_path=row["parent_folder_path"] or "",
            attempts=row["attempts"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            is_part=bool(row["is_part"]),
            parent_file_id=row["parent_file_id"],
            part_index=row["part_index"],
        )


@dataclass
class FolderMapping:
    gofile_path: str
    filester_folder_id: str
    filester_folder_name: str
    created_at: str


@dataclass
class BandwidthSnapshot:
    timestamp: str
    traffic_downloaded_bytes: int | None
    storage_used_bytes: int | None
    source: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class QueueStats:
    total: int = 0
    pending: int = 0
    downloading: int = 0
    downloaded: int = 0
    splitting: int = 0
    uploading: int = 0
    uploaded: int = 0
    failed: int = 0
    skipped: int = 0
    total_bytes: int = 0
    uploaded_bytes: int = 0

    @property
    def completion_pct(self) -> float:
        if self.total == 0:
            return 0.0
        return (self.uploaded / self.total) * 100.0


def utc_now() -> str:
    return datetime.utcnow().isoformat() + "Z"
