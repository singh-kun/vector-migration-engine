"""Native Chroma source and destination adapter."""

from __future__ import annotations

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


class ChromaAdapter(SourceAdapter, DestinationAdapter):
    def __init__(self, config: Mapping[str, Any]) -> None:
        self.config = dict(config)
        self.collection_name = str(self.config.get("collection", ""))
        if not self.collection_name:
            raise AdapterConfigurationError("Chroma config requires resource.collection")
        self._client_instance: Any | None = None
        self._collection_instance: Any | None = None
        self._target_vector_name = "default"

    async def probe(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            adapter_name="chroma",
            source=True,
            destination=True,
            vector_kinds=frozenset({VectorKind.DENSE}),
            metrics=frozenset(
                {MetricKind.COSINE, MetricKind.DOT, MetricKind.SQUARED_EUCLIDEAN}
            ),
            id_kinds=frozenset({IdKind.STRING}),
            metric_specs={
                MetricKind.COSINE: _chroma_metric("cosine"),
                MetricKind.DOT: _chroma_metric("ip"),
                MetricKind.SQUARED_EUCLIDEAN: _chroma_metric("l2"),
            },
            named_vectors=False,
            nested_metadata=False,
            array_metadata=True,
            stable_cursor=True,
            snapshot_read=False,
            exact_count=True,
            read_by_id=True,
            idempotent_upsert=True,
            max_batch_records=int(self.config.get("max_batch_records", 5_000)),
            max_batch_bytes=int(self.config.get("max_batch_bytes", 16 * 1024 * 1024)),
        )

    async def discover(self) -> CollectionSpec:
        collection = await self._collection()
        count = int(await sdk_call(collection.count))
        sample = await sdk_call(
            collection.get,
            limit=1,
            offset=0,
            include=["embeddings", "documents", "metadatas"],
        )
        embeddings = _result_list(sample, "embeddings")
        dimension = len(embeddings[0]) if embeddings else self.config.get("dimension")
        metric = self._discover_metric(collection)
        field = VectorFieldSpec(
            "default",
            VectorKind.DENSE,
            int(dimension) if dimension is not None else None,
            "float32",
            metric,
        )
        return CollectionSpec(
            name=self.collection_name,
            vector_fields={"default": field},
            id_kind=IdKind.STRING,
            supports_nested_metadata=False,
            supports_array_metadata=True,
            estimated_count=count,
        )

    async def partitions(self) -> Sequence[SourcePartition]:
        return [SourcePartition("default")]

    async def read_batch(
        self,
        partition: SourcePartition,
        cursor: Any | None,
        limit: BatchLimit,
    ) -> ReadBatch:
        collection = await self._collection()
        offset = int(cursor or 0)
        result = await sdk_call(
            collection.get,
            limit=limit.max_records,
            offset=offset,
            include=["embeddings", "documents", "metadatas"],
        )
        ids = _result_list(result, "ids")
        embeddings = _result_list(result, "embeddings")
        documents = _result_list(result, "documents") or [None] * len(ids)
        metadatas = _result_list(result, "metadatas") or [None] * len(ids)
        records = [
            VectorRecord(
                id=str(record_id),
                vectors={"default": DenseVector(vector)},
                document=documents[index],
                metadata=dict(metadatas[index] or {}),
            )
            for index, (record_id, vector) in enumerate(zip(ids, embeddings, strict=True))
        ]
        selected: list[VectorRecord] = []
        selected_bytes = 0
        for record in records:
            if selected and selected_bytes + record.estimated_bytes > limit.max_bytes:
                break
            selected.append(record)
            selected_bytes += record.estimated_bytes
        records = selected
        next_offset = offset + len(records)
        total = int(await sdk_call(collection.count))
        return ReadBatch(records, next_offset, next_offset >= total)

    async def count(self, partition: SourcePartition | RecordScope) -> CountResult:
        collection = await self._collection()
        return CountResult(int(await sdk_call(collection.count)), CountQuality.EXACT)

    async def prepare(self, plan: MigrationPlan, *, resume: bool = False) -> None:
        if len(plan.target.vector_fields) != 1:
            raise FatalAdapterError("Chroma MVP1 supports exactly one dense vector field")
        field = next(iter(plan.target.vector_fields.values()))
        self._target_vector_name = field.name
        if field.kind is not VectorKind.DENSE:
            raise FatalAdapterError("Chroma MVP1 supports dense vectors only")

        client = await self._client()
        existing = await sdk_call(client.list_collections)
        existing_names = {
            item if isinstance(item, str) else getattr(item, "name", None) for item in existing
        }
        if self.collection_name in existing_names:
            if not resume and not bool(self.config.get("allow_existing", False)):
                raise FatalAdapterError(
                    f"Chroma collection {self.collection_name!r} already exists; "
                    "set allow_existing only for a verified resume/upsert"
                )
            self._collection_instance = await sdk_call(
                client.get_collection, name=self.collection_name
            )
            return

        space = _metric_to_chroma(field.metric.kind)
        try:
            self._collection_instance = await sdk_call(
                client.create_collection,
                name=self.collection_name,
                configuration={"hnsw": {"space": space}},
            )
        except FatalAdapterError as error:
            if "configuration" not in str(error).lower():
                raise
            self._collection_instance = await sdk_call(
                client.create_collection,
                name=self.collection_name,
                metadata={"hnsw:space": space},
            )

    async def write_batch(self, records: Sequence[VectorRecord]) -> BatchWriteResult:
        collection = await self._collection()
        with_document = [record for record in records if record.document is not None]
        without_document = [record for record in records if record.document is None]
        for group in (with_document, without_document):
            if not group:
                continue
            kwargs: dict[str, Any] = {
                "ids": [str(record.id) for record in group],
                "embeddings": [_only_dense(record).values for record in group],
                "metadatas": [dict(record.metadata) for record in group],
            }
            if group is with_document:
                kwargs["documents"] = [record.document for record in group]
            await sdk_call(collection.upsert, **kwargs)
        return BatchWriteResult([record.scoped_id for record in records])

    async def read_by_ids(self, ids: Sequence[ScopedId]) -> Sequence[VectorRecord]:
        if not ids:
            return []
        collection = await self._collection()
        result = await sdk_call(
            collection.get,
            ids=[str(item.id) for item in ids],
            include=["embeddings", "documents", "metadatas"],
        )
        raw_ids = _result_list(result, "ids")
        embeddings = _result_list(result, "embeddings")
        documents = _result_list(result, "documents") or [None] * len(raw_ids)
        metadatas = _result_list(result, "metadatas") or [None] * len(raw_ids)
        return [
            VectorRecord(
                id=str(record_id),
                vectors={self._target_vector_name: DenseVector(embeddings[index])},
                document=documents[index],
                metadata=dict(metadatas[index] or {}),
            )
            for index, record_id in enumerate(raw_ids)
        ]

    async def close(self) -> None:
        client = self._client_instance
        close = getattr(client, "close", None)
        if callable(close):
            await sdk_call(close)

    async def _client(self) -> Any:
        if self._client_instance is not None:
            return self._client_instance
        try:
            import chromadb
        except ImportError as error:
            raise AdapterConfigurationError(
                "Chroma adapter requires `pip install vector-migration-engine[chroma]`"
            ) from error
        if path := self.config.get("path"):
            self._client_instance = await sdk_call(chromadb.PersistentClient, path=str(path))
        else:
            self._client_instance = await sdk_call(
                chromadb.HttpClient,
                host=str(self.config.get("host", "localhost")),
                port=int(self.config.get("port", 8000)),
                ssl=bool(self.config.get("ssl", False)),
                headers=dict(self.config.get("headers", {})),
                tenant=str(self.config.get("tenant", "default_tenant")),
                database=str(self.config.get("database", "default_database")),
            )
        return self._client_instance

    async def _collection(self) -> Any:
        if self._collection_instance is None:
            client = await self._client()
            self._collection_instance = await sdk_call(
                client.get_collection, name=self.collection_name
            )
        return self._collection_instance

    def _discover_metric(self, collection: Any) -> MetricSpec:
        configured = self.config.get("metric")
        if configured:
            return _chroma_metric(str(configured))
        metadata = getattr(collection, "metadata", None) or {}
        if isinstance(metadata, Mapping) and metadata.get("hnsw:space"):
            return _chroma_metric(str(metadata["hnsw:space"]))
        configuration = getattr(collection, "configuration", None)
        if isinstance(configuration, Mapping):
            hnsw = configuration.get("hnsw", {})
            if isinstance(hnsw, Mapping) and hnsw.get("space"):
                return _chroma_metric(str(hnsw["space"]))
        hnsw = getattr(configuration, "hnsw", None)
        space = getattr(hnsw, "space", None)
        if space:
            return _chroma_metric(str(space))
        raise AdapterConfigurationError(
            "could not discover Chroma distance space; set source.metric explicitly"
        )


