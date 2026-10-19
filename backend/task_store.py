"""SQLite schema and connection setup for persistent task state."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path


SCHEMA_VERSION = 5
_MIGRATION_LOCK = threading.RLock()


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _backfill_idempotency(connection: sqlite3.Connection) -> None:
    """Keep the oldest legacy row for a duplicate key, matching find_submission_task."""
    rows = connection.execute(
        "SELECT id, project_id, payload, batch_id FROM tasks "
        "WHERE json_extract(payload, '$.submission_receipt.key') IS NOT NULL "
        "ORDER BY project_id, created_at, rowid"
    ).fetchall()
    seen: set[tuple[str, str]] = set()
    for row in rows:
        try:
            payload = json.loads(row[2])
            key = (payload.get("submission_receipt") or {}).get("key")
        except (TypeError, ValueError, json.JSONDecodeError):
            key = None
        if not key:
            continue
        identity = (str(row[1] or ""), str(key))
        if identity in seen:
            continue
        seen.add(identity)
        connection.execute(
            "INSERT OR IGNORE INTO task_idempotency(project_id, idempotency_key, task_id, batch_id) VALUES (?, ?, ?, ?)",
            (*identity, row[0], row[3]),
        )


def _migrate(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, status TEXT NOT NULL,
            prompt_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
            stop_requested INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL,
            result TEXT, error TEXT, batch_id TEXT, sequence INTEGER, project_id TEXT
        )"""
    )
    columns = _columns(connection, "tasks")
    for column, declaration in (("batch_id", "TEXT"), ("sequence", "INTEGER"), ("project_id", "TEXT")):
        if column not in columns:
            connection.execute(f"ALTER TABLE tasks ADD COLUMN {column} {declaration}")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS resource_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, timestamp REAL NOT NULL,
            gpu_free_mib INTEGER, gpu_total_mib INTEGER, ram_free_gib REAL,
            queue_running INTEGER, queue_pending INTEGER
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS project_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            asset_id TEXT NOT NULL DEFAULT '',
            record_type TEXT NOT NULL,
            revision INTEGER NOT NULL,
            source TEXT NOT NULL,
            created_at REAL NOT NULL,
            data TEXT NOT NULL,
            UNIQUE(project_id, asset_id, record_type, revision)
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_tasks_project_asset ON tasks(project_id, asset_id, created_at DESC)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_tasks_project_updated ON tasks(project_id, updated_at DESC, id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_records_project_type ON project_records(project_id, record_type, asset_id, revision DESC)")
    connection.execute("CREATE TABLE IF NOT EXISTS user_settings (owner TEXT PRIMARY KEY, revision INTEGER NOT NULL, guide TEXT NOT NULL)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS login_failure_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            identity TEXT NOT NULL,
            source TEXT NOT NULL,
            failed_at REAL NOT NULL
        )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_login_failure_events_key_time "
        "ON login_failure_events(identity, source, failed_at)"
    )

    # The mapping table is deliberately separate from the frozen payload.  Legacy
    # databases may contain duplicate payload keys; the oldest row remains the
    # canonical retry target and later rows stay readable as historical attempts.
    connection.execute(
        """CREATE TABLE IF NOT EXISTS task_idempotency (
            project_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            task_id TEXT PRIMARY KEY,
            batch_id TEXT,
            UNIQUE(project_id, idempotency_key)
        )"""
    )
    if "batch_id" not in _columns(connection, "task_idempotency"):
        connection.execute("ALTER TABLE task_idempotency ADD COLUMN batch_id TEXT")
    connection.execute("UPDATE task_idempotency SET batch_id=(SELECT batch_id FROM tasks WHERE tasks.id=task_id) WHERE batch_id IS NULL")
    _backfill_idempotency(connection)
    connection.execute("DROP TRIGGER IF EXISTS tasks_idempotency_after_insert")
    connection.execute("DROP TRIGGER IF EXISTS tasks_idempotency_after_payload_update")
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS tasks_idempotency_after_insert
        AFTER INSERT ON tasks
        WHEN json_extract(NEW.payload, '$.submission_receipt.key') IS NOT NULL
             AND (NEW.batch_id IS NULL OR NOT EXISTS (
                 SELECT 1 FROM task_idempotency
                 WHERE project_id = COALESCE(NEW.project_id, '')
                   AND idempotency_key = json_extract(NEW.payload, '$.submission_receipt.key')
                   AND batch_id IS NEW.batch_id
             ))
        BEGIN
            INSERT INTO task_idempotency(project_id, idempotency_key, task_id, batch_id)
            VALUES (COALESCE(NEW.project_id, ''), json_extract(NEW.payload, '$.submission_receipt.key'), NEW.id, NEW.batch_id);
        END"""
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS tasks_idempotency_after_payload_update
        AFTER UPDATE OF payload, project_id ON tasks
        WHEN json_extract(NEW.payload, '$.submission_receipt.key') IS NOT NULL
             AND (NEW.batch_id IS NULL OR NOT EXISTS (
                 SELECT 1 FROM task_idempotency
                 WHERE project_id = COALESCE(NEW.project_id, '')
                   AND idempotency_key = json_extract(NEW.payload, '$.submission_receipt.key')
                   AND batch_id IS NEW.batch_id
             ))
        BEGIN
            INSERT INTO task_idempotency(project_id, idempotency_key, task_id, batch_id)
            VALUES (COALESCE(NEW.project_id, ''), json_extract(NEW.payload, '$.submission_receipt.key'), NEW.id, NEW.batch_id)
            ON CONFLICT(task_id) DO UPDATE SET project_id=excluded.project_id, idempotency_key=excluded.idempotency_key;
        END"""
    )
    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def configure(connection: sqlite3.Connection, database_path: str | Path | None = None) -> sqlite3.Connection:
    """Configure one connection and run the repeatable schema migration."""
    connection.row_factory = sqlite3.Row
    with _MIGRATION_LOCK:
        # WAL mode changes the database header and must be serialized before
        # concurrent worker connections begin their first write.
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=5000")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version < SCHEMA_VERSION:
            _migrate(connection)
            connection.commit()
    return connection
