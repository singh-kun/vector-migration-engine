"""Compose immutable migration input from reusable service resources."""

from __future__ import annotations

from typing import Any

from vme.errors import ConfigurationError
from vme.server.models import MigrationDefinition, ProfileRole
from vme.server.secrets import SecretResolver
from vme.server.store import SQLiteServiceStore


def resolved_migration_settings(
    store: SQLiteServiceStore,
    migration: MigrationDefinition,
    resolver: SecretResolver,
) -> dict[str, Any]:
    specification = migration.specification
    source_profile_id = _required_string(specification, "source_profile_id")
    destination_profile_id = _required_string(specification, "destination_profile_id")
    source = store.get_profile(migration.workspace_id, source_profile_id)
    destination = store.get_profile(migration.workspace_id, destination_profile_id)
    if source.role not in {ProfileRole.SOURCE, ProfileRole.BOTH}:
        raise ConfigurationError(f"profile {source.id} cannot be used as a source")
    if destination.role not in {ProfileRole.DESTINATION, ProfileRole.BOTH}:
        raise ConfigurationError(f"profile {destination.id} cannot be used as a destination")

    raw: dict[str, Any] = {
        "metadata": {"name": migration.name},
        "source": {
            "adapter": source.adapter,
            "connection": resolver.resolve(source.connection),
            "resource": dict(specification.get("source_resource") or {}),
        },
        "destination": {
            "adapter": destination.adapter,
            "connection": resolver.resolve(destination.connection),
            "resource": dict(specification.get("destination_resource") or {}),
        },
    }
    for key in ("mapping", "execution", "verification"):
        if key in specification:
            raw[key] = specification[key]
    return raw


def _required_string(value: dict[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ConfigurationError(f"migration specification requires {key}")
    return result