def _only_dense(record: VectorRecord) -> DenseVector:
    if len(record.vectors) != 1:
        raise FatalAdapterError("Chroma records must contain exactly one vector")
    vector = next(iter(record.vectors.values()))
    if not isinstance(vector, DenseVector):
        raise FatalAdapterError("Chroma records must contain a dense vector")
    return vector


def _result_list(result: Mapping[str, Any], key: str) -> Any:
    value = result.get(key)
    return [] if value is None else value


def _chroma_metric(value: str) -> MetricSpec:
    normalized = value.lower()
    if normalized == "cosine":
        return MetricSpec(MetricKind.COSINE, ScoreOrder.LOWER_IS_BETTER, Normalization.AUTOMATIC)
    if normalized in {"ip", "dot", "dotproduct"}:
        return MetricSpec(MetricKind.DOT, ScoreOrder.LOWER_IS_BETTER, Normalization.NONE)
    if normalized in {"l2", "squared_euclidean"}:
        return MetricSpec(
            MetricKind.SQUARED_EUCLIDEAN, ScoreOrder.LOWER_IS_BETTER, Normalization.NONE
        )
    raise AdapterConfigurationError(f"unsupported Chroma metric {value!r}")


def _metric_to_chroma(metric: MetricKind) -> str:
    mapping = {
        MetricKind.COSINE: "cosine",
        MetricKind.DOT: "ip",
        MetricKind.SQUARED_EUCLIDEAN: "l2",
    }
    try:
        return mapping[metric]
    except KeyError as error:
        raise FatalAdapterError(f"Chroma cannot create metric {metric.value!r}") from error
