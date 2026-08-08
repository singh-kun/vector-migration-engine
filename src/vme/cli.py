"""Command-line interface for planning, running, resuming, and inspecting migrations."""

from __future__ import annotations

import argparse
import asyncio
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
    except (VMEError, ValueError, OSError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 1


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
