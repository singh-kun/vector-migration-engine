"""Strict public API request schemas."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from vme.server.models import ProfileRole


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConnectionProfileCreate(StrictModel):
    name: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9 _.:-]*$",
    )
    adapter: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_-]+$")
    role: ProfileRole = ProfileRole.BOTH
    connection: dict[str, Any] = Field(default_factory=dict)


class MigrationCreate(StrictModel):
    name: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9 _.:-]*$",
    )
    source_profile_id: str = Field(min_length=1)
    destination_profile_id: str = Field(min_length=1)
    source_resource: dict[str, Any]
    destination_resource: dict[str, Any]
    mapping: dict[str, Any] = Field(default_factory=dict)
    execution: dict[str, Any] = Field(default_factory=dict)
    verification: dict[str, Any] = Field(default_factory=dict)


class PlanCreate(StrictModel):
    migration_id: str = Field(min_length=1)


class JobCreate(StrictModel):
    plan_id: str = Field(min_length=1)
