"""Adapter discovery with built-ins and Python entry points."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from importlib.metadata import entry_points
from typing import Any

from vme.adapters.base import DestinationAdapter, SourceAdapter
from vme.errors import AdapterConfigurationError

SourceFactory = Callable[[Mapping[str, Any]], SourceAdapter]
DestinationFactory = Callable[[Mapping[str, Any]], DestinationAdapter]


class AdapterRegistry:
    def __init__(self) -> None:
        self._sources: dict[str, SourceFactory] = {}
        self._destinations: dict[str, DestinationFactory] = {}

    def register_source(self, name: str, factory: SourceFactory) -> None:
        self._register(self._sources, name, factory)

    def register_destination(self, name: str, factory: DestinationFactory) -> None:
        self._register(self._destinations, name, factory)

    @staticmethod
    def _register(registry: dict[str, Any], name: str, factory: Any) -> None:
        normalized = name.strip().lower()
        if not normalized:
            raise ValueError("adapter name cannot be empty")
        if normalized in registry:
            raise ValueError(f"adapter {normalized!r} is already registered")
        registry[normalized] = factory

    def create_source(self, name: str, config: Mapping[str, Any]) -> SourceAdapter:
        try:
            factory = self._sources[name.lower()]
        except KeyError as error:
            raise AdapterConfigurationError(f"unknown source adapter {name!r}") from error
        return factory(config)

    def create_destination(self, name: str, config: Mapping[str, Any]) -> DestinationAdapter:
        try:
            factory = self._destinations[name.lower()]
        except KeyError as error:
            raise AdapterConfigurationError(f"unknown destination adapter {name!r}") from error
        return factory(config)

    def available(self) -> dict[str, list[str]]:
        return {
            "sources": sorted(self._sources),
            "destinations": sorted(self._destinations),
        }

    def load_entry_points(self) -> None:
        for entry_point in entry_points(group="vme.adapters"):
            plugin = entry_point.load()
            plugin(self)


def builtin_registry() -> AdapterRegistry:
    from vme.adapters.chroma import ChromaAdapter
    from vme.adapters.memory import memory_destination_factory, memory_source_factory
    from vme.adapters.qdrant import QdrantAdapter

    registry = AdapterRegistry()
    registry.register_source("memory", memory_source_factory)
    registry.register_destination("memory", memory_destination_factory)
    registry.register_source("chroma", ChromaAdapter)
    registry.register_destination("chroma", ChromaAdapter)
    registry.register_source("qdrant", QdrantAdapter)
    registry.register_destination("qdrant", QdrantAdapter)
    registry.load_entry_points()
    return registry
