from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from importlib.metadata import version
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vme.server.app import create_app
from vme.server.secrets import SecretResolver
from vme.server.security import EndpointPolicy
from vme.server.settings import ServerSettings
from vme.server.store import SQLiteServiceStore
from vme.server.worker import ServiceWorker

pytestmark = pytest.mark.integration


CORPUS = (
    (
        "checkpoint-recovery",
        "Reliable data migrations persist checkpoints only after a destination batch is "
        "acknowledged, allowing safe replay after crashes.",
        "migration",
    ),
    (
        "vector-schema",
        "Vector database migration must preserve dimensions, distance metrics, identifiers, "
        "metadata, and named vector fields.",
        "migration",
    ),
    (
        "batch-throughput",
        "Bounded batches, controlled concurrency, retry backoff, and idempotent upserts "
        "improve large migration throughput.",
        "migration",
    ),
    (
        "semantic-validation",
        "Semantic validation compares nearest-neighbor search results before and after "
        "moving vector embeddings.",
        "migration",
    ),
    (
        "database-cutover",
        "A safe production cutover verifies counts and query quality before switching the "
        "application alias to the new database.",
        "migration",
    ),
    (
        "embedding-model",
        "An embedding model converts text into dense numerical vectors whose geometry "
        "captures semantic similarity.",
        "machine-learning",
    ),
    (
        "model-evaluation",
        "Machine learning evaluation separates training data from test data and tracks "
        "accuracy, recall, and calibration.",
        "machine-learning",
    ),
    (
        "neural-network",
        "A neural network learns layered representations by optimizing weights with "
        "gradient descent.",
        "machine-learning",
    ),
    (
        "feature-store",
        "A feature store serves consistent machine learning features to offline training "
        "and online inference systems.",
        "machine-learning",
    ),
    (
        "retrieval-augmented",
        "Retrieval augmented generation finds relevant passages and supplies them as context "
        "to a language model.",
        "machine-learning",
    ),
    (
        "kubernetes-scaling",
        "Kubernetes scales container workloads with deployments, replicas, health probes, "
        "and autoscaling policies.",
        "infrastructure",
    ),
    (
        "observability",
        "Production observability combines metrics, structured logs, and distributed traces "
        "for incident diagnosis.",
        "infrastructure",
    ),
    (
        "message-queue",
        "A durable message queue decouples producers from consumers and supports "
        "asynchronous processing.",
        "infrastructure",
    ),
    (
        "zero-downtime",
        "Zero downtime deployment uses readiness checks, gradual traffic shifting, and "
        "automated rollback.",
        "infrastructure",
    ),
    (
        "backup-restore",
        "Backup and restore drills validate recovery objectives before a real infrastructure "
        "failure occurs.",
        "infrastructure",
    ),
    (
        "bread-baking",
        "Bread dough develops flavor through fermentation before baking in a hot oven.",
        "cooking",
    ),
    (
        "pasta-sauce",
        "A tomato pasta sauce balances acidity with olive oil, aromatics, herbs, and slow "
        "simmering.",
        "cooking",
    ),
    (
        "coffee-brewing",
        "Coffee extraction depends on grind size, water temperature, contact time, and the "
        "ratio of water to beans.",
        "cooking",
    ),
    (
        "trail-hiking",
        "A safe mountain hike requires weather checks, navigation, water, layered clothing, "
        "and an emergency plan.",
        "outdoors",
    ),
    (
        "vegetable-garden",
        "Healthy vegetable gardens need fertile soil, sunlight, irrigation, mulch, and "
        "seasonal planting.",
        "outdoors",
    ),
)

QUERIES = (
    ("recover a database migration after a crash", "checkpoint-recovery"),
    ("check nearest neighbor quality after moving embeddings", "semantic-validation"),
    ("how does text become a numeric semantic vector", "embedding-model"),
    ("diagnose a production incident using traces and logs", "observability"),
    ("variables that affect extracting flavor from coffee", "coffee-brewing"),
)


