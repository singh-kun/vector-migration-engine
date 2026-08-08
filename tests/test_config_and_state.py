from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from tests.helpers import records, spec
from vme.adapters.memory import MemoryDestinationAdapter, MemorySourceAdapter
from vme.config import MigrationSettings, load_config
from vme.domain.models import IdPolicy
from vme.errors import ConfigurationError, StateConflictError
from vme.planning.planner import MigrationPlanner
from vme.state.sqlite import SQLiteStateStore


class ConfigTests(unittest.TestCase):
    def test_parses_mapping_and_execution_options(self) -> None:
        settings = MigrationSettings.from_mapping(
            {
                "metadata": {"name": "example"},
                "source": {"adapter": "memory", "resource": {"collection": "source"}},
                "destination": {
                    "adapter": "memory",
                    "resource": {"collection": "target"},
                },
                "mapping": {
                    "ids": {
                        "policy": "deterministic_uuid",
                        "namespace": "60f5f0b8-6574-4b98-9f55-e365efa51e20",
                    }
                },
                "execution": {"batch": {"max_records": 25}},
            }
        )
        self.assertEqual(settings.name, "example")
        self.assertEqual(settings.mapping.id_policy, IdPolicy.DETERMINISTIC_UUID)
        self.assertEqual(settings.execution.max_batch_records, 25)

    def test_missing_adapter_is_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            MigrationSettings.from_mapping({"source": {}, "destination": {}})

    def test_environment_secret_reference_is_resolved_at_load_time(self) -> None:
        raw = {
            "source": {
                "adapter": "memory",
                "connection": {"api_key": "env:VME_TEST_KEY"},
                "resource": {"collection": "source"},
            },
            "destination": {
                "adapter": "memory",
                "resource": {"collection": "target"},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "migration.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            previous = os.environ.get("VME_TEST_KEY")
            os.environ["VME_TEST_KEY"] = "secret-value"
            try:
                settings = load_config(path)
            finally:
                if previous is None:
                    del os.environ["VME_TEST_KEY"]
                else:
                    os.environ["VME_TEST_KEY"] = previous
        self.assertEqual(settings.source.config["api_key"], "secret-value")


class StateTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_rejects_changed_plan(self) -> None:
        source = MemorySourceAdapter(spec(1), records(1))
        destination = MemoryDestinationAdapter()
        source_capabilities = await source.probe()
        destination_capabilities = await destination.probe()
        planner = MigrationPlanner()
        first = planner.build(
            source=await source.discover(),
            source_capabilities=source_capabilities,
            destination_capabilities=destination_capabilities,
            target_name="first",
        )
        second = planner.build(
            source=await source.discover(),
            source_capabilities=source_capabilities,
            destination_capabilities=destination_capabilities,
            target_name="second",
        )
        with tempfile.TemporaryDirectory() as directory:
            state = SQLiteStateStore(Path(directory) / "state.sqlite3")
            try:
                job_id = state.create_job(first)
                with self.assertRaises(StateConflictError):
                    state.assert_resume(job_id, second)
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main()
