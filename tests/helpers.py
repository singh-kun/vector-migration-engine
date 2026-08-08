from __future__ import annotations

from vme.adapters.memory import default_memory_spec
from vme.domain.models import DenseVector, RecordScope, VectorRecord


def records(count: int, *, partitions: int = 1) -> list[VectorRecord]:
    return [
        VectorRecord(
            id=f"record-{index}",
            vectors={"default": DenseVector((float(index), 0.5, 1.0))},
            scope=RecordScope(
                partition=f"partition-{index % partitions}" if partitions > 1 else None
            ),
            document=f"document {index}",
            metadata={"index": index, "tags": ["test", str(index % 2)]},
        )
        for index in range(count)
    ]


def spec(count: int, *, partitions: int = 1):
    base = default_memory_spec("source", count)
    if partitions == 1:
        return base
    return type(base)(
        name=base.name,
        vector_fields=base.vector_fields,
        id_kind=base.id_kind,
        scope_kinds=frozenset({"partition"}),
        supports_nested_metadata=base.supports_nested_metadata,
        supports_array_metadata=base.supports_array_metadata,
        estimated_count=count,
    )
