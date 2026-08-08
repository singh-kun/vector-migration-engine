"""Provider adapter contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any

from vme.domain.models import (
    AdapterCapabilities,
    BatchLimit,
    BatchWriteResult,
    CollectionSpec,
    CountResult,
    MigrationPlan,
    ReadBatch,
    RecordScope,
    ScopedId,
    SourcePartition,
    VectorRecord,
)


class SourceAdapter(ABC):
    @abstractmethod
    async def probe(self) -> AdapterCapabilities: ...

    @abstractmethod
    async def discover(self) -> CollectionSpec: ...

    @abstractmethod
    async def partitions(self) -> Sequence[SourcePartition]: ...

    @abstractmethod
    async def read_batch(
        self,
        partition: SourcePartition,
        cursor: Any | None,
        limit: BatchLimit,
    ) -> ReadBatch: ...

    @abstractmethod
    async def count(self, partition: SourcePartition) -> CountResult: ...

    async def close(self) -> None:
        return None


class DestinationAdapter(ABC):
    @abstractmethod
    async def probe(self) -> AdapterCapabilities: ...

    @abstractmethod
    async def prepare(self, plan: MigrationPlan, *, resume: bool = False) -> None: ...

    @abstractmethod
    async def write_batch(self, records: Sequence[VectorRecord]) -> BatchWriteResult: ...

    @abstractmethod
    async def read_by_ids(self, ids: Sequence[ScopedId]) -> Sequence[VectorRecord]: ...

    @abstractmethod
    async def count(self, scope: RecordScope) -> CountResult: ...

    async def finalize(self) -> None:
        return None

    async def close(self) -> None:
        return None
