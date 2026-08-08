"""Bounded, resumable migration orchestration."""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

from vme.adapters.base import DestinationAdapter, SourceAdapter
from vme.domain.models import (
    BatchLimit,
    BinaryVector,
    CountQuality,
    DenseVector,
    IdKind,
    JobStatus,
    MigrationPlan,
    MultiVector,
    SparseVector,
    VectorKind,
    VectorRecord,
)
from vme.errors import (
    FatalAdapterError,
    MigrationRunError,
    PlanRejectedError,
    RecordValidationError,
    ThrottledAdapterError,
    TransientAdapterError,
    VerificationError,
    redact_text,
)
from vme.execution.transforms import RecordTransformer
from vme.state.sqlite import SQLiteStateStore
from vme.verification.digests import record_digest
from vme.verification.verifier import VerificationResult, Verifier

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 8
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("retry delays cannot be negative")


@dataclass(frozen=True, slots=True)
class ExecutionOptions:
    max_batch_records: int = 500
    max_batch_bytes: int = 8 * 1024 * 1024
    partition_concurrency: int = 2
    writer_concurrency: int = 4
    sample_size: int = 1_000
    target_batch_latency_seconds: float = 1.0
    retry: RetryPolicy = field(default_factory=RetryPolicy)

    def __post_init__(self) -> None:
        values = (
            self.max_batch_records,
            self.max_batch_bytes,
            self.partition_concurrency,
            self.writer_concurrency,
        )
        if any(value <= 0 for value in values):
            raise ValueError("batch and concurrency settings must be positive")
        if self.sample_size < 0:
            raise ValueError("sample_size cannot be negative")


@dataclass(frozen=True, slots=True)
class RunSummary:
    job_id: str
    status: JobStatus
    records_written: int
    bytes_written: int
    batches_written: int
    verification: VerificationResult


