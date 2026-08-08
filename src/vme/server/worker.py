"""Durable plan/job reconciler that owns provider connections and migration execution."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress

from vme.config import MigrationSettings
from vme.domain.models import to_jsonable
from vme.errors import (
    MigrationRunError,
    MigrationStoppedError,
    StateConflictError,
    TransientAdapterError,
    WorkerLeaseLostError,
)
from vme.server.configuration import resolved_migration_settings
from vme.server.models import DesiredState, LeaseClaim, ServiceJobStatus
from vme.server.secrets import SecretResolver
from vme.server.store import SQLiteServiceStore
from vme.service import build_plan, run_migration
from vme.state.sqlite import SQLiteStateStore


class ServiceWorker:
    def __init__(
        self,
        *,
        store: SQLiteServiceStore,
        state_path: str,
        resolver: SecretResolver,
        poll_seconds: float = 0.5,
        lease_seconds: int = 30,
        worker_id: str | None = None,
    ) -> None:
        self.store = store
        self.state_path = state_path
        self.resolver = resolver
        self.poll_seconds = poll_seconds
        self.lease_seconds = lease_seconds
        self.worker_id = worker_id or f"worker-{uuid.uuid4()}"
        self._stop = asyncio.Event()

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            worked = await self.run_once()
            if not worked:
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)

    def request_shutdown(self) -> None:
        self._stop.set()

    async def run_once(self) -> bool:
        self.store.recover_expired_jobs()
        plan_claim = self.store.claim_plan(self.worker_id, self.lease_seconds)
        if plan_claim is not None:
            await self._run_plan(plan_claim)
            return True
        job_claim = self.store.claim_job(self.worker_id, self.lease_seconds)
        if job_claim is not None:
            await self._run_job(job_claim)
            return True
        return False

    async def _run_plan(self, claim: LeaseClaim) -> None:
        heartbeat = asyncio.create_task(
            self._heartbeat_plan(claim.resource_id, claim.lease_token),
            name=f"vme-plan-heartbeat-{claim.resource_id}",
        )
        try:
            record = self.store.get_plan(claim.workspace_id, claim.resource_id)
            migration = self.store.get_migration(record.workspace_id, record.migration_id)
            raw = resolved_migration_settings(self.store, migration, self.resolver)
            plan = await build_plan(MigrationSettings.from_mapping(raw))
            self.store.finish_plan(
                plan_id=record.id,
                lease_token=claim.lease_token,
                fingerprint=plan.fingerprint,
                plan=to_jsonable(plan),
                executable=plan.executable,
            )
        except Exception as error:
            with suppress(StateConflictError):
                self.store.fail_plan(claim.resource_id, claim.lease_token, str(error))
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    async def _run_job(self, claim: LeaseClaim) -> None:
        record = self.store.get_job(claim.workspace_id, claim.resource_id)
        plan_record = self.store.get_plan(record.workspace_id, record.plan_id)
        migration = self.store.get_migration(record.workspace_id, plan_record.migration_id)
        heartbeat = asyncio.create_task(
            self._heartbeat(record.id, claim.lease_token),
            name=f"vme-heartbeat-{record.id}",
        )
        try:
            raw = resolved_migration_settings(self.store, migration, self.resolver)
            settings = MigrationSettings.from_mapping(raw)
            resuming = self._engine_job_exists(record.id)
            summary = await run_migration(
                settings,
                state_path=self.state_path,
                expected_plan_fingerprint=plan_record.fingerprint,
                resume_job_id=record.id if resuming else None,
                job_id=None if resuming else record.id,
                should_stop=lambda: self._should_stop(record.id, claim.lease_token),
                lease_is_valid=lambda: self.store.job_lease_valid(record.id, claim.lease_token),
            )
            self.store.finish_job(
                job_id=record.id,
                lease_token=claim.lease_token,
                status=ServiceJobStatus.COMPLETED,
                report=to_jsonable(summary),
            )
        except MigrationStoppedError:
            desired = self.store.desired_state(record.id, claim.lease_token)
            status = (
                ServiceJobStatus.CANCELLED
                if desired is DesiredState.CANCELLED
                else ServiceJobStatus.STOPPED
            )
            self.store.finish_job(job_id=record.id, lease_token=claim.lease_token, status=status)
        except MigrationRunError as error:
            cause = error.cause
            status = (
                ServiceJobStatus.RECOVERABLE_FAILED
                if isinstance(cause, (TransientAdapterError, WorkerLeaseLostError))
                else ServiceJobStatus.TERMINAL_FAILED
            )
            with suppress(StateConflictError):
                self.store.finish_job(
                    job_id=record.id,
                    lease_token=claim.lease_token,
                    status=status,
                    error=str(cause),
                )
        except Exception as error:
            with suppress(StateConflictError):
                self.store.finish_job(
                    job_id=record.id,
                    lease_token=claim.lease_token,
                    status=ServiceJobStatus.TERMINAL_FAILED,
                    error=str(error),
                )
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    async def _heartbeat(self, job_id: str, lease_token: str) -> None:
        interval = max(1.0, self.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            if not self.store.renew_job_lease(job_id, lease_token, self.lease_seconds):
                return

    async def _heartbeat_plan(self, plan_id: str, lease_token: str) -> None:
        interval = max(1.0, self.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            if not self.store.renew_plan_lease(plan_id, lease_token, self.lease_seconds):
                return

    def _should_stop(self, job_id: str, lease_token: str) -> bool:
        try:
            return self.store.desired_state(job_id, lease_token) is not DesiredState.RUNNING
        except StateConflictError:
            return True

    def _engine_job_exists(self, job_id: str) -> bool:
        state = SQLiteStateStore(self.state_path)
        try:
            state.get_job(job_id)
        except StateConflictError:
            return False
        finally:
            state.close()
        return True
