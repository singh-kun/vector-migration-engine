from __future__ import annotations

import asyncio
import unittest

from vme.adapters.chroma import ChromaAdapter
from vme.adapters.memory import MemorySourceAdapter
from vme.adapters.qdrant import QdrantAdapter
from vme.domain.models import (
    CollectionSpec,
    IdKind,
    IdPolicy,
    MappingOptions,
    MetricKind,
    MetricSpec,
    Normalization,
    ScoreOrder,
    VectorFieldSpec,
    VectorKind,
)
from vme.planning.planner import MigrationPlanner

from tests.helpers import records, spec


class PlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = MemorySourceAdapter(spec(1), records(1))
        self.qdrant = QdrantAdapter({"collection": "target"})

    def test_incompatible_string_ids_are_rejected(self) -> None:
        plan = MigrationPlanner().build(
            source=asyncio.run(self.source.discover()),
            source_capabilities=asyncio.run(self.source.probe()),
            destination_capabilities=asyncio.run(self.qdrant.probe()),
            target_name="target",
        )
        self.assertFalse(plan.executable)
        self.assertIn("VME-ID-001", {finding.code for finding in plan.findings})

    def test_deterministic_uuid_policy_is_accepted(self) -> None:
        mapping = MappingOptions(
            id_policy=IdPolicy.DETERMINISTIC_UUID,
            uuid_namespace="60f5f0b8-6574-4b98-9f55-e365efa51e20",
        )
        plan = MigrationPlanner().build(
            source=asyncio.run(self.source.discover()),
            source_capabilities=asyncio.run(self.source.probe()),
            destination_capabilities=asyncio.run(self.qdrant.probe()),
            target_name="target",
            mapping=mapping,
        )
        self.assertTrue(plan.executable)
        self.assertEqual(plan.target.id_kind.value, "uuid")

    def test_score_order_change_is_reported(self) -> None:
        metric = MetricSpec(
            MetricKind.COSINE,
            ScoreOrder.LOWER_IS_BETTER,
            Normalization.AUTOMATIC,
        )
        source_spec = CollectionSpec(
            "source",
            {
                "default": VectorFieldSpec(
                    "default", VectorKind.DENSE, 3, "float32", metric
                )
            },
            IdKind.STRING,
        )
        mapping = MappingOptions(
            id_policy=IdPolicy.DETERMINISTIC_UUID,
            uuid_namespace="60f5f0b8-6574-4b98-9f55-e365efa51e20",
        )
        plan = MigrationPlanner().build(
            source=source_spec,
            source_capabilities=asyncio.run(
                ChromaAdapter({"collection": "source"}).probe()
            ),
            destination_capabilities=asyncio.run(self.qdrant.probe()),
            target_name="target",
            mapping=mapping,
        )
        self.assertIn("VME-METRIC-003", {finding.code for finding in plan.findings})


if __name__ == "__main__":
    unittest.main()
