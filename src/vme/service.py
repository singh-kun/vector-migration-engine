"""High-level API shared by the CLI and Python callers."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

from vme.adapters.registry import AdapterRegistry, builtin_registry
from vme.config import MigrationSettings
from vme.domain.models import MigrationPlan, to_jsonable
from vme.errors import PlanRejectedError
from vme.execution.executor import MigrationExecutor, RunSummary
from vme.planning.planner import MigrationPlanner
from vme.state.sqlite import SQLiteStateStore


async def build_plan(
    settings: MigrationSettings,
    *,
    registry: AdapterRegistry | None = None,
    close_adapters: bool = True,
) -> MigrationPlan:
    registry = registry or builtin_registry()
    source = registry.create_source(settings.source.adapter, settings.source.config)
    destination = registry.create_destination(
        settings.destination.adapter, settings.destination.config
    )
    try:
        source_capabilities, destination_capabilities, source_spec = await asyncio.gather(
            source.probe(), destination.probe(), source.discover()
        )
        target_name = str(settings.destination.config.get("collection", settings.name))
        return MigrationPlanner().build(
            source=source_spec,
            source_capabilities=source_capabilities,
            destination_capabilities=destination_capabilities,
            target_name=target_name,
            mapping=settings.mapping,
        )
    finally:
        if close_adapters:
            await asyncio.gather(source.close(), destination.close(), return_exceptions=True)


async def run_migration(
    settings: MigrationSettings,
    *,
    state_path: str | Path,
    expected_plan_fingerprint: str | None = None,
    resume_job_id: str | None = None,
    job_id: str | None = None,
    should_stop: Callable[[], bool] | None = None,
    lease_is_valid: Callable[[], bool] | None = None,
    error_redactor: Callable[[str], str] | None = None,
    registry: AdapterRegistry | None = None,
) -> RunSummary:
    registry = registry or builtin_registry()
    source = registry.create_source(settings.source.adapter, settings.source.config)
    destination = registry.create_destination(
        settings.destination.adapter, settings.destination.config
    )
    executor_started = False
    try:
        source_capabilities, destination_capabilities, source_spec = await asyncio.gather(
            source.probe(), destination.probe(), source.discover()
        )
        plan = MigrationPlanner().build(
            source=source_spec,
            source_capabilities=source_capabilities,
            destination_capabilities=destination_capabilities,
            target_name=str(settings.destination.config.get("collection", settings.name)),
            mapping=settings.mapping,
        )
        if not plan.executable:
            errors = "; ".join(
                f"{finding.code}: {finding.message}"
                for finding in plan.findings
                if finding.severity.value == "error"
            )
            raise PlanRejectedError(errors)
        if expected_plan_fingerprint and plan.fingerprint != expected_plan_fingerprint:
            raise ValueError("live endpoint plan does not match the supplied plan artifact")
        state = SQLiteStateStore(state_path)
        try:
            executor = MigrationExecutor(
                source=source,
                destination=destination,
                state=state,
                options=settings.execution,
                should_stop=should_stop,
                lease_is_valid=lease_is_valid,
                error_redactor=error_redactor,
            )
            executor_started = True
            effective_job_id = resume_job_id or job_id
            return await executor.run(
                plan,
                job_id=effective_job_id,
                resume=resume_job_id is not None,
            )
        finally:
            state.close()
    finally:
        if not executor_started:
            await asyncio.gather(source.close(), destination.close(), return_exceptions=True)


def write_plan(plan: MigrationPlan, path: str | Path) -> None:
    plan_path = Path(path)
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    output = json.dumps(to_jsonable(plan), indent=2, sort_keys=True) + "\n"
    plan_path.write_text(output, encoding="utf-8")


def read_plan_fingerprint(path: str | Path) -> str:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return str(raw["fingerprint"])
