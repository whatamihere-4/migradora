"""SQLite schema and connection helpers."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gofile_content_id TEXT NOT NULL UNIQUE,
    gofile_path TEXT NOT NULL,
    filename TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    download_link TEXT,
    sha256 TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    local_path TEXT,
    filester_slug TEXT DEFAULT '[]',
    parent_folder_path TEXT DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    is_part INTEGER NOT NULL DEFAULT 0,
    parent_file_id INTEGER,
    part_index INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (parent_file_id) REFERENCES files(id)
);

CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_parent ON files(parent_file_id);

CREATE TABLE IF NOT EXISTS folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gofile_path TEXT NOT NULL UNIQUE,
    filester_folder_id TEXT NOT NULL,
    filester_folder_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    state TEXT NOT NULL DEFAULT 'running',
    notes TEXT
);

CREATE TABLE IF NOT EXISTS bandwidth_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    traffic_downloaded_bytes INTEGER,
    storage_used_bytes INTEGER,
    source TEXT NOT NULL,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS queue_control (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state TEXT NOT NULL DEFAULT 'running',
    pause_reason TEXT,
    updated_at TEXT NOT NULL
);

INSERT OR IGNORE INTO queue_control (id, state, updated_at)
VALUES (1, 'running', datetime('now'));
"""


def connect(db_path: str | Path, timeout: float = 30.0) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str | Path) -> sqlite3.Connection:
    conn = connect(db_path)
    conn.executescript(SCHEMA_SQL)
    row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()
    return conn


def row_to_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    return dict(row)
