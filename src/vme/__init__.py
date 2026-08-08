"""Vector Migration Engine public API."""

from .domain.models import (
    CollectionSpec,
    DenseVector,
    MetricKind,
    MetricSpec,
    RecordScope,
    VectorFieldSpec,
    VectorRecord,
)
from .execution.executor import MigrationExecutor, RunSummary
from .planning.planner import MigrationPlanner

__all__ = [
    "CollectionSpec",
    "DenseVector",
    "MetricKind",
    "MetricSpec",
    "MigrationExecutor",
    "MigrationPlanner",
    "RecordScope",
    "RunSummary",
    "VectorFieldSpec",
    "VectorRecord",
]

__version__ = "1.0.0a1"
