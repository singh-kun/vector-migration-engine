from __future__ import annotations

import unittest
from types import SimpleNamespace

from vme.adapters.chroma import ChromaAdapter
from vme.adapters.memory import MemorySourceAdapter
from vme.adapters.qdrant import QdrantAdapter
from vme.domain.models import (
    BatchLimit,
    DenseVector,
    IdPolicy,
    MappingOptions,
    ScopedId,
    SourcePartition,
    VectorRecord,
)
from vme.execution.transforms import RecordTransformer
from vme.planning.planner import MigrationPlanner
from vme.verification.digests import record_digest

from tests.helpers import records, spec


class FakeChromaCollection:
    def __init__(self) -> None:
        self.metadata = {"hnsw:space": "cosine"}
        self.rows = {
            "one": ([0.1, 0.2, 0.3], "document one", {"kind": "test"}),
            "two": ([0.4, 0.5, 0.6], None, {"kind": "test"}),
        }

    def count(self):
        return len(self.rows)

    def get(self, ids=None, limit=None, offset=0, include=None):
        keys = list(self.rows) if ids is None else [item for item in ids if item in self.rows]
        if ids is None:
            keys = keys[offset : offset + limit if limit is not None else None]
        return {
            "ids": keys,
            "embeddings": [self.rows[key][0] for key in keys],
            "documents": [self.rows[key][1] for key in keys],
            "metadatas": [self.rows[key][2] for key in keys],
        }

    def upsert(self, ids, embeddings, metadatas, documents=None):
        documents = documents or [None] * len(ids)
        for index, record_id in enumerate(ids):
            self.rows[record_id] = (
                list(embeddings[index]),
                documents[index],
                dict(metadatas[index]),
            )


class FakeQdrantModels:
    class Distance:
        COSINE = "Cosine"
        DOT = "Dot"
        EUCLID = "Euclid"
        MANHATTAN = "Manhattan"

    class VectorParams:
        def __init__(self, *, size, distance):
            self.size = size
            self.distance = distance
            self.datatype = None

    class PointStruct(SimpleNamespace):
        def __init__(self, *, id, vector, payload):
            super().__init__(id=id, vector=vector, payload=payload)


class FakeQdrantClient:
    def __init__(self, *, existing=False, points=None):
        self.existing = existing
        self.points = list(points or [])
        self.created_vectors = None

    def collection_exists(self, name):
        return self.existing

    def create_collection(self, *, collection_name, vectors_config):
        self.existing = True
        self.created_vectors = vectors_config

    def upsert(self, *, collection_name, points, wait):
        by_id = {point.id: point for point in self.points}
        by_id.update({point.id: point for point in points})
        self.points = list(by_id.values())

    def retrieve(self, *, collection_name, ids, with_payload, with_vectors):
        return [point for point in self.points if point.id in ids]

    def count(self, collection_name, exact):
        return SimpleNamespace(count=len(self.points))

    def scroll(self, *, collection_name, limit, offset, with_payload, with_vectors):
        start = int(offset or 0)
        page = self.points[start : start + limit]
        next_offset = start + len(page) if start + len(page) < len(self.points) else None
        return page, next_offset

    def get_collection(self, name):
        vectors = FakeQdrantModels.VectorParams(size=3, distance="Cosine")
        return SimpleNamespace(config=SimpleNamespace(params=SimpleNamespace(vectors=vectors)))


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_chroma_source_reads_vectors_documents_and_metadata(self) -> None:
        adapter = ChromaAdapter({"collection": "source"})
        adapter._collection_instance = FakeChromaCollection()
        discovered = await adapter.discover()
        batch = await adapter.read_batch(SourcePartition("default"), None, BatchLimit(10, 1024))
        self.assertEqual(discovered.estimated_count, 2)
        self.assertEqual(discovered.vector_fields["default"].dimension, 3)
        self.assertEqual(len(batch.records), 2)
        self.assertEqual(batch.records[0].document, "document one")
        self.assertTrue(batch.exhausted)

    async def test_chroma_destination_upsert_is_readable(self) -> None:
        adapter = ChromaAdapter({"collection": "target"})
        adapter._collection_instance = FakeChromaCollection()
        record = VectorRecord(
            id="three",
            vectors={"default": DenseVector((0.7, 0.8, 0.9))},
            document="document three",
            metadata={"kind": "new"},
        )
        await adapter.write_batch([record])
        returned = await adapter.read_by_ids([ScopedId("three")])
        self.assertEqual(record_digest(returned[0]), record_digest(record))

    async def test_qdrant_destination_provisions_and_upserts(self) -> None:
        source = MemorySourceAdapter(spec(1), records(1))
        adapter = QdrantAdapter({"collection": "target"})
        client = FakeQdrantClient()
        adapter._client_instance = client
        adapter._models = FakeQdrantModels
        mapping = MappingOptions(
            id_policy=IdPolicy.DETERMINISTIC_UUID,
            uuid_namespace="60f5f0b8-6574-4b98-9f55-e365efa51e20",
        )
        plan = MigrationPlanner().build(
            source=await source.discover(),
            source_capabilities=await source.probe(),
            destination_capabilities=await adapter.probe(),
            target_name="target",
            mapping=mapping,
        )
        await adapter.prepare(plan)
        transformed = RecordTransformer(mapping).transform(records(1)[0])
        await adapter.write_batch([transformed])
        returned = await adapter.read_by_ids([transformed.scoped_id])
        self.assertEqual(record_digest(returned[0]), record_digest(transformed))
        self.assertEqual(client.created_vectors.size, 3)

    async def test_qdrant_source_discovers_and_scrolls(self) -> None:
        point = FakeQdrantModels.PointStruct(
            id=7,
            vector=[0.1, 0.2, 0.3],
            payload={"_vme_document": "hello", "kind": "test"},
        )
        adapter = QdrantAdapter({"collection": "source"})
        adapter._client_instance = FakeQdrantClient(existing=True, points=[point])
        adapter._models = FakeQdrantModels
        discovered = await adapter.discover()
        batch = await adapter.read_batch(SourcePartition("default"), None, BatchLimit(10, 1024))
        self.assertEqual(discovered.estimated_count, 1)
        self.assertEqual(discovered.id_kind.value, "integer")
        self.assertEqual(batch.records[0].document, "hello")
        self.assertEqual(batch.records[0].metadata, {"kind": "test"})


if __name__ == "__main__":
    unittest.main()
