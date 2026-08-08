"""Deterministic transforms approved by a migration plan."""

from __future__ import annotations

import uuid

from vme.domain.models import IdPolicy, MappingOptions, VectorRecord


class RecordTransformer:
    def __init__(self, mapping: MappingOptions) -> None:
        self.mapping = mapping
        self._uuid_namespace = (
            uuid.UUID(mapping.uuid_namespace) if mapping.uuid_namespace is not None else None
        )

    def transform(self, record: VectorRecord) -> VectorRecord:
        if self.mapping.id_policy is IdPolicy.PRESERVE:
            target_id = record.id
        elif self.mapping.id_policy is IdPolicy.STRINGIFY:
            target_id = str(record.id)
        else:
            assert self._uuid_namespace is not None
            target_id = str(uuid.uuid5(self._uuid_namespace, record.scoped_id.key))

        vectors = {
            self.mapping.vector_name_map.get(name, name): vector
            for name, vector in record.vectors.items()
        }
        return VectorRecord(
            id=target_id,
            vectors=vectors,
            scope=record.scope,
            document=record.document,
            metadata=dict(record.metadata),
            source_version=record.source_version,
        )
