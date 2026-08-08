"""Native Qdrant source and destination adapter for dense vectors."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from vme.adapters._common import sdk_call
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
    ScoreOrder,
    ScopedId,
    SourcePartition,
    VectorFieldSpec,
    VectorKind,
    VectorRecord,
)
from vme.errors import AdapterConfigurationError, FatalAdapterError


class QdrantAdapter(SourceAdapter, DestinationAdapter):
    def __init__(self, config: Mapping[str, Any]) -> None:
        self.config = dict(config)
        self.collection_name = str(self.config.get("collection", ""))
        if not self.collection_name:
            raise AdapterConfigurationError("Qdrant config requires resource.collection")
        self.document_field = str(self.config.get("document_field", "_vme_document"))
        self._client_instance: Any | None = None
        self._models: Any | None = None
        self._target_vector_names: tuple[str, ...] = ()
        self._target_named = False

    async def probe(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            adapter_name="qdrant",
            source=True,
            destination=True,
            vector_kinds=frozenset({VectorKind.DENSE}),
            metrics=frozenset(
                {
                    MetricKind.COSINE,
                    MetricKind.DOT,
                    MetricKind.EUCLIDEAN,
                    MetricKind.MANHATTAN,
                }
            ),
            id_kinds=frozenset({IdKind.INTEGER, IdKind.UUID}),
            metric_specs={
                MetricKind.COSINE: MetricSpec(
                    MetricKind.COSINE,
                    ScoreOrder.HIGHER_IS_BETTER,
                    Normalization.AUTOMATIC,
                ),
                MetricKind.DOT: MetricSpec(
                    MetricKind.DOT,
                    ScoreOrder.HIGHER_IS_BETTER,
                    Normalization.NONE,
                ),
                MetricKind.EUCLIDEAN: MetricSpec(
                    MetricKind.EUCLIDEAN,
                    ScoreOrder.LOWER_IS_BETTER,
                    Normalization.NONE,
                ),
                MetricKind.MANHATTAN: MetricSpec(
                    MetricKind.MANHATTAN,
                    ScoreOrder.LOWER_IS_BETTER,
                    Normalization.NONE,
                ),
            },
            named_vectors=True,
            nested_metadata=True,
            array_metadata=True,
            stable_cursor=True,
            snapshot_read=False,
            exact_count=True,
            read_by_id=True,
            idempotent_upsert=True,
            max_batch_records=int(self.config.get("max_batch_records", 1_000)),
            max_batch_bytes=int(self.config.get("max_batch_bytes", 32 * 1024 * 1024)),
        )

    async def discover(self) -> CollectionSpec:
        client = await self._client()
        info = await sdk_call(client.get_collection, self.collection_name)
        vectors_config = info.config.params.vectors
        fields = self._fields_from_config(vectors_config)
        points, _ = await self._scroll(limit=1, offset=None)
        if points:
            id_kind = _qdrant_id_kind(points[0].id)
        else:
            configured = str(self.config.get("id_kind", "uuid"))
            id_kind = IdKind(configured)
            if id_kind is IdKind.STRING:
                raise AdapterConfigurationError("Qdrant string IDs must be UUIDs")
        count = await self.count(SourcePartition("default"))
        return CollectionSpec(
            name=self.collection_name,
            vector_fields=fields,
            id_kind=id_kind,
            supports_nested_metadata=True,
            supports_array_metadata=True,
            estimated_count=count.value,
        )

    async def partitions(self) -> Sequence[SourcePartition]:
        return [SourcePartition("default")]

    async def read_batch(
        self,
        partition: SourcePartition,
        cursor: Any | None,
        limit: BatchLimit,
    ) -> ReadBatch:
        points, next_offset = await self._scroll(limit=limit.max_records, offset=cursor)
        records = [self._point_to_record(point) for point in points]
        records = _bounded_prefix(records, limit.max_bytes)
        if len(records) < len(points):
            next_offset = records[-1].id if records else cursor
        return ReadBatch(records, next_offset, next_offset is None)

    async def count(self, partition: SourcePartition | RecordScope) -> CountResult:
        client = await self._client()
        result = await sdk_call(client.count, self.collection_name, exact=True)
        return CountResult(int(result.count), CountQuality.EXACT)

    async def prepare(self, plan: MigrationPlan, *, resume: bool = False) -> None:
        client = await self._client()
        models = self._models
        assert models is not None
        exists = bool(await sdk_call(client.collection_exists, self.collection_name))
        self._target_vector_names = tuple(plan.target.vector_fields)
        self._target_named = len(self._target_vector_names) > 1 or self._target_vector_names != (
            "default",
        )
        if exists:
            if not resume and not bool(self.config.get("allow_existing", False)):
                raise FatalAdapterError(
                    f"Qdrant collection {self.collection_name!r} already exists; "
                    "set allow_existing only for a verified resume/upsert"
                )
            return

        vector_params = {
            name: models.VectorParams(
                size=_required_dimension(field.dimension, name),
                distance=_metric_to_qdrant(field.metric.kind, models),
            )
            for name, field in plan.target.vector_fields.items()
        }
        vectors_config: Any
        if self._target_named:
            vectors_config = vector_params
        else:
            vectors_config = vector_params["default"]
        await sdk_call(
            client.create_collection,
            collection_name=self.collection_name,
            vectors_config=vectors_config,
        )

    async def write_batch(self, records: Sequence[VectorRecord]) -> BatchWriteResult:
        client = await self._client()
        models = self._models
        assert models is not None
        points = []
        for record in records:
            if self.document_field in record.metadata:
                raise FatalAdapterError(
                    f"metadata field {self.document_field!r} conflicts with configured "
                    "document field"
                )
            payload = dict(record.metadata)
            if record.document is not None:
                payload[self.document_field] = record.document
            vectors = {name: _dense_values(value) for name, value in record.vectors.items()}
            vector_payload: Any = vectors if self._target_named else vectors["default"]
            points.append(models.PointStruct(id=record.id, vector=vector_payload, payload=payload))
        await sdk_call(
            client.upsert,
            collection_name=self.collection_name,
            points=points,
            wait=True,
        )
        return BatchWriteResult([record.scoped_id for record in records])

    async def read_by_ids(self, ids: Sequence[ScopedId]) -> Sequence[VectorRecord]:
        if not ids:
            return []
        client = await self._client()
        points = await sdk_call(
            client.retrieve,
            collection_name=self.collection_name,
            ids=[item.id for item in ids],
            with_payload=True,
            with_vectors=True,
        )
        return [self._point_to_record(point) for point in points]

    async def close(self) -> None:
        client = self._client_instance
        close = getattr(client, "close", None)
        if callable(close):
            await sdk_call(close)

    async def _client(self) -> Any:
        if self._client_instance is not None:
            return self._client_instance
        try:
            from qdrant_client import QdrantClient, models
        except ImportError as error:
            raise AdapterConfigurationError(
                "Qdrant adapter requires `pip install vector-migration-engine[qdrant]`"
            ) from error
        self._models = models
        kwargs: dict[str, Any] = {
            "url": str(self.config.get("url", "http://localhost:6333")),
            "timeout": float(self.config.get("timeout", 30.0)),
        }
        if self.config.get("api_key"):
            kwargs["api_key"] = str(self.config["api_key"])
        self._client_instance = await sdk_call(QdrantClient, **kwargs)
        return self._client_instance

    async def _scroll(self, *, limit: int, offset: Any | None) -> tuple[Sequence[Any], Any | None]:
        client = await self._client()
        points, next_offset = await sdk_call(
            client.scroll,
            collection_name=self.collection_name,
            limit=limit,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        return points, next_offset

    def _fields_from_config(self, config: Any) -> dict[str, VectorFieldSpec]:
        if isinstance(config, Mapping):
            return {
                str(name): _qdrant_field(str(name), params)
                for name, params in config.items()
            }
        return {"default": _qdrant_field("default", config)}

    def _point_to_record(self, point: Any) -> VectorRecord:
        payload = dict(point.payload or {})
        document = payload.pop(self.document_field, None)
        raw_vectors = point.vector
        if isinstance(raw_vectors, Mapping):
            vectors = {str(name): DenseVector(values) for name, values in raw_vectors.items()}
        else:
            vectors = {"default": DenseVector(raw_vectors)}
        return VectorRecord(
            id=point.id,
            vectors=vectors,
            document=document,
            metadata=payload,
        )


def _qdrant_field(name: str, params: Any) -> VectorFieldSpec:
    size = int(getattr(params, "size"))
    distance = getattr(params, "distance")
    metric = _qdrant_metric(distance)
    datatype = getattr(params, "datatype", None)
    dtype = str(getattr(datatype, "value", datatype) or "float32").lower()
    return VectorFieldSpec(name, VectorKind.DENSE, size, dtype, metric)


def _qdrant_metric(value: Any) -> MetricSpec:
    normalized = str(getattr(value, "value", value)).lower().split(".")[-1]
    if normalized == "cosine":
        return MetricSpec(MetricKind.COSINE, ScoreOrder.HIGHER_IS_BETTER, Normalization.AUTOMATIC)
    if normalized == "dot":
        return MetricSpec(MetricKind.DOT, ScoreOrder.HIGHER_IS_BETTER, Normalization.NONE)
    if normalized in {"euclid", "euclidean"}:
        return MetricSpec(MetricKind.EUCLIDEAN, ScoreOrder.LOWER_IS_BETTER, Normalization.NONE)
    if normalized == "manhattan":
        return MetricSpec(MetricKind.MANHATTAN, ScoreOrder.LOWER_IS_BETTER, Normalization.NONE)
    raise AdapterConfigurationError(f"unsupported Qdrant distance {value!r}")


def _metric_to_qdrant(metric: MetricKind, models: Any) -> Any:
    mapping = {
        MetricKind.COSINE: models.Distance.COSINE,
        MetricKind.DOT: models.Distance.DOT,
        MetricKind.EUCLIDEAN: models.Distance.EUCLID,
    }
    if hasattr(models.Distance, "MANHATTAN"):
        mapping[MetricKind.MANHATTAN] = models.Distance.MANHATTAN
    try:
        return mapping[metric]
    except KeyError as error:
        raise FatalAdapterError(f"Qdrant cannot create metric {metric.value!r}") from error


def _qdrant_id_kind(value: str | int) -> IdKind:
    if isinstance(value, int):
        return IdKind.INTEGER
    try:
        uuid.UUID(str(value))
    except ValueError as error:
        raise AdapterConfigurationError(
            f"Qdrant returned a non-UUID string ID {value!r}"
        ) from error
    return IdKind.UUID


def _required_dimension(value: int | None, name: str) -> int:
    if value is None:
        raise FatalAdapterError(f"Qdrant requires a known dimension for vector {name!r}")
    return value


def _dense_values(value: Any) -> Sequence[float]:
    if not isinstance(value, DenseVector):
        raise FatalAdapterError("Qdrant MVP1 supports dense vectors only")
    return value.values


def _bounded_prefix(records: Sequence[VectorRecord], max_bytes: int) -> list[VectorRecord]:
    selected: list[VectorRecord] = []
    selected_bytes = 0
    for record in records:
        if selected and selected_bytes + record.estimated_bytes > max_bytes:
            break
        selected.append(record)
        selected_bytes += record.estimated_bytes
    return selected
