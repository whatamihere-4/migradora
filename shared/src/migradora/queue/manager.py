"""SQLite-backed queue manager for file mirroring jobs."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

from migradora.db import init_db, row_to_dict
from migradora.models import FileRecord, FileStatus, QueueState, QueueStats, utc_now


class QueueManager:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        init_db(db_path)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = init_db(self.db_path)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def enqueue_file(
        self,
        gofile_content_id: str,
        gofile_path: str,
        filename: str,
        size_bytes: int,
        gofile_url: str | None = None,
        download_link: str | None = None,
        parent_folder_path: str = "",
        force: bool = False,
        *,
        initial_status: FileStatus = FileStatus.PENDING,
        skip_reason: str | None = None,
    ) -> int | None:
        now = utc_now()
        url = gofile_url or download_link
        status = initial_status.value
        last_error = skip_reason if initial_status == FileStatus.SKIPPED else None
        with self.connection() as conn:
            existing = conn.execute(
                "SELECT id, status FROM files WHERE gofile_content_id = ?",
                (gofile_content_id,),
            ).fetchone()
            if existing:
                if force and existing["status"] == FileStatus.UPLOADED.value:
                    conn.execute(
                        """UPDATE files SET status=?, attempts=0, last_error=NULL,
                           local_path=NULL, filester_slug='[]', updated_at=?
                           WHERE id=?""",
                        (FileStatus.PENDING.value, now, existing["id"]),
                    )
                    return existing["id"]
                return None

            cur = conn.execute(
                """INSERT INTO files (
                    gofile_content_id, gofile_path, filename, size_bytes,
                    download_link, gofile_url, status, parent_folder_path,
                    last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    gofile_content_id,
                    gofile_path,
                    filename,
                    size_bytes,
                    download_link,
                    url,
                    status,
                    parent_folder_path,
                    last_error,
                    now,
                    now,
                ),
            )
            return cur.lastrowid

    def claim_pending_job(self) -> FileRecord | None:
        return self.claim_download_job()

    def get_file(self, file_id: int) -> FileRecord | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
            if not row:
                return None
            return FileRecord.from_row(row)

    def touch_file(self, file_id: int) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE files SET updated_at=? WHERE id=?",
                (utc_now(), file_id),
            )

    def enqueue_part(
        self,
        parent_file_id: int,
        filename: str,
        size_bytes: int,
        local_path: str,
        part_index: int,
        gofile_content_id: str,
        parent_folder_path: str,
    ) -> int:
        now = utc_now()
        with self.connection() as conn:
            cur = conn.execute(
                """INSERT INTO files (
                    gofile_content_id, gofile_path, filename, size_bytes,
                    status, local_path, parent_folder_path, is_part, parent_file_id,
                    part_index, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)""",
                (
                    f"{gofile_content_id}.part{part_index:03d}",
                    f"part/{filename}",
                    filename,
                    size_bytes,
                    FileStatus.DOWNLOADED.value,
                    local_path,
                    parent_folder_path,
                    parent_file_id,
                    part_index,
                    now,
                    now,
                ),
            )
            return cur.lastrowid or 0

    def claim_download_job(self) -> FileRecord | None:
        now = utc_now()
        with self.connection() as conn:
            if not self._is_running(conn):
                return None
            row = conn.execute(
                """SELECT * FROM files
                   WHERE status = ? AND is_part = 0
                   ORDER BY id ASC LIMIT 1""",
                (FileStatus.PENDING.value,),
            ).fetchone()
            if not row:
                return None
            updated = conn.execute(
                """UPDATE files SET status=?, attempts=attempts+1, updated_at=?
                   WHERE id=? AND status=?""",
                (FileStatus.DOWNLOADING.value, now, row["id"], FileStatus.PENDING.value),
            )
            if updated.rowcount == 0:
                return None
            row = conn.execute("SELECT * FROM files WHERE id=?", (row["id"],)).fetchone()
            return FileRecord.from_row(row)

    def claim_upload_job(self) -> FileRecord | None:
        now = utc_now()
        with self.connection() as conn:
            if not self._is_running(conn):
                return None
            row = conn.execute(
                """SELECT * FROM files
                   WHERE status = ?
                   ORDER BY is_part ASC, id ASC LIMIT 1""",
                (FileStatus.DOWNLOADED.value,),
            ).fetchone()
            if not row:
                return None
            updated = conn.execute(
                """UPDATE files SET status=?, attempts=attempts+1, updated_at=?
                   WHERE id=? AND status=?""",
                (
                    FileStatus.UPLOADING.value,
                    now,
                    row["id"],
                    FileStatus.DOWNLOADED.value,
                ),
            )
            if updated.rowcount == 0:
                return None
            row = conn.execute("SELECT * FROM files WHERE id=?", (row["id"],)).fetchone()
            return FileRecord.from_row(row)

    def update_file(
        self,
        file_id: int,
        *,
        status: FileStatus | None = None,
        local_path: str | None = None,
        sha256: str | None = None,
        filester_slug: list[str] | None = None,
        last_error: str | None = None,
        download_link: str | None = None,
        gofile_url: str | None = None,
        jd2_package_name: str | None = None,
    ) -> None:
        fields: list[str] = ["updated_at = ?"]
        values: list[Any] = [utc_now()]
        if status is not None:
            fields.append("status = ?")
            values.append(status.value)
        if local_path is not None:
            fields.append("local_path = ?")
            values.append(local_path)
        if sha256 is not None:
            fields.append("sha256 = ?")
            values.append(sha256)
        if filester_slug is not None:
            fields.append("filester_slug = ?")
            values.append(json.dumps(filester_slug))
        if last_error is not None:
            fields.append("last_error = ?")
            values.append(last_error)
        if download_link is not None:
            fields.append("download_link = ?")
            values.append(download_link)
        if gofile_url is not None:
            fields.append("gofile_url = ?")
            values.append(gofile_url)
        if jd2_package_name is not None:
            fields.append("jd2_package_name = ?")
            values.append(jd2_package_name)
        values.append(file_id)
        with self.connection() as conn:
            conn.execute(
                f"UPDATE files SET {', '.join(fields)} WHERE id = ?",
                values,
            )

    def mark_failed(self, file_id: int, error: str, retry: bool = True) -> None:
        with self.connection() as conn:
            row = conn.execute("SELECT attempts FROM files WHERE id=?", (file_id,)).fetchone()
            if row and retry:
                conn.execute(
                    "UPDATE files SET status=?, last_error=?, updated_at=? WHERE id=?",
                    (FileStatus.PENDING.value, error, utc_now(), file_id),
                )
            else:
                conn.execute(
                    "UPDATE files SET status=?, last_error=?, updated_at=? WHERE id=?",
                    (FileStatus.FAILED.value, error, utc_now(), file_id),
                )

    def mark_skipped(self, file_id: int, reason: str = "Skipped by user") -> None:
        with self.connection() as conn:
            conn.execute(
                """UPDATE files SET status=?, last_error=?, local_path=NULL,
                   updated_at=? WHERE id=? AND is_part=0""",
                (FileStatus.SKIPPED.value, reason, utc_now(), file_id),
            )

    def requeue_job(self, file_id: int, error: str) -> None:
        """Return job to pending without counting the failed attempt."""
        with self.connection() as conn:
            conn.execute(
                """UPDATE files SET status=?, last_error=?,
                   attempts=CASE WHEN attempts > 0 THEN attempts - 1 ELSE 0 END,
                   updated_at=? WHERE id=?""",
                (FileStatus.PENDING.value, error, utc_now(), file_id),
            )

    def reset_failed_jobs(self) -> int:
        """Move failed jobs back to pending (clears attempts and last_error)."""
        with self.connection() as conn:
            cur = conn.execute(
                """UPDATE files SET status=?, attempts=0, last_error=NULL, updated_at=?
                   WHERE status=? AND is_part=0""",
                (FileStatus.PENDING.value, utc_now(), FileStatus.FAILED.value),
            )
            return cur.rowcount

    def get_folder_mapping(self, gofile_path: str) -> str | None:
        record = self.get_folder_mapping_record(gofile_path)
        return record[0] if record else None

    def get_folder_mapping_record(self, gofile_path: str) -> tuple[str, str] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT filester_folder_id, filester_folder_name FROM folders WHERE gofile_path = ?",
                (gofile_path,),
            ).fetchone()
            if not row:
                return None
            return row["filester_folder_id"], row["filester_folder_name"] or ""

    def save_folder_mapping(
        self, gofile_path: str, filester_folder_id: str, filester_folder_name: str
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO folders
                   (gofile_path, filester_folder_id, filester_folder_name, created_at)
                   VALUES (?, ?, ?, ?)""",
                (gofile_path, filester_folder_id, filester_folder_name, utc_now()),
            )

    def clear_flat_folder_mappings(self) -> int:
        """Remove cached mappings from the old flat-folder fallback (names with ' / ')."""
        with self.connection() as conn:
            cur = conn.execute(
                "DELETE FROM folders WHERE instr(filester_folder_name, ' / ') > 0"
            )
            return cur.rowcount

    def clear_all_folder_mappings(self) -> int:
        with self.connection() as conn:
            cur = conn.execute("DELETE FROM folders")
            return cur.rowcount

    def reset_queue(self) -> dict[str, int]:
        """Delete all jobs, folder mappings, and resume the queue."""
        with self.connection() as conn:
            files_deleted = conn.execute("DELETE FROM files").rowcount
            folders_deleted = conn.execute("DELETE FROM folders").rowcount
            conn.execute(
                """UPDATE queue_control SET state=?, pause_reason='', updated_at=?
                   WHERE id=1""",
                (QueueState.RUNNING.value, utc_now()),
            )
        return {
            "files_deleted": files_deleted,
            "folders_deleted": folders_deleted,
        }

    def get_stats(self) -> QueueStats:
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT status, COUNT(*) as cnt, COALESCE(SUM(size_bytes),0) as bytes
                   FROM files WHERE is_part = 0 GROUP BY status"""
            ).fetchall()
            stats = QueueStats()
            for row in rows:
                status = row["status"]
                cnt = row["cnt"]
                stats.total += cnt
                setattr(stats, status, cnt)
            uploaded = conn.execute(
                """SELECT COALESCE(SUM(size_bytes),0) as b FROM files
                   WHERE status=? AND is_part=0""",
                (FileStatus.UPLOADED.value,),
            ).fetchone()
            total_bytes = conn.execute(
                "SELECT COALESCE(SUM(size_bytes),0) as b FROM files WHERE is_part=0"
            ).fetchone()
            stats.uploaded_bytes = uploaded["b"] if uploaded else 0
            stats.total_bytes = total_bytes["b"] if total_bytes else 0
            return stats

    def list_files(self, status: str | None = None, limit: int = 100) -> list[FileRecord]:
        with self.connection() as conn:
            if status:
                rows = conn.execute(
                    """SELECT * FROM files
                       WHERE is_part=0 AND status=? ORDER BY id ASC LIMIT ?""",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM files
                       WHERE is_part=0 ORDER BY id ASC LIMIT ?""",
                    (limit,),
                ).fetchall()
            return [FileRecord.from_row(r) for r in rows]

    def reset_active_jobs(self, exclude_ids: list[int] | None = None) -> int:
        """Return stuck downloading/uploading jobs to pending (e.g. after crash)."""
        exclude_ids = exclude_ids or []
        with self.connection() as conn:
            if exclude_ids:
                placeholders = ",".join("?" * len(exclude_ids))
                cur = conn.execute(
                    f"""UPDATE files SET status=?, updated_at=?
                        WHERE is_part=0 AND status IN (?, ?)
                        AND id NOT IN ({placeholders})""",
                    (
                        FileStatus.PENDING.value,
                        utc_now(),
                        FileStatus.DOWNLOADING.value,
                        FileStatus.UPLOADING.value,
                        *exclude_ids,
                    ),
                )
            else:
                cur = conn.execute(
                    """UPDATE files SET status=?, updated_at=?
                       WHERE is_part=0 AND status IN (?, ?)""",
                    (
                        FileStatus.PENDING.value,
                        utc_now(),
                        FileStatus.DOWNLOADING.value,
                        FileStatus.UPLOADING.value,
                    ),
                )
            return cur.rowcount

    def reset_stale_jobs(
        self, timeout_sec: int, exclude_ids: list[int] | None = None
    ) -> int:
        exclude_ids = exclude_ids or []
        with self.connection() as conn:
            if exclude_ids:
                placeholders = ",".join("?" * len(exclude_ids))
                cur = conn.execute(
                    f"""UPDATE files SET status=?, updated_at=?
                        WHERE is_part=0 AND status IN (?, ?)
                        AND datetime(updated_at) < datetime('now', ? || ' seconds')
                        AND id NOT IN ({placeholders})""",
                    (
                        FileStatus.PENDING.value,
                        utc_now(),
                        FileStatus.DOWNLOADING.value,
                        FileStatus.UPLOADING.value,
                        f"-{timeout_sec}",
                        *exclude_ids,
                    ),
                )
            else:
                cur = conn.execute(
                    """UPDATE files SET status=?, updated_at=?
                       WHERE is_part=0 AND status IN (?, ?)
                       AND datetime(updated_at) < datetime('now', ? || ' seconds')""",
                    (
                        FileStatus.PENDING.value,
                        utc_now(),
                        FileStatus.DOWNLOADING.value,
                        FileStatus.UPLOADING.value,
                        f"-{timeout_sec}",
                    ),
                )
            return cur.rowcount

    def set_queue_state(self, state: QueueState, reason: str = "") -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE queue_control SET state=?, pause_reason=?, updated_at=? WHERE id=1",
                (state.value, reason, utc_now()),
            )

    def get_queue_state(self) -> tuple[QueueState, str]:
        with self.connection() as conn:
            row = conn.execute("SELECT state, pause_reason FROM queue_control WHERE id=1").fetchone()
            if not row:
                return QueueState.RUNNING, ""
            try:
                return QueueState(row["state"]), row["pause_reason"] or ""
            except ValueError:
                return QueueState.RUNNING, ""

    def log_bandwidth(
        self,
        traffic_bytes: int | None,
        storage_bytes: int | None,
        source: str,
        raw: dict[str, Any] | None = None,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """INSERT INTO bandwidth_log
                   (timestamp, traffic_downloaded_bytes, storage_used_bytes, source, raw_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (utc_now(), traffic_bytes, storage_bytes, source, json.dumps(raw or {})),
            )

    def latest_bandwidth(self, source: str) -> dict[str, Any]:
        with self.connection() as conn:
            row = conn.execute(
                """SELECT * FROM bandwidth_log WHERE source=?
                   ORDER BY id DESC LIMIT 1""",
                (source,),
            ).fetchone()
            return row_to_dict(row)

    def _is_running(self, conn: sqlite3.Connection) -> bool:
        row = conn.execute("SELECT state FROM queue_control WHERE id=1").fetchone()
        if not row:
            return True
        return row["state"] == QueueState.RUNNING.value
