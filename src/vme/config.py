"""Validated migration configuration with environment-backed secrets."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vme.domain.models import IdPolicy, MappingOptions
from vme.errors import ConfigurationError
from vme.execution.executor import ExecutionOptions, RetryPolicy


@dataclass(frozen=True, slots=True)
class EndpointSettings:
    adapter: str
    config: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class MigrationSettings:
    name: str
    source: EndpointSettings
    destination: EndpointSettings
    mapping: MappingOptions
    execution: ExecutionOptions

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "MigrationSettings":
        source = _endpoint(raw.get("source"), "source")
        destination = _endpoint(raw.get("destination"), "destination")
        metadata = raw.get("metadata") or {}
        name = str(metadata.get("name") or raw.get("name") or "migration")

        mapping_raw = raw.get("mapping") or {}
        ids_raw = mapping_raw.get("ids") or {}
        vector_raw = mapping_raw.get("vectors") or {}
        vector_names: dict[str, str] = {}
        for source_name, target in vector_raw.items():
            if isinstance(target, Mapping):
                target_name = target.get("to", source_name)
            else:
                target_name = target
            vector_names[str(source_name)] = str(target_name)
        try:
            mapping = MappingOptions(
                id_policy=IdPolicy(str(ids_raw.get("policy", "preserve"))),
                uuid_namespace=ids_raw.get("namespace"),
                vector_name_map=vector_names,
            )
        except (ValueError, TypeError) as error:
            raise ConfigurationError(f"invalid mapping configuration: {error}") from error

        execution_raw = raw.get("execution") or {}
        batch_raw = execution_raw.get("batch") or {}
        concurrency_raw = execution_raw.get("concurrency") or {}
        retry_raw = execution_raw.get("retry") or {}
        verification_raw = raw.get("verification") or {}
        sample_raw = verification_raw.get("sample") or {}
        try:
            execution = ExecutionOptions(
                max_batch_records=int(batch_raw.get("max_records", 500)),
                max_batch_bytes=int(batch_raw.get("max_bytes", 8 * 1024 * 1024)),
                partition_concurrency=int(concurrency_raw.get("partitions", 2)),
                writer_concurrency=int(concurrency_raw.get("writers", 4)),
                sample_size=int(sample_raw.get("records", 1_000)),
                target_batch_latency_seconds=float(
                    execution_raw.get("target_batch_latency_seconds", 1.0)
                ),
                retry=RetryPolicy(
                    max_attempts=int(retry_raw.get("max_attempts", 8)),
                    base_delay_seconds=float(retry_raw.get("base_delay_seconds", 0.25)),
                    max_delay_seconds=float(retry_raw.get("max_delay_seconds", 30.0)),
                ),
            )
        except (TypeError, ValueError) as error:
            raise ConfigurationError(f"invalid execution configuration: {error}") from error
        return cls(name, source, destination, mapping, execution)


def load_config(path: str | Path) -> MigrationSettings:
    config_path = Path(path)
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigurationError(f"cannot read config {config_path}: {error}") from error
    if config_path.suffix.lower() == ".json":
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as error:
            raise ConfigurationError(f"invalid JSON configuration: {error}") from error
    else:
        try:
            import yaml
        except ImportError as error:
            raise ConfigurationError("YAML configuration requires PyYAML") from error
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as error:
            raise ConfigurationError(f"invalid YAML configuration: {error}") from error
    if not isinstance(raw, Mapping):
        raise ConfigurationError("configuration root must be an object")
    return MigrationSettings.from_mapping(_resolve_env(raw))


def _endpoint(value: Any, name: str) -> EndpointSettings:
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{name} must be an object")
    adapter = str(value.get("adapter", "")).strip().lower()
    if not adapter:
        raise ConfigurationError(f"{name}.adapter is required")
    connection = value.get("connection") or {}
    resource = value.get("resource") or {}
    if not isinstance(connection, Mapping) or not isinstance(resource, Mapping):
        raise ConfigurationError(f"{name}.connection and {name}.resource must be objects")
    config = dict(connection)
    config.update(resource)
    return EndpointSettings(adapter, config)


def _resolve_env(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _resolve_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_env(item) for item in value]
    if isinstance(value, str) and value.startswith("env:"):
        variable = value[4:]
        if not variable:
            raise ConfigurationError("environment variable reference cannot be empty")
        try:
            return os.environ[variable]
        except KeyError as error:
            raise ConfigurationError(
                f"required environment variable {variable!r} is not set"
            ) from error
    return value
