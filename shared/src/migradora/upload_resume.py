"""Persist split/upload progress in the job download dir for watchdog resume."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("migradora.upload_resume")

_STATE_NAME = ".upload-resume.json"


@dataclass
class UploadedPart:
    part_index: int
    filename: str
    size_bytes: int
    slug: str
    upload_response: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "part_index": self.part_index,
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "slug": self.slug,
            "upload_response": self.upload_response,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UploadedPart:
        return UploadedPart(
            part_index=int(data["part_index"]),
            filename=str(data["filename"]),
            size_bytes=int(data["size_bytes"]),
            slug=str(data["slug"]),
            upload_response=data.get("upload_response") or {},
        )


@dataclass
class UploadResumeState:
    oshash: str | None = None
    source_path: str | None = None
    was_split: bool = False
    total_parts: int | None = None
    parts: list[UploadedPart] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "oshash": self.oshash,
            "source_path": self.source_path,
            "was_split": self.was_split,
            "total_parts": self.total_parts,
            "parts": [p.to_dict() for p in self.parts],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UploadResumeState:
        parts = [UploadedPart.from_dict(p) for p in data.get("parts") or []]
        total = data.get("total_parts")
        return UploadResumeState(
            oshash=data.get("oshash"),
            source_path=data.get("source_path"),
            was_split=bool(data.get("was_split")),
            total_parts=int(total) if total is not None else None,
            parts=parts,
        )

    def skip_part_indices(self) -> frozenset[int]:
        return frozenset(p.part_index for p in self.parts)

    def uploaded_bytes(self) -> int:
        return sum(p.size_bytes for p in self.parts)

    def upload_complete(self) -> bool:
        if self.total_parts is None:
            return False
        return len(self.parts) >= self.total_parts


def state_path(job_dir: Path) -> Path:
    return job_dir / _STATE_NAME


def load_upload_resume_state(job_dir: Path) -> UploadResumeState | None:
    path = state_path(job_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return UploadResumeState.from_dict(data)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        logger.warning("Could not read upload resume state %s: %s", path, exc)
        return None


def save_upload_resume_state(job_dir: Path, state: UploadResumeState) -> None:
    path = state_path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")


def delete_upload_resume_state(job_dir: Path) -> None:
    path = state_path(job_dir)
    path.unlink(missing_ok=True)
