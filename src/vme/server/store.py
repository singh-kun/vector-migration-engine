"""SQLite service repository with durable queues, leases, and audit events."""

from __future__ import annotations

import enum
import hashlib
import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from vme.errors import ResourceNotFoundError, StateConflictError, redact_text
from vme.server.models import (
    ConnectionProfile,
    DesiredState,
    JobRecord,
    LeaseClaim,
    MigrationDefinition,
    PlanRecord,
    PlanStatus,
    ProfileRole,
    ServiceJobStatus,
)


class SQLiteServiceStore:
    """Own service resources while the engine owns record-level checkpoints."""

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
                CREATE TABLE IF NOT EXISTS service_profiles (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    adapter TEXT NOT NULL,
                    role TEXT NOT NULL,
                    connection_json TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(workspace_id, name)
                );

                CREATE TABLE IF NOT EXISTS service_migrations (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    specification_json TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(workspace_id, name)
                );

                CREATE TABLE IF NOT EXISTS service_plans (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    migration_id TEXT NOT NULL,
                    migration_revision INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    fingerprint TEXT,
                    plan_json TEXT,
                    error TEXT,
                    lease_token TEXT,
                    lease_expires_at TEXT,
                    worker_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(migration_id) REFERENCES service_migrations(id)
                );

                CREATE TABLE IF NOT EXISTS service_jobs (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    desired_state TEXT NOT NULL,
                    error TEXT,
                    report_json TEXT,
                    lease_token TEXT,
                    lease_expires_at TEXT,
                    worker_id TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(plan_id) REFERENCES service_plans(id)
                );

                CREATE TABLE IF NOT EXISTS service_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    workspace_id TEXT NOT NULL,
                    resource_type TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS service_events_resource_idx
                ON service_events(workspace_id, resource_type, resource_id, sequence);

                CREATE TABLE IF NOT EXISTS service_idempotency (
                    workspace_id TEXT NOT NULL,
                    route TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(workspace_id, route, idempotency_key)
                );
                """
            )
            self._ensure_column("service_jobs", "report_json", "TEXT")

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        columns = {row["name"] for row in self._connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            self._connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def create_profile(
        self,
        *,
        workspace_id: str,
        name: str,
        adapter: str,
        role: ProfileRole,
        connection: Mapping[str, Any],
        actor: str,
    ) -> ConnectionProfile:
        profile_id = str(uuid.uuid4())
        now = _utc_now()
        with self._lock, self._connection:
            try:
                self._connection.execute(
                    """
                    INSERT INTO service_profiles(
                        id, workspace_id, name, adapter, role, connection_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        profile_id,
                        workspace_id,
                        name,
                        adapter,
                        role.value,
                        _json(connection),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise StateConflictError(
                    f"connection profile {name!r} already exists in this workspace"
                ) from error
            self._event(
                workspace_id, "profile", profile_id, "profile_created", actor, {"name": name}
            )
        return self.get_profile(workspace_id, profile_id)

    def get_profile(self, workspace_id: str, profile_id: str) -> ConnectionProfile:
        row = self._one(
            "SELECT * FROM service_profiles WHERE workspace_id = ? AND id = ?",
            (workspace_id, profile_id),
            f"connection profile {profile_id} does not exist",
        )
        return _profile(row)

    def list_profiles(self, workspace_id: str) -> Sequence[ConnectionProfile]:
        rows = self._all(
            "SELECT * FROM service_profiles WHERE workspace_id = ? ORDER BY name, id",
            (workspace_id,),
        )
        return [_profile(row) for row in rows]

    def create_migration(
        self,
        *,
        workspace_id: str,
        name: str,
        specification: Mapping[str, Any],
        actor: str,
    ) -> MigrationDefinition:
        migration_id = str(uuid.uuid4())
        now = _utc_now()
        with self._lock, self._connection:
            try:
                self._connection.execute(
                    """
                    INSERT INTO service_migrations(
                        id, workspace_id, name, specification_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (migration_id, workspace_id, name, _json(specification), now, now),
                )
            except sqlite3.IntegrityError as error:
                raise StateConflictError(
                    f"migration {name!r} already exists in this workspace"
                ) from error
            self._event(
                workspace_id,
                "migration",
                migration_id,
                "migration_created",
                actor,
                {"name": name},
            )
        return self.get_migration(workspace_id, migration_id)

    def get_migration(self, workspace_id: str, migration_id: str) -> MigrationDefinition:
        row = self._one(
            "SELECT * FROM service_migrations WHERE workspace_id = ? AND id = ?",
            (workspace_id, migration_id),
            f"migration {migration_id} does not exist",
        )
        return _migration(row)

    def create_plan(self, *, workspace_id: str, migration_id: str, actor: str) -> PlanRecord:
        migration = self.get_migration(workspace_id, migration_id)
        plan_id = str(uuid.uuid4())
        now = _utc_now()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO service_plans(
                    id, workspace_id, migration_id, migration_revision, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    workspace_id,
                    migration_id,
                    migration.revision,
                    PlanStatus.QUEUED.value,
                    now,
                    now,
                ),
            )
            self._event(
                workspace_id, "plan", plan_id, "plan_queued", actor, {"migration_id": migration_id}
            )
        return self.get_plan(workspace_id, plan_id)

    def get_plan(self, workspace_id: str, plan_id: str) -> PlanRecord:
        row = self._one(
            "SELECT * FROM service_plans WHERE workspace_id = ? AND id = ?",
            (workspace_id, plan_id),
            f"plan {plan_id} does not exist",
        )
        return _plan(row)

    def claim_plan(self, worker_id: str, lease_seconds: int) -> LeaseClaim | None:
        now = _utc_now()
        expires = _future(lease_seconds)
        with self._immediate_transaction():
            row = self._connection.execute(
                """
                SELECT id, workspace_id FROM service_plans
                WHERE status = ? OR (status = ? AND lease_expires_at < ?)
                ORDER BY created_at, id LIMIT 1
                """,
                (PlanStatus.QUEUED.value, PlanStatus.PLANNING.value, now),
            ).fetchone()
            if row is None:
                return None
            token = str(uuid.uuid4())
            self._connection.execute(
                """
                UPDATE service_plans
                SET status = ?, lease_token = ?, lease_expires_at = ?, worker_id = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (PlanStatus.PLANNING.value, token, expires, worker_id, now, row["id"]),
            )
            return LeaseClaim(row["id"], row["workspace_id"], token)

    def finish_plan(
        self,
        *,
        plan_id: str,
        lease_token: str,
        fingerprint: str,
        plan: Mapping[str, Any],
        executable: bool,
    ) -> None:
        status = PlanStatus.READY if executable else PlanStatus.REJECTED
        self._lease_update(
            table="service_plans",
            resource_id=plan_id,
            lease_token=lease_token,
            assignments="status = ?, fingerprint = ?, plan_json = ?, error = NULL",
            values=(status.value, fingerprint, _json(plan)),
            event_type=f"plan_{status.value}",
            event_payload={"fingerprint": fingerprint},
        )

    def fail_plan(self, plan_id: str, lease_token: str, error: str) -> None:
        self._lease_update(
            table="service_plans",
            resource_id=plan_id,
            lease_token=lease_token,
            assignments="status = ?, error = ?",
            values=(PlanStatus.FAILED.value, _safe_error(error)),
            event_type="plan_failed",
            event_payload={"error": _safe_error(error)},
        )

    def renew_plan_lease(self, plan_id: str, lease_token: str, lease_seconds: int) -> bool:
        return self._renew_lease(
            table="service_plans",
            resource_id=plan_id,
            lease_token=lease_token,
            status=PlanStatus.PLANNING.value,
            lease_seconds=lease_seconds,
        )

    def create_job(self, *, workspace_id: str, plan_id: str, actor: str) -> JobRecord:
        plan = self.get_plan(workspace_id, plan_id)
        if plan.status is not PlanStatus.READY:
            raise StateConflictError(f"plan {plan_id} is not ready for execution")
        job_id = str(uuid.uuid4())
        now = _utc_now()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO service_jobs(
                    id, workspace_id, plan_id, status, desired_state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    workspace_id,
                    plan_id,
                    ServiceJobStatus.QUEUED.value,
                    DesiredState.RUNNING.value,
                    now,
                    now,
                ),
            )
            self._event(workspace_id, "job", job_id, "job_queued", actor, {"plan_id": plan_id})
        return self.get_job(workspace_id, job_id)

    def get_job(self, workspace_id: str, job_id: str) -> JobRecord:
        row = self._one(
            "SELECT * FROM service_jobs WHERE workspace_id = ? AND id = ?",
            (workspace_id, job_id),
            f"job {job_id} does not exist",
        )
        return _job(row)

    def list_jobs(self, workspace_id: str) -> Sequence[JobRecord]:
        rows = self._all(
            "SELECT * FROM service_jobs WHERE workspace_id = ? ORDER BY created_at DESC, id",
            (workspace_id,),
        )
        return [_job(row) for row in rows]

    def claim_job(self, worker_id: str, lease_seconds: int) -> LeaseClaim | None:
        now = _utc_now()
        expires = _future(lease_seconds)
        with self._immediate_transaction():
            row = self._connection.execute(
                """
                SELECT id, workspace_id FROM service_jobs
                WHERE status = ? AND desired_state = ?
                ORDER BY created_at, id LIMIT 1
                """,
                (ServiceJobStatus.QUEUED.value, DesiredState.RUNNING.value),
            ).fetchone()
            if row is None:
                return None
            token = str(uuid.uuid4())
            self._connection.execute(
                """
                UPDATE service_jobs
                SET status = ?, lease_token = ?, lease_expires_at = ?, worker_id = ?,
                    attempts = attempts + 1, updated_at = ?
                WHERE id = ?
                """,
                (
                    ServiceJobStatus.RUNNING.value,
                    token,
                    expires,
                    worker_id,
                    now,
                    row["id"],
                ),
            )
            return LeaseClaim(row["id"], row["workspace_id"], token)

    def renew_job_lease(self, job_id: str, lease_token: str, lease_seconds: int) -> bool:
        return self._renew_lease(
            table="service_jobs",
            resource_id=job_id,
            lease_token=lease_token,
            status=ServiceJobStatus.RUNNING.value,
            lease_seconds=lease_seconds,
        )

    def job_lease_valid(self, job_id: str, lease_token: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT 1 FROM service_jobs
                WHERE id = ? AND lease_token = ? AND status = ? AND lease_expires_at > ?
                """,
                (job_id, lease_token, ServiceJobStatus.RUNNING.value, _utc_now()),
            ).fetchone()
        return row is not None

    def desired_state(self, job_id: str, lease_token: str) -> DesiredState:
        row = self._one(
            "SELECT desired_state, lease_token FROM service_jobs WHERE id = ?",
            (job_id,),
            f"job {job_id} does not exist",
        )
        if row["lease_token"] != lease_token:
            raise StateConflictError(f"worker no longer owns job {job_id}")
        return DesiredState(row["desired_state"])

    def finish_job(
        self,
        *,
        job_id: str,
        lease_token: str,
        status: ServiceJobStatus,
        error: str | None = None,
        report: Mapping[str, Any] | None = None,
    ) -> None:
        if status not in {
            ServiceJobStatus.COMPLETED,
            ServiceJobStatus.STOPPED,
            ServiceJobStatus.RECOVERABLE_FAILED,
            ServiceJobStatus.TERMINAL_FAILED,
            ServiceJobStatus.CANCELLED,
        }:
            raise ValueError(f"invalid terminal worker status {status.value}")
        self._lease_update(
            table="service_jobs",
            resource_id=job_id,
            lease_token=lease_token,
            assignments="status = ?, error = ?, report_json = ?",
            values=(
                status.value,
                _safe_error(error) if error else None,
                _json(report) if report is not None else None,
            ),
            event_type=f"job_{status.value}",
            event_payload={"error": _safe_error(error) if error else None},
        )

    def request_job_state(
        self,
        *,
        workspace_id: str,
        job_id: str,
        desired: DesiredState,
        actor: str,
    ) -> JobRecord:
        job = self.get_job(workspace_id, job_id)
        terminal = {
            ServiceJobStatus.COMPLETED,
            ServiceJobStatus.CANCELLED,
            ServiceJobStatus.TERMINAL_FAILED,
        }
        if job.status in terminal:
            raise StateConflictError(f"job {job_id} is already {job.status.value}")
        if desired is DesiredState.RUNNING:
            if job.status not in {
                ServiceJobStatus.STOPPED,
                ServiceJobStatus.RECOVERABLE_FAILED,
            }:
                raise StateConflictError(f"job {job_id} cannot resume from {job.status.value}")
            status = ServiceJobStatus.QUEUED
        elif job.status is ServiceJobStatus.QUEUED:
            status = (
                ServiceJobStatus.CANCELLED
                if desired is DesiredState.CANCELLED
                else ServiceJobStatus.STOPPED
            )
        else:
            status = job.status
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE service_jobs SET desired_state = ?, status = ?, updated_at = ?
                WHERE workspace_id = ? AND id = ?
                """,
                (desired.value, status.value, _utc_now(), workspace_id, job_id),
            )
            self._event(
                workspace_id,
                "job",
                job_id,
                f"job_{desired.value}_requested",
                actor,
                {},
            )
        return self.get_job(workspace_id, job_id)

    def recover_expired_jobs(self) -> int:
        now = _utc_now()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE service_jobs
                SET status = ?, error = ?, lease_token = NULL, lease_expires_at = NULL,
                    worker_id = NULL, updated_at = ?
                WHERE status = ? AND lease_expires_at < ?
                """,
                (
                    ServiceJobStatus.RECOVERABLE_FAILED.value,
                    "worker lease expired",
                    now,
                    ServiceJobStatus.RUNNING.value,
                    now,
                ),
            )
        return cursor.rowcount

    def events(
        self,
        *,
        workspace_id: str,
        resource_type: str,
        resource_id: str,
        after: int = 0,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 1000))
        rows = self._all(
            """
            SELECT * FROM service_events
            WHERE workspace_id = ? AND resource_type = ? AND resource_id = ?
                AND sequence > ?
            ORDER BY sequence LIMIT ?
            """,
            (workspace_id, resource_type, resource_id, after, limit),
        )
        return [
            {
                "sequence": row["sequence"],
                "event_type": row["event_type"],
                "actor": row["actor"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def check_idempotency(
        self,
        *,
        workspace_id: str,
        route: str,
        key: str,
        request: Mapping[str, Any],
    ) -> str | None:
        digest = _request_digest(request)
        with self._lock:
            row = self._connection.execute(
                """
                SELECT request_digest, resource_id FROM service_idempotency
                WHERE workspace_id = ? AND route = ? AND idempotency_key = ?
                """,
                (workspace_id, route, key),
            ).fetchone()
        if row is None:
            return None
        if row["request_digest"] != digest:
            raise StateConflictError("idempotency key was already used with a different request")
        return str(row["resource_id"])

    def record_idempotency(
        self,
        *,
        workspace_id: str,
        route: str,
        key: str,
        request: Mapping[str, Any],
        resource_id: str,
    ) -> str:
        digest = _request_digest(request)
        with self._lock, self._connection:
            try:
                self._connection.execute(
                    """
                    INSERT INTO service_idempotency(
                        workspace_id, route, idempotency_key, request_digest,
                        resource_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (workspace_id, route, key, digest, resource_id, _utc_now()),
                )
            except sqlite3.IntegrityError:
                existing = self.check_idempotency(
                    workspace_id=workspace_id, route=route, key=key, request=request
                )
                if existing is None:
                    raise
                return existing
        return resource_id

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _lease_update(
        self,
        *,
        table: str,
        resource_id: str,
        lease_token: str,
        assignments: str,
        values: tuple[Any, ...],
        event_type: str,
        event_payload: Mapping[str, Any],
    ) -> None:
        if table not in {"service_plans", "service_jobs"}:
            raise ValueError("invalid leased resource table")
        with self._lock, self._connection:
            row = self._connection.execute(
                f"SELECT workspace_id FROM {table} WHERE id = ? AND lease_token = ?",
                (resource_id, lease_token),
            ).fetchone()
            if row is None:
                raise StateConflictError(f"worker lease for {resource_id} is no longer valid")
            cursor = self._connection.execute(
                f"""
                UPDATE {table}
                SET {assignments}, lease_token = NULL, lease_expires_at = NULL,
                    worker_id = NULL, updated_at = ?
                WHERE id = ? AND lease_token = ?
                """,
                (*values, _utc_now(), resource_id, lease_token),
            )
            if cursor.rowcount != 1:
                raise StateConflictError(f"worker lease for {resource_id} is no longer valid")
            self._event(
                row["workspace_id"],
                "plan" if table == "service_plans" else "job",
                resource_id,
                event_type,
                "worker",
                event_payload,
            )

    def _renew_lease(
        self,
        *,
        table: str,
        resource_id: str,
        lease_token: str,
        status: str,
        lease_seconds: int,
    ) -> bool:
        if table not in {"service_plans", "service_jobs"}:
            raise ValueError("invalid leased resource table")
        with self._lock, self._connection:
            cursor = self._connection.execute(
                f"""
                UPDATE {table} SET lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND lease_token = ? AND status = ?
                """,
                (_future(lease_seconds), _utc_now(), resource_id, lease_token, status),
            )
        return cursor.rowcount == 1

    def _one(self, sql: str, values: tuple[Any, ...], missing: str) -> sqlite3.Row:
        with self._lock:
            row = self._connection.execute(sql, values).fetchone()
        if row is None:
            raise ResourceNotFoundError(missing)
        return row

    def _all(self, sql: str, values: tuple[Any, ...]) -> list[sqlite3.Row]:
        with self._lock:
            return self._connection.execute(sql, values).fetchall()

    @contextmanager
    def _immediate_transaction(self) -> Iterator[None]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def _event(
        self,
        workspace_id: str,
        resource_type: str,
        resource_id: str,
        event_type: str,
        actor: str,
        payload: Mapping[str, Any],
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO service_events(
                workspace_id, resource_type, resource_id, event_type,
                actor, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workspace_id,
                resource_type,
                resource_id,
                event_type,
                actor,
                _json(payload),
                _utc_now(),
            ),
        )


