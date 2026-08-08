"""SQLite WAL-backed checkpoints with transactional cursor advancement."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vme.domain.models import JobStatus, MigrationPlan, RecordScope, ScopedId, to_jsonable
from vme.errors import StateConflictError
from vme.state.base import JobSnapshot, PartitionSnapshot, SampleExpectation


class SQLiteStateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._configure()
        self._migrate()

    def _configure(self) -> None:
        with self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA busy_timeout=5000")

    def _migrate(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    records_written INTEGER NOT NULL DEFAULT 0,
                    bytes_written INTEGER NOT NULL DEFAULT 0,
                    batches_written INTEGER NOT NULL DEFAULT 0,
                    expected_records INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS partitions (
                    job_id TEXT NOT NULL,
                    partition_key TEXT NOT NULL,
                    scope_json TEXT NOT NULL,
                    cursor_json TEXT,
                    exhausted INTEGER NOT NULL DEFAULT 0,
                    records_written INTEGER NOT NULL DEFAULT 0,
                    bytes_written INTEGER NOT NULL DEFAULT 0,
                    batches_written INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (job_id, partition_key),
                    FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS samples (
                    job_id TEXT NOT NULL,
                    sample_key TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    id_json TEXT NOT NULL,
                    scope_json TEXT NOT NULL,
                    expected_digest TEXT NOT NULL,
                    PRIMARY KEY (job_id, sample_key),
                    FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
                );
                """
            )
            self._ensure_column("jobs", "expected_records", "INTEGER")

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        columns = {row["name"] for row in self._connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            self._connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def create_job(self, plan: MigrationPlan, *, job_id: str | None = None) -> str:
        job_id = job_id or str(uuid.uuid4())
        now = _utc_now()
        plan_json = json.dumps(to_jsonable(plan), sort_keys=True, separators=(",", ":"))
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO jobs(job_id, fingerprint, plan_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (job_id, plan.fingerprint, plan_json, JobStatus.PLANNED.value, now, now),
            )
            self._event(job_id, "job_created", {"fingerprint": plan.fingerprint})
        return job_id

    def assert_resume(self, job_id: str, plan: MigrationPlan) -> JobSnapshot:
        snapshot = self.get_job(job_id)
        if snapshot.fingerprint != plan.fingerprint:
            raise StateConflictError(
                f"job {job_id} fingerprint does not match the current migration plan"
            )
        if snapshot.status is JobStatus.COMPLETED:
            raise StateConflictError(f"job {job_id} is already completed")
        return snapshot

    def get_job(self, job_id: str) -> JobSnapshot:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise StateConflictError(f"migration job {job_id} does not exist")
        return JobSnapshot(
            job_id=row["job_id"],
            fingerprint=row["fingerprint"],
            status=JobStatus(row["status"]),
            records_written=row["records_written"],
            bytes_written=row["bytes_written"],
            batches_written=row["batches_written"],
            expected_records=row["expected_records"],
        )

    def set_expected_records(self, job_id: str, value: int) -> None:
        if value < 0:
            raise ValueError("expected record count cannot be negative")
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT expected_records FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise StateConflictError(f"migration job {job_id} does not exist")
            existing = row["expected_records"]
            if existing is not None and existing != value:
                raise StateConflictError(
                    f"job {job_id} source count changed from {existing} to {value}"
                )
            self._connection.execute(
                "UPDATE jobs SET expected_records = ?, updated_at = ? WHERE job_id = ?",
                (value, _utc_now(), job_id),
            )

    def set_status(self, job_id: str, status: JobStatus, error: str | None = None) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE jobs SET status = ?, error = ?, updated_at = ? WHERE job_id = ?",
                (status.value, error, _utc_now(), job_id),
            )
            self._event(job_id, "status_changed", {"status": status.value, "error": error})

    def ensure_partition(self, job_id: str, key: str, scope: RecordScope) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO partitions(job_id, partition_key, scope_json)
                VALUES (?, ?, ?)
                """,
                (job_id, key, json.dumps(to_jsonable(scope), sort_keys=True)),
            )

    def get_partition(self, job_id: str, key: str) -> PartitionSnapshot:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM partitions WHERE job_id = ? AND partition_key = ?",
                (job_id, key),
            ).fetchone()
        if row is None:
            raise StateConflictError(f"partition {key!r} is not initialized for job {job_id}")
        return PartitionSnapshot(
            key=key,
            cursor=json.loads(row["cursor_json"]) if row["cursor_json"] is not None else None,
            exhausted=bool(row["exhausted"]),
            records_written=row["records_written"],
        )

    def commit_batch(
        self,
        *,
        job_id: str,
        partition_key: str,
        next_cursor: Any,
        exhausted: bool,
        record_count: int,
        byte_count: int,
        samples: Sequence[tuple[str, ScopedId, str]],
        sample_limit: int,
    ) -> None:
        cursor_json = json.dumps(next_cursor, sort_keys=True, separators=(",", ":"))
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE partitions
                SET cursor_json = ?, exhausted = ?,
                    records_written = records_written + ?,
                    bytes_written = bytes_written + ?,
                    batches_written = batches_written + 1
                WHERE job_id = ? AND partition_key = ?
                """,
                (
                    cursor_json,
                    int(exhausted),
                    record_count,
                    byte_count,
                    job_id,
                    partition_key,
                ),
            )
            if cursor.rowcount != 1:
                raise StateConflictError(
                    f"partition {partition_key!r} disappeared while committing checkpoint"
                )
            self._connection.execute(
                """
                UPDATE jobs
                SET records_written = records_written + ?,
                    bytes_written = bytes_written + ?,
                    batches_written = batches_written + 1,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (record_count, byte_count, _utc_now(), job_id),
            )
            for priority, scoped_id, expected_digest in samples:
                self._connection.execute(
                    """
                    INSERT OR REPLACE INTO samples(
                        job_id, sample_key, priority, id_json, scope_json, expected_digest
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        scoped_id.key,
                        priority,
                        json.dumps(scoped_id.id),
                        json.dumps(to_jsonable(scoped_id.scope), sort_keys=True),
                        expected_digest,
                    ),
                )
            if sample_limit >= 0:
                self._connection.execute(
                    """
                    DELETE FROM samples
                    WHERE job_id = ? AND sample_key NOT IN (
                        SELECT sample_key FROM samples
                        WHERE job_id = ? ORDER BY priority ASC LIMIT ?
                    )
                    """,
                    (job_id, job_id, sample_limit),
                )
            self._event(
                job_id,
                "batch_committed",
                {
                    "partition": partition_key,
                    "records": record_count,
                    "bytes": byte_count,
                    "exhausted": exhausted,
                },
            )

    def samples(self, job_id: str) -> Sequence[SampleExpectation]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM samples WHERE job_id = ? ORDER BY priority ASC", (job_id,)
            ).fetchall()
        results: list[SampleExpectation] = []
        for row in rows:
            scope_raw = json.loads(row["scope_json"])
            results.append(
                SampleExpectation(
                    ScopedId(
                        json.loads(row["id_json"]),
                        RecordScope(
                            namespace=scope_raw.get("namespace"),
                            tenant=scope_raw.get("tenant"),
                            partition=scope_raw.get("partition"),
                        ),
                    ),
                    row["expected_digest"],
                )
            )
        return results

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _event(self, job_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self._connection.execute(
            """
            INSERT INTO events(job_id, created_at, event_type, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                job_id,
                _utc_now(),
                event_type,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
            ),
        )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
