"""Durable service resource models kept independent from the HTTP framework."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ProfileRole(StrEnum):
    SOURCE = "source"
    DESTINATION = "destination"
    BOTH = "both"


class PlanStatus(StrEnum):
    QUEUED = "queued"
    PLANNING = "planning"
    READY = "ready"
    REJECTED = "rejected"
    FAILED = "failed"


class ServiceJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    STOPPED = "stopped"
    RECOVERABLE_FAILED = "recoverable_failed"
    TERMINAL_FAILED = "terminal_failed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class DesiredState(StrEnum):
    RUNNING = "running"
    STOPPED = "stopped"
    CANCELLED = "cancelled"


class WorkspaceRole(StrEnum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"


@dataclass(frozen=True, slots=True)
class Actor:
    subject: str
    workspace_id: str
    role: WorkspaceRole


@dataclass(frozen=True, slots=True)
class ConnectionProfile:
    id: str
    workspace_id: str
    name: str
    adapter: str
    role: ProfileRole
    connection: dict[str, Any]
    revision: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class MigrationDefinition:
    id: str
    workspace_id: str
    name: str
    specification: dict[str, Any]
    revision: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class PlanRecord:
    id: str
    workspace_id: str
    migration_id: str
    migration_revision: int
    status: PlanStatus
    fingerprint: str | None
    plan: dict[str, Any] | None
    error: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class JobRecord:
    id: str
    workspace_id: str
    plan_id: str
    status: ServiceJobStatus
    desired_state: DesiredState
    error: str | None
    report: dict[str, Any] | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class LeaseClaim:
    resource_id: str
    workspace_id: str
    lease_token: str
