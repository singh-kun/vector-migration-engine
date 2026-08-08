"""Persistence contract used by the migration executor."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from vme.domain.models import JobStatus, MigrationPlan, RecordScope, ScopedId


@dataclass(frozen=True, slots=True)
class JobSnapshot:
    job_id: str
    fingerprint: str
    status: JobStatus
    records_written: int
    bytes_written: int
    batches_written: int
    expected_records: int | None


@dataclass(frozen=True, slots=True)
class PartitionSnapshot:
    key: str
    cursor: Any | None
    exhausted: bool
    records_written: int


@dataclass(frozen=True, slots=True)
class SampleExpectation:
    scoped_id: ScopedId
    expected_digest: str


class StateStore(Protocol):
    """Durable state operations required by execution and verification."""

    def create_job(self, plan: MigrationPlan, *, job_id: str | None = None) -> str: ...

    def assert_resume(self, job_id: str, plan: MigrationPlan) -> JobSnapshot: ...

    def get_job(self, job_id: str) -> JobSnapshot: ...

    def set_expected_records(self, job_id: str, value: int) -> None: ...

    def set_status(self, job_id: str, status: JobStatus, error: str | None = None) -> None: ...

    def ensure_partition(self, job_id: str, key: str, scope: RecordScope) -> None: ...

    def get_partition(self, job_id: str, key: str) -> PartitionSnapshot: ...

    def commit_batch(
        self,
        *,
        job_id: str,
        partition_key: str,
        next_cursor: Any | None,
        exhausted: bool,
        record_count: int,
        byte_count: int,
        samples: Sequence[tuple[str, ScopedId, str]],
        sample_limit: int,
    ) -> None: ...

    def samples(self, job_id: str) -> Sequence[SampleExpectation]: ...

    def close(self) -> None: ...
