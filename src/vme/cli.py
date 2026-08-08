"""Command-line interface for planning, running, resuming, and inspecting migrations."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

from vme.adapters.registry import builtin_registry
from vme.config import load_config
from vme.domain.models import to_jsonable
from vme.errors import VMEError
from vme.service import build_plan, read_plan_fingerprint, run_migration, write_plan
from vme.state.sqlite import SQLiteStateStore


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="vme", description="Vector Migration Engine")
    root.add_argument("--version", action="version", version="vme 1.0.0a1")
    commands = root.add_subparsers(dest="command", required=True)

    commands.add_parser("adapters", help="list installed source and destination adapters")

    plan = commands.add_parser("plan", help="discover endpoints and compile a migration plan")
    plan.add_argument("--config", required=True)
    plan.add_argument("--out", required=True)

    run = commands.add_parser("run", help="execute a migration")
    run.add_argument("--config", required=True)
    run.add_argument("--plan", help="require this plan artifact fingerprint")
    run.add_argument("--state", default=".vme/state.sqlite3")
    run.add_argument("--resume", metavar="JOB_ID")

    status = commands.add_parser("status", help="show durable migration job state")
    status.add_argument("job_id")
    status.add_argument("--state", default=".vme/state.sqlite3")

    serve = commands.add_parser("serve", help="run the self-hosted REST API and worker")
    serve.add_argument("--state", default=None, help="service/checkpoint SQLite path")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)

    worker = commands.add_parser("worker", help="run a standalone migration worker")
    worker.add_argument("--state", default=None, help="service/checkpoint SQLite path")
    return root


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        if arguments.command == "adapters":
            _print_json(builtin_registry().available())
            return 0
        if arguments.command == "plan":
            settings = load_config(arguments.config)
            plan = asyncio.run(build_plan(settings))
            write_plan(plan, arguments.out)
            _print_json(
                {
                    "fingerprint": plan.fingerprint,
                    "executable": plan.executable,
                    "findings": to_jsonable(plan.findings),
                    "plan": str(Path(arguments.out).resolve()),
                }
            )
            return 0 if plan.executable else 2
        if arguments.command == "run":
            settings = load_config(arguments.config)
            fingerprint = read_plan_fingerprint(arguments.plan) if arguments.plan else None
            summary = asyncio.run(
                run_migration(
                    settings,
                    state_path=arguments.state,
                    expected_plan_fingerprint=fingerprint,
                    resume_job_id=arguments.resume,
                )
            )
            _print_json(to_jsonable(summary))
            return 0
        if arguments.command == "status":
            state = SQLiteStateStore(arguments.state)
            try:
                _print_json(to_jsonable(state.get_job(arguments.job_id)))
            finally:
                state.close()
            return 0
        if arguments.command == "serve":
            from vme.server.app import create_app
            from vme.server.settings import ServerSettings

            try:
                import uvicorn
            except ImportError as error:
                raise VMEError(
                    "service mode requires `pip install vector-migration-engine[server]`"
                ) from error
            settings = ServerSettings.from_env()
            settings = dataclasses.replace(
                settings,
                state_path=Path(arguments.state) if arguments.state else settings.state_path,
                host=arguments.host or settings.host,
                port=arguments.port or settings.port,
            )
            settings.validate()
            uvicorn.run(
                create_app(settings),
                host=settings.host,
                port=settings.port,
                limit_concurrency=settings.max_concurrency,
                timeout_keep_alive=settings.timeout_keep_alive,
                proxy_headers=False,
                server_header=False,
            )
            return 0
        if arguments.command == "worker":
            from vme.server.secrets import SecretResolver
            from vme.server.security import EndpointPolicy
            from vme.server.settings import ServerSettings
            from vme.server.store import SQLiteServiceStore
            from vme.server.worker import ServiceWorker

            settings = ServerSettings.from_env()
            if arguments.state:
                settings = dataclasses.replace(settings, state_path=Path(arguments.state))
            settings.validate(require_api_auth=False)
            store = SQLiteServiceStore(settings.state_path)
            service_worker = ServiceWorker(
                store=store,
                state_path=str(settings.state_path),
                resolver=SecretResolver(
                    settings.allowed_secret_roots,
                    settings.allowed_secret_env_names,
                ),
                poll_seconds=settings.worker_poll_seconds,
                lease_seconds=settings.lease_seconds,
                endpoint_policy=EndpointPolicy(
                    allowed_adapters=settings.allowed_adapters,
                    allowed_data_roots=settings.allowed_data_roots,
                    allowed_endpoints=settings.allowed_endpoints,
                    allow_insecure_endpoints=settings.allow_insecure_endpoints,
                    allow_embedded_chroma=settings.allow_embedded_chroma,
                ),
            )
            try:
                asyncio.run(service_worker.run_forever())
            finally:
                store.close()
            return 0
    except (VMEError, ValueError, OSError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 1


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