def _profile(row: sqlite3.Row) -> ConnectionProfile:
    return ConnectionProfile(
        id=row["id"],
        workspace_id=row["workspace_id"],
        name=row["name"],
        adapter=row["adapter"],
        role=ProfileRole(row["role"]),
        connection=json.loads(row["connection_json"]),
        revision=row["revision"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _migration(row: sqlite3.Row) -> MigrationDefinition:
    return MigrationDefinition(
        id=row["id"],
        workspace_id=row["workspace_id"],
        name=row["name"],
        specification=json.loads(row["specification_json"]),
        revision=row["revision"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _plan(row: sqlite3.Row) -> PlanRecord:
    return PlanRecord(
        id=row["id"],
        workspace_id=row["workspace_id"],
        migration_id=row["migration_id"],
        migration_revision=row["migration_revision"],
        status=PlanStatus(row["status"]),
        fingerprint=row["fingerprint"],
        plan=json.loads(row["plan_json"]) if row["plan_json"] else None,
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _job(row: sqlite3.Row) -> JobRecord:
    return JobRecord(
        id=row["id"],
        workspace_id=row["workspace_id"],
        plan_id=row["plan_id"],
        status=ServiceJobStatus(row["status"]),
        desired_state=DesiredState(row["desired_state"]),
        error=row["error"],
        report=json.loads(row["report_json"]) if row["report_json"] else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _future(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


def _request_digest(request: Mapping[str, Any]) -> str:
    return hashlib.sha256(_json(request).encode("utf-8")).hexdigest()


def _safe_error(value: str) -> str:
    return redact_text(value).replace("\n", " ")[:1000]


def to_public_dict(value: Any) -> dict[str, Any]:
    """Serialize service dataclasses for the API without exposing connection values."""

    result = asdict(value)
    if isinstance(value, ConnectionProfile):
        result["connection"] = _redact_references(value.connection)
    for key, item in tuple(result.items()):
        if isinstance(item, enum.Enum):
            result[key] = item.value
    return result


def _redact_references(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _redact_references(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_references(item) for item in value]
    if isinstance(value, str) and value.startswith(("env:", "file:")):
        return {"secret_ref": value.split(":", 1)[0] + ":<redacted>"}
    return value
