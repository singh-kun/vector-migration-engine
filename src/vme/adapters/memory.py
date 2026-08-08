"""Deterministic in-memory adapters used by examples and contract tests."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from vme.adapters.base import DestinationAdapter, SourceAdapter
from vme.domain.models import (
    AdapterCapabilities,
    BatchLimit,
    BatchWriteResult,
    CollectionSpec,
    CountQuality,
    CountResult,
    DenseVector,
    IdKind,
    MetricKind,
    MetricSpec,
    MigrationPlan,
    Normalization,
    ReadBatch,
    RecordScope,
    ScopedId,
    ScoreOrder,
    SourcePartition,
    VectorFieldSpec,
    VectorKind,
    VectorRecord,
)
from vme.errors import FatalAdapterError, TransientAdapterError

ALL_METRICS = frozenset(metric for metric in MetricKind if metric is not MetricKind.UNKNOWN)
ALL_VECTOR_KINDS = frozenset(VectorKind)
ALL_ID_KINDS = frozenset(IdKind)


def _capabilities(*, source: bool, destination: bool) -> AdapterCapabilities:
    return AdapterCapabilities(
        adapter_name="memory",
        source=source,
        destination=destination,
        vector_kinds=ALL_VECTOR_KINDS,
        metrics=ALL_METRICS,
        id_kinds=ALL_ID_KINDS,
        named_vectors=True,
        nested_metadata=True,
        array_metadata=True,
        namespaces=True,
        tenants=True,
        partitions=True,
        stable_cursor=True,
        snapshot_read=True,
        exact_count=True,
        read_by_id=True,
        idempotent_upsert=True,
        max_batch_records=10_000,
        max_batch_bytes=64 * 1024 * 1024,
    )


class MemorySourceAdapter(SourceAdapter):
    def __init__(self, spec: CollectionSpec, records: Sequence[VectorRecord]) -> None:
        self.spec = spec
        self._records: dict[str, list[VectorRecord]] = {}
        self._scopes: dict[str, RecordScope] = {}
        for record in records:
            partition_key = record.scope.key
            self._records.setdefault(partition_key, []).append(record)
            self._scopes[partition_key] = record.scope
        if not self._records:
            self._records["default"] = []
            self._scopes["default"] = RecordScope()

    async def probe(self) -> AdapterCapabilities:
        return _capabilities(source=True, destination=False)

    async def discover(self) -> CollectionSpec:
        return self.spec

    async def partitions(self) -> Sequence[SourcePartition]:
        return [SourcePartition(key=key, scope=self._scopes[key]) for key in sorted(self._records)]

    async def read_batch(
        self,
        partition: SourcePartition,
        cursor: Any | None,
        limit: BatchLimit,
    ) -> ReadBatch:
        offset = int(cursor or 0)
        records = self._records[partition.key]
        selected: list[VectorRecord] = []
        selected_bytes = 0
        while offset + len(selected) < len(records) and len(selected) < limit.max_records:
            candidate = records[offset + len(selected)]
            if selected and selected_bytes + candidate.estimated_bytes > limit.max_bytes:
                break
            selected.append(candidate)
            selected_bytes += candidate.estimated_bytes
        next_offset = offset + len(selected)
        return ReadBatch(selected, next_offset, next_offset >= len(records))

    async def count(self, partition: SourcePartition) -> CountResult:
        return CountResult(len(self._records[partition.key]), CountQuality.EXACT)


class MemoryDestinationAdapter(DestinationAdapter):
    def __init__(self) -> None:
        self.spec: CollectionSpec | None = None
        self.records: dict[str, VectorRecord] = {}
        self.write_calls = 0
        self.fail_next_writes = 0
        self.fail_fatally_after_writes: int | None = None

    async def probe(self) -> AdapterCapabilities:
        return _capabilities(source=False, destination=True)

    async def prepare(self, plan: MigrationPlan, *, resume: bool = False) -> None:
        self.spec = plan.target

    async def write_batch(self, records: Sequence[VectorRecord]) -> BatchWriteResult:
        if self.fail_next_writes:
            self.fail_next_writes -= 1
            raise TransientAdapterError("injected transient write failure")
        if (
            self.fail_fatally_after_writes is not None
            and self.write_calls >= self.fail_fatally_after_writes
        ):
            raise FatalAdapterError("injected fatal write failure")
        self.write_calls += 1
        for record in records:
            self.records[record.scoped_id.key] = record
        return BatchWriteResult([record.scoped_id for record in records])

    async def read_by_ids(self, ids: Sequence[ScopedId]) -> Sequence[VectorRecord]:
        return [self.records[item.key] for item in ids if item.key in self.records]

    async def count(self, scope: RecordScope) -> CountResult:
        value = sum(record.scope == scope for record in self.records.values())
        return CountResult(value, CountQuality.EXACT)

    def corrupt_document(self, scoped_id: ScopedId, value: str) -> None:
        old = self.records[scoped_id.key]
        self.records[scoped_id.key] = VectorRecord(
            id=old.id,
            vectors=old.vectors,
            scope=old.scope,
            document=value,
            metadata=old.metadata,
            source_version=old.source_version,
        )


def default_memory_spec(name: str, count: int = 0) -> CollectionSpec:
    metric = MetricSpec(MetricKind.COSINE, ScoreOrder.HIGHER_IS_BETTER, Normalization.NONE)
    field = VectorFieldSpec("default", VectorKind.DENSE, 3, "float32", metric)
    return CollectionSpec(
        name=name,
        vector_fields={"default": field},
        id_kind=IdKind.STRING,
        estimated_count=count,
    )


def _record_from_mapping(item: Mapping[str, Any]) -> VectorRecord:
    raw_id = item["id"]
    scope_raw = item.get("scope", {})
    scope = RecordScope(
        namespace=scope_raw.get("namespace"),
        tenant=scope_raw.get("tenant"),
        partition=scope_raw.get("partition"),
    )
    vectors = {
        name: DenseVector(tuple(float(value) for value in values))
        for name, values in item["vectors"].items()
    }
    return VectorRecord(
        id=raw_id,
        vectors=vectors,
        scope=scope,
        document=item.get("document"),
        metadata=dict(item.get("metadata", {})),
    )


def memory_source_factory(config: Mapping[str, Any]) -> MemorySourceAdapter:
    records = [_record_from_mapping(item) for item in config.get("records", [])]
    if records:
        sample = records[0]
        fields = {
            name: VectorFieldSpec(
                name,
                VectorKind.DENSE,
                vector.dimension if isinstance(vector, DenseVector) else None,
                getattr(vector, "dtype", "unknown"),
                MetricSpec(MetricKind.COSINE, ScoreOrder.HIGHER_IS_BETTER, Normalization.NONE),
            )
            for name, vector in sample.vectors.items()
        }
        id_kind = _infer_id_kind(sample.id)
        spec = CollectionSpec(
            name=str(config.get("collection", "memory-source")),
            vector_fields=fields,
            id_kind=id_kind,
            scope_kinds=frozenset(
                kind
                for kind in ("namespace", "tenant", "partition")
                if any(getattr(record.scope, kind) is not None for record in records)
            ),
            estimated_count=len(records),
        )
    else:
        spec = default_memory_spec(str(config.get("collection", "memory-source")))
    return MemorySourceAdapter(spec, records)


def memory_destination_factory(config: Mapping[str, Any]) -> MemoryDestinationAdapter:
    return MemoryDestinationAdapter()


def _infer_id_kind(value: str | int) -> IdKind:
    if isinstance(value, int):
        return IdKind.INTEGER
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError):
        return IdKind.STRING
    return IdKind.UUID
