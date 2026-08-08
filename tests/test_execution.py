from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vme.adapters.memory import MemoryDestinationAdapter, MemorySourceAdapter
from vme.domain.models import JobStatus, ReadBatch
from vme.errors import MigrationRunError
from vme.execution.executor import ExecutionOptions, MigrationExecutor, RetryPolicy
from vme.planning.planner import MigrationPlanner
from vme.state.sqlite import SQLiteStateStore
from vme.verification.verifier import Verifier

from tests.helpers import records, spec


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state = SQLiteStateStore(Path(self.temporary.name) / "state.sqlite3")

    async def asyncTearDown(self) -> None:
        self.state.close()
        self.temporary.cleanup()

    async def _components(self, count: int, *, partitions: int = 1, batch: int = 2):
        source = MemorySourceAdapter(
            spec(count, partitions=partitions),
            records(count, partitions=partitions),
        )
        destination = MemoryDestinationAdapter()
        plan = MigrationPlanner().build(
            source=await source.discover(),
            source_capabilities=await source.probe(),
            destination_capabilities=await destination.probe(),
            target_name="target",
        )
        options = ExecutionOptions(
            max_batch_records=batch,
            partition_concurrency=partitions,
            writer_concurrency=2,
            sample_size=count,
            retry=RetryPolicy(max_attempts=3, base_delay_seconds=0, max_delay_seconds=0),
        )
        return source, destination, plan, options

    async def test_migrates_multiple_partitions_with_bounded_batches(self) -> None:
        source, destination, plan, options = await self._components(11, partitions=3, batch=2)
        summary = await MigrationExecutor(
            source=source,
            destination=destination,
            state=self.state,
            options=options,
        ).run(plan)
        self.assertEqual(summary.status, JobStatus.COMPLETED)
        self.assertEqual(summary.records_written, 11)
        self.assertEqual(len(destination.records), 11)
        self.assertGreaterEqual(summary.batches_written, 6)
        self.assertTrue(summary.verification.passed)

    async def test_transient_writes_are_retried(self) -> None:
        source, destination, plan, options = await self._components(3, batch=3)
        destination.fail_next_writes = 2
        summary = await MigrationExecutor(
            source=source,
            destination=destination,
            state=self.state,
            options=options,
        ).run(plan)
        self.assertEqual(summary.records_written, 3)
        self.assertEqual(destination.fail_next_writes, 0)

    async def test_resume_continues_from_checkpoint(self) -> None:
        source, destination, plan, options = await self._components(5, batch=2)
        destination.fail_fatally_after_writes = 1
        executor = MigrationExecutor(
            source=source,
            destination=destination,
            state=self.state,
            options=options,
        )
        with self.assertRaises(MigrationRunError) as raised:
            await executor.run(plan)
        job_id = raised.exception.job_id
        failed = self.state.get_job(job_id)
        self.assertEqual(failed.status, JobStatus.FAILED)
        self.assertEqual(failed.records_written, 2)

        destination.fail_fatally_after_writes = None
        resumed = await MigrationExecutor(
            source=source,
            destination=destination,
            state=self.state,
            options=options,
        ).run(plan, job_id=job_id)
        self.assertEqual(resumed.status, JobStatus.COMPLETED)
        self.assertEqual(resumed.records_written, 5)
        self.assertEqual(len(destination.records), 5)

    async def test_verifier_detects_corruption(self) -> None:
        source, destination, plan, options = await self._components(3, batch=2)
        summary = await MigrationExecutor(
            source=source,
            destination=destination,
            state=self.state,
            options=options,
        ).run(plan)
        expectation = self.state.samples(summary.job_id)[0]
        destination.corrupt_document(expectation.scoped_id, "corrupted")
        result = await Verifier().verify(
            job_id=summary.job_id,
            expected_count=3,
            scopes=[record.scope for record in records(3)],
            destination=destination,
            state=self.state,
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.mismatched_samples, 1)

    async def test_exact_source_count_detects_premature_exhaustion(self) -> None:
        source, destination, plan, options = await self._components(3, batch=2)
        original_read = source.read_batch

        async def truncated_read(partition, cursor, limit):
            result = await original_read(partition, cursor, limit)
            return ReadBatch(result.records[:1], 1, True)

        source.read_batch = truncated_read  # type: ignore[method-assign]
        with self.assertRaises(MigrationRunError) as raised:
            await MigrationExecutor(
                source=source,
                destination=destination,
                state=self.state,
                options=options,
            ).run(plan)
        self.assertIn("only 1 records were transferred", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
