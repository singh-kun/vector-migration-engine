"""Typed errors used to make retry and failure behavior deterministic."""

from __future__ import annotations

import re


class VMEError(Exception):
    """Base class for all public VME failures."""


class ConfigurationError(VMEError):
    """Configuration is missing, malformed, or unsafe."""


class AdapterError(VMEError):
    """Base class for connector failures."""


class AdapterConfigurationError(AdapterError):
    """A connector cannot be configured for the selected endpoint."""


class TransientAdapterError(AdapterError):
    """A connector operation may succeed when retried."""


class ThrottledAdapterError(TransientAdapterError):
    """The destination asked the caller to reduce request rate."""

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class FatalAdapterError(AdapterError):
    """A connector operation must not be retried automatically."""


class RecordValidationError(VMEError):
    """A record violates the canonical or destination contract."""


class PlanRejectedError(VMEError):
    """A migration plan contains one or more blocking findings."""


class StateConflictError(VMEError):
    """Durable state does not match the requested migration."""


class ResourceNotFoundError(VMEError):
    """A requested service or migration resource does not exist."""


class VerificationError(VMEError):
    """The destination failed an enabled verification check."""


class MigrationRunError(VMEError):
    """A run failed after a durable job was created."""

    def __init__(self, job_id: str, cause: BaseException) -> None:
        super().__init__(f"migration job {job_id} failed: {cause}")
        self.job_id = job_id
        self.cause = cause


class MigrationStoppedError(VMEError):
    """A run stopped cooperatively after its last durable checkpoint."""

    def __init__(self, job_id: str) -> None:
        super().__init__(f"migration job {job_id} stopped at a durable checkpoint")
        self.job_id = job_id


class WorkerLeaseLostError(VMEError):
    """A stale worker must stop before it can advance durable migration state."""


_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|token|password|authorization)\b(\s*[:=]\s*)([^\s,;]+)"
)
_BEARER_TOKEN = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+")


def redact_text(value: str) -> str:
    """Remove common credential forms before errors reach logs or durable state."""
    value = _SECRET_ASSIGNMENT.sub(r"\1\2<redacted>", value)
    return _BEARER_TOKEN.sub("Bearer <redacted>", value)