class AdaptiveBatchController:
    def __init__(self, initial: int, maximum: int, target_latency: float) -> None:
        self.current = min(initial, maximum)
        self.maximum = maximum
        self.target_latency = target_latency

    def observe(self, latency_seconds: float) -> None:
        if latency_seconds > self.target_latency * 2 and self.current > 1:
            self.current = max(1, self.current // 2)
        elif latency_seconds < self.target_latency * 0.5 and self.current < self.maximum:
            self.current = min(self.maximum, max(self.current + 1, int(self.current * 1.25)))


class MigrationExecutor:
    def __init__(
        self,
        *,
        source: SourceAdapter,
        destination: DestinationAdapter,
        state: SQLiteStateStore,
        options: ExecutionOptions | None = None,
        verifier: Verifier | None = None,
    ) -> None:
        self.source = source
        self.destination = destination
        self.state = state
        self.options = options or ExecutionOptions()
        self.verifier = verifier or Verifier()
        self._writer_slots = asyncio.Semaphore(self.options.writer_concurrency)

    async def run(self, plan: MigrationPlan, *, job_id: str | None = None) -> RunSummary:
        if not plan.executable:
            errors = "; ".join(
                f"{finding.code}: {finding.message}"
                for finding in plan.findings
                if finding.severity.value == "error"
            )
            raise PlanRejectedError(errors)

        resuming = job_id is not None
        if job_id is None:
            job_id = self.state.create_job(plan)
        else:
            self.state.assert_resume(job_id, plan)

        try:
            await self.destination.prepare(plan, resume=resuming)
            self.state.set_status(job_id, JobStatus.PREPARED)
            partitions = await self.source.partitions()
            for partition in partitions:
                self.state.ensure_partition(job_id, partition.key, partition.scope)
            exact_source_count = await self._source_exact_count(partitions)
            if exact_source_count is not None:
                self.state.set_expected_records(job_id, exact_source_count)

            self.state.set_status(job_id, JobStatus.COPYING)
            partition_slots = asyncio.Semaphore(self.options.partition_concurrency)

            async def migrate_partition(partition: Any) -> None:
                async with partition_slots:
                    await self._migrate_partition(job_id, plan, partition)

            await asyncio.gather(*(migrate_partition(partition) for partition in partitions))
            await self.destination.finalize()
            self.state.set_status(job_id, JobStatus.VERIFYING)

            job = self.state.get_job(job_id)
            expected_count = (
                job.expected_records if job.expected_records is not None else job.records_written
            )
            if job.expected_records is not None and job.records_written != job.expected_records:
                raise VerificationError(
                    f"source count was {job.expected_records}, but only "
                    f"{job.records_written} records were transferred"
                )
            verification = await self.verifier.verify(
                job_id=job_id,
                expected_count=expected_count,
                scopes=[partition.scope for partition in partitions],
                destination=self.destination,
                state=self.state,
            )
            if not verification.passed:
                raise VerificationError(
                    "destination verification failed: "
                    f"count={verification.destination_count}/{verification.expected_count}, "
                    f"missing_samples={verification.missing_samples}, "
                    f"mismatched_samples={verification.mismatched_samples}"
                )

            self.state.set_status(job_id, JobStatus.COMPLETED)
            completed = self.state.get_job(job_id)
            return RunSummary(
                job_id=job_id,
                status=completed.status,
                records_written=completed.records_written,
                bytes_written=completed.bytes_written,
                batches_written=completed.batches_written,
                verification=verification,
            )
        except asyncio.CancelledError:
            self.state.set_status(job_id, JobStatus.FAILED, "migration cancelled")
            raise
        except Exception as error:
            self.state.set_status(job_id, JobStatus.FAILED, _safe_error(error))
            raise MigrationRunError(job_id, error) from error
        finally:
            await asyncio.gather(
                self.source.close(),
                self.destination.close(),
                return_exceptions=True,
            )

    async def _source_exact_count(self, partitions: Sequence[Any]) -> int | None:
        results = await asyncio.gather(*(self.source.count(partition) for partition in partitions))
        if any(
            result.quality is not CountQuality.EXACT or result.value is None for result in results
        ):
            return None
        return sum(int(result.value) for result in results if result.value is not None)

    async def _migrate_partition(self, job_id: str, plan: MigrationPlan, partition: Any) -> None:
        snapshot = self.state.get_partition(job_id, partition.key)
        if snapshot.exhausted:
            return

        transformer = RecordTransformer(plan.mapping)
        maximum_records = min(
            self.options.max_batch_records,
            plan.source_capabilities.max_batch_records,
            plan.destination_capabilities.max_batch_records,
        )
        maximum_bytes = min(
            self.options.max_batch_bytes,
            plan.source_capabilities.max_batch_bytes,
            plan.destination_capabilities.max_batch_bytes,
        )
        controller = AdaptiveBatchController(
            initial=maximum_records,
            maximum=maximum_records,
            target_latency=self.options.target_batch_latency_seconds,
        )
        cursor = snapshot.cursor
        exhausted = snapshot.exhausted
        while not exhausted:
            read = await self._retry(
                lambda: self.source.read_batch(
                    partition,
                    cursor,
                    BatchLimit(controller.current, maximum_bytes),
                )
            )
            if not read.records and not read.exhausted:
                raise FatalAdapterError(
                    f"source returned an empty, non-exhausted batch for partition {partition.key!r}"
                )
            transformed = tuple(transformer.transform(record) for record in read.records)
            for record in transformed:
                _validate_for_target(record, plan)

            started = time.monotonic()
            if transformed:
                async with self._writer_slots:
                    result = await self._retry(lambda: self.destination.write_batch(transformed))
                expected = {record.scoped_id.key for record in transformed}
                accepted = {item.key for item in result.accepted}
                if accepted != expected or len(result.accepted) != len(transformed):
                    raise FatalAdapterError(
                        "destination acknowledgement did not match the submitted record IDs"
                    )
            controller.observe(time.monotonic() - started)

            byte_count = sum(record.estimated_bytes for record in transformed)
            samples = [
                (
                    hashlib.sha256(record.scoped_id.key.encode("utf-8")).hexdigest(),
                    record.scoped_id,
                    record_digest(record),
                )
                for record in transformed
            ]
            self.state.commit_batch(
                job_id=job_id,
                partition_key=partition.key,
                next_cursor=read.next_cursor,
                exhausted=read.exhausted,
                record_count=len(transformed),
                byte_count=byte_count,
                samples=samples,
                sample_limit=self.options.sample_size,
            )
            cursor = read.next_cursor
            exhausted = read.exhausted

    async def _retry(self, operation: Callable[[], Awaitable[T]]) -> T:
        for attempt in range(1, self.options.retry.max_attempts + 1):
            try:
                return await operation()
            except ThrottledAdapterError as error:
                if attempt >= self.options.retry.max_attempts:
                    raise
                delay = error.retry_after_seconds
                if delay is None:
                    delay = self._retry_delay(attempt)
                await asyncio.sleep(delay)
            except TransientAdapterError:
                if attempt >= self.options.retry.max_attempts:
                    raise
                await asyncio.sleep(self._retry_delay(attempt))
        raise AssertionError("retry loop exhausted without returning or raising")

    def _retry_delay(self, attempt: int) -> float:
        maximum = min(
            self.options.retry.max_delay_seconds,
            self.options.retry.base_delay_seconds * (2 ** (attempt - 1)),
        )
        return random.uniform(0, maximum) if maximum else 0


def _validate_for_target(record: VectorRecord, plan: MigrationPlan) -> None:
    expected_fields = set(plan.target.vector_fields)
    if set(record.vectors) != expected_fields:
        raise RecordValidationError(
            f"record {record.id!r} vector fields do not match target schema"
        )
    for name, vector in record.vectors.items():
        spec = plan.target.vector_fields[name]
        kind, dimension = _vector_shape(vector)
        if kind is not spec.kind:
            raise RecordValidationError(
                f"record {record.id!r} vector {name!r} has kind {kind.value}, "
                f"expected {spec.kind.value}"
            )
        if spec.dimension is not None and dimension != spec.dimension:
            raise RecordValidationError(
                f"record {record.id!r} vector {name!r} has dimension {dimension}, "
                f"expected {spec.dimension}"
            )
    _validate_id(record, plan.target.id_kind)
    if (
        not plan.destination_capabilities.nested_metadata
        and _contains_nested_mapping(record.metadata)
    ):
        raise RecordValidationError(
            f"record {record.id!r} contains nested metadata unsupported by destination"
        )
    if not plan.destination_capabilities.array_metadata and _contains_array(record.metadata):
        raise RecordValidationError(
            f"record {record.id!r} contains array metadata unsupported by destination"
        )


def _vector_shape(vector: Any) -> tuple[VectorKind, int | None]:
    if isinstance(vector, DenseVector):
        return VectorKind.DENSE, vector.dimension
    if isinstance(vector, SparseVector):
        return VectorKind.SPARSE, vector.dimension
    if isinstance(vector, BinaryVector):
        return VectorKind.BINARY, vector.dimension_bits
    if isinstance(vector, MultiVector):
        first = vector.rows[0]
        dimension = first.dimension if isinstance(first, DenseVector) else first.dimension_bits
        return VectorKind.MULTI, dimension
    raise RecordValidationError(f"unknown vector value type {type(vector)!r}")


def _validate_id(record: VectorRecord, id_kind: IdKind) -> None:
    if id_kind is IdKind.INTEGER and not isinstance(record.id, int):
        raise RecordValidationError(f"record ID {record.id!r} is not an integer")
    if id_kind is IdKind.STRING and not isinstance(record.id, str):
        raise RecordValidationError(f"record ID {record.id!r} is not a string")
    if id_kind is IdKind.UUID:
        try:
            uuid.UUID(str(record.id))
        except ValueError as error:
            raise RecordValidationError(f"record ID {record.id!r} is not a UUID") from error


def _contains_nested_mapping(value: Mapping[str, Any]) -> bool:
    return any(isinstance(item, Mapping) for item in value.values())


def _contains_array(value: Mapping[str, Any]) -> bool:
    return any(isinstance(item, Sequence) and not isinstance(item, str) for item in value.values())


def _safe_error(error: BaseException) -> str:
    text = redact_text(str(error)).replace("\n", " ")
    return f"{type(error).__name__}: {text[:500]}"