@unittest.skipUnless(
    os.environ.get("VME_RUN_LIVE_INTEGRATION") == "1",
    "set VME_RUN_LIVE_INTEGRATION=1 to run real Chroma/Qdrant integration",
)
class LiveChromaToQdrantTest(unittest.TestCase):
    def test_embedded_migration_preserves_data_and_semantic_search(self) -> None:
        from chromadb import PersistentClient
        from chromadb.api.client import SharedSystemClient
        from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
        from qdrant_client import QdrantClient

        with tempfile.TemporaryDirectory(prefix="vme-live-", ignore_cleanup_errors=True) as root:
            root_path = Path(root)
            chroma_path = root_path / "chroma"
            qdrant_path = root_path / "qdrant"
            source_name = "knowledge_source"
            target_name = "knowledge_target"

            embed = DefaultEmbeddingFunction()
            documents = [document for _, document, _ in CORPUS]
            vectors = [_as_list(vector) for vector in embed(documents)]

            chroma = PersistentClient(path=str(chroma_path))
            source = chroma.create_collection(
                source_name,
                configuration={"hnsw": {"space": "cosine"}},
            )
            source.add(
                ids=[record_id for record_id, _, _ in CORPUS],
                documents=documents,
                embeddings=vectors,
                metadatas=[
                    {"source_id": record_id, "category": category}
                    for record_id, _, category in CORPUS
                ],
            )

            state_path = root_path / "service-state.sqlite3"
            store = SQLiteServiceStore(state_path)
            worker = ServiceWorker(
                store=store,
                state_path=str(state_path),
                resolver=SecretResolver(),
                lease_seconds=30,
                worker_id="live-integration-worker",
                endpoint_policy=EndpointPolicy(
                    allowed_adapters=("chroma", "qdrant"),
                    allowed_data_roots=(root_path,),
                    allowed_endpoints=(),
                    allow_insecure_endpoints=False,
                    allow_embedded_chroma=True,
                ),
            )
            service_settings = ServerSettings(
                state_path=state_path,
                auth_mode="none",
                run_worker=False,
                allowed_data_roots=(root_path,),
                allow_embedded_chroma=True,
            )
            try:
                with TestClient(create_app(service_settings, store=store, worker=worker)) as client:
                    source_profile = _post(
                        client,
                        "/v1/connection-profiles",
                        "live-source-profile",
                        {
                            "name": "live-chroma",
                            "adapter": "chroma",
                            "role": "source",
                            "connection": {"path": str(chroma_path)},
                        },
                        201,
                    )
                    destination_profile = _post(
                        client,
                        "/v1/connection-profiles",
                        "live-destination-profile",
                        {
                            "name": "live-qdrant",
                            "adapter": "qdrant",
                            "role": "destination",
                            "connection": {"path": str(qdrant_path)},
                        },
                        201,
                    )
                    migration = _post(
                        client,
                        "/v1/migrations",
                        "live-migration-definition",
                        {
                            "name": "live-chroma-to-qdrant",
                            "source_profile_id": source_profile["id"],
                            "destination_profile_id": destination_profile["id"],
                            "source_resource": {
                                "collection": source_name,
                                "metric": "cosine",
                            },
                            "destination_resource": {"collection": target_name},
                            "mapping": {
                                "ids": {
                                    "policy": "deterministic_uuid",
                                    "namespace": "60f5f0b8-6574-4b98-9f55-e365efa51e20",
                                }
                            },
                            "execution": {
                                "batch": {"max_records": 7, "max_bytes": 1048576},
                                "concurrency": {"partitions": 1, "writers": 2},
                            },
                            "verification": {"sample": {"records": len(CORPUS)}},
                        },
                        201,
                    )
                    plan = _post(
                        client,
                        "/v1/plans",
                        "live-plan-request",
                        {"migration_id": migration["id"]},
                        202,
                    )
                    self.assertTrue(asyncio.run(worker.run_once()))
                    planned = client.get(f"/v1/plans/{plan['id']}")
                    self.assertEqual(planned.status_code, 200, planned.text)
                    self.assertEqual(planned.json()["status"], "ready")
                    job = _post(
                        client,
                        "/v1/jobs",
                        "live-job-request",
                        {"plan_id": plan["id"]},
                        202,
                    )
                    self.assertTrue(asyncio.run(worker.run_once()))
                    completed = client.get(f"/v1/jobs/{job['id']}")
                    self.assertEqual(completed.status_code, 200, completed.text)
                    self.assertEqual(completed.json()["status"], "completed")
                    report_response = client.get(f"/v1/jobs/{job['id']}/report")
                    self.assertEqual(report_response.status_code, 200, report_response.text)
                    summary = report_response.json()
            finally:
                store.close()
            qdrant = QdrantClient(path=str(qdrant_path))
            try:
                destination_count = qdrant.count(target_name, exact=True).count
                query_vectors = [_as_list(vector) for vector in embed([q for q, _ in QUERIES])]
                overlaps: list[float] = []
                top_matches: list[dict[str, str]] = []
                for (query, expected), vector in zip(QUERIES, query_vectors, strict=True):
                    chroma_ids = source.query(query_embeddings=[vector], n_results=5, include=[])[
                        "ids"
                    ][0]
                    qdrant_points = qdrant.query_points(
                        collection_name=target_name,
                        query=vector,
                        limit=5,
                        with_payload=True,
                    ).points
                    qdrant_ids = [str(point.payload["source_id"]) for point in qdrant_points]
                    overlap = len(set(chroma_ids) & set(qdrant_ids)) / 5
                    overlaps.append(overlap)
                    top_matches.append(
                        {
                            "query": query,
                            "expected": expected,
                            "chroma_top1": chroma_ids[0],
                            "qdrant_top1": qdrant_ids[0],
                        }
                    )
                    self.assertEqual(chroma_ids[0], expected)
                    self.assertEqual(qdrant_ids[0], expected)

                report = {
                    "embedding_model": "Chroma ONNX all-MiniLM-L6-v2",
                    "embedding_dimensions": len(vectors[0]),
                    "chroma_version": version("chromadb"),
                    "qdrant_client_version": version("qdrant-client"),
                    "source_records": source.count(),
                    "destination_records": destination_count,
                    "records_written": summary["records_written"],
                    "sample_digest_mismatches": summary["verification"]["mismatched_samples"],
                    "mean_top5_overlap": sum(overlaps) / len(overlaps),
                    "minimum_top5_overlap": min(overlaps),
                    "queries": top_matches,
                }
                print("\nVME_LIVE_INTEGRATION=" + json.dumps(report, sort_keys=True))

                self.assertEqual(summary["status"], "completed")
                self.assertEqual(source.count(), len(CORPUS))
                self.assertEqual(destination_count, len(CORPUS))
                self.assertEqual(summary["records_written"], len(CORPUS))
                self.assertEqual(summary["verification"]["missing_samples"], 0)
                self.assertEqual(summary["verification"]["mismatched_samples"], 0)
                self.assertGreaterEqual(min(overlaps), 0.8)
            finally:
                qdrant.close()
                SharedSystemClient.clear_system_cache()


def _as_list(vector: object) -> list[float]:
    tolist = getattr(vector, "tolist", None)
    values = tolist() if callable(tolist) else vector
    return [float(value) for value in values]


def _post(
    client: TestClient,
    path: str,
    idempotency_key: str,
    body: dict[str, object],
    expected_status: int,
) -> dict[str, object]:
    response = client.post(
        path,
        headers={"Idempotency-Key": idempotency_key},
        json=body,
    )
    if response.status_code != expected_status:
        raise AssertionError(f"{path} returned {response.status_code}: {response.text}")
    return response.json()


if __name__ == "__main__":
    unittest.main()
