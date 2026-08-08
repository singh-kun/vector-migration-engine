"""Provider-neutral records, schemas, plans, and execution results."""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeAlias

from vme.errors import RecordValidationError

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]
ExternalId: TypeAlias = str | int


class VectorKind(enum.StrEnum):
    DENSE = "dense"
    SPARSE = "sparse"
    BINARY = "binary"
    MULTI = "multi"


class MetricKind(enum.StrEnum):
    COSINE = "cosine"
    DOT = "dot"
    EUCLIDEAN = "euclidean"
    SQUARED_EUCLIDEAN = "squared_euclidean"
    MANHATTAN = "manhattan"
    HAMMING = "hamming"
    JACCARD = "jaccard"
    BM25 = "bm25"
    UNKNOWN = "unknown"


class ScoreOrder(enum.StrEnum):
    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"


class Normalization(enum.StrEnum):
    REQUIRED = "required"
    AUTOMATIC = "automatic"
    NONE = "none"
    UNKNOWN = "unknown"


class IdKind(enum.StrEnum):
    STRING = "string"
    INTEGER = "integer"
    UUID = "uuid"


class FindingSeverity(enum.StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class JobStatus(enum.StrEnum):
    QUEUED = "queued"
    PLANNED = "planned"
    PREPARED = "prepared"
    COPYING = "copying"
    VERIFYING = "verifying"
    STOPPED = "stopped"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


class CountQuality(enum.StrEnum):
    EXACT = "exact"
    APPROXIMATE = "approximate"
    UNKNOWN = "unknown"


class IdPolicy(enum.StrEnum):
    PRESERVE = "preserve"
    STRINGIFY = "stringify"
    DETERMINISTIC_UUID = "deterministic_uuid"


@dataclass(frozen=True, slots=True)
class MetricSpec:
    kind: MetricKind
    order: ScoreOrder
    normalization: Normalization = Normalization.UNKNOWN


@dataclass(frozen=True, slots=True)
class DenseVector:
    values: Sequence[float]
    dtype: str = "float32"

    def __post_init__(self) -> None:
        if len(self.values) == 0:
            raise RecordValidationError("dense vectors cannot be empty")
        if any(not math.isfinite(float(value)) for value in self.values):
            raise RecordValidationError("dense vectors cannot contain NaN or infinity")

    @property
    def dimension(self) -> int:
        return len(self.values)

    @property
    def estimated_bytes(self) -> int:
        widths = {"float16": 2, "bfloat16": 2, "int8": 1, "uint8": 1, "float32": 4}
        return self.dimension * widths.get(self.dtype, 8)


@dataclass(frozen=True, slots=True)
class SparseVector:
    indices: Sequence[int]
    values: Sequence[float]
    dimension: int | None = None

    def __post_init__(self) -> None:
        if len(self.indices) != len(self.values):
            raise RecordValidationError("sparse indices and values must have the same length")
        if any(index < 0 for index in self.indices):
            raise RecordValidationError("sparse indices cannot be negative")
        if tuple(self.indices) != tuple(sorted(set(self.indices))):
            raise RecordValidationError("sparse indices must be unique and sorted")
        if any(not math.isfinite(float(value)) for value in self.values):
            raise RecordValidationError("sparse vectors cannot contain NaN or infinity")

    @property
    def estimated_bytes(self) -> int:
        return len(self.indices) * 12


@dataclass(frozen=True, slots=True)
class BinaryVector:
    values: bytes
    dimension_bits: int

    def __post_init__(self) -> None:
        if self.dimension_bits <= 0 or self.dimension_bits > len(self.values) * 8:
            raise RecordValidationError("binary vector dimension is invalid")

    @property
    def estimated_bytes(self) -> int:
        return len(self.values)


@dataclass(frozen=True, slots=True)
class MultiVector:
    rows: Sequence[DenseVector | BinaryVector]

    def __post_init__(self) -> None:
        if len(self.rows) == 0:
            raise RecordValidationError("multi-vectors cannot be empty")
        dimensions = {
            row.dimension if isinstance(row, DenseVector) else row.dimension_bits
            for row in self.rows
        }
        if len(dimensions) != 1:
            raise RecordValidationError("all multi-vector rows must have the same dimension")
        if len({type(row) for row in self.rows}) != 1:
            raise RecordValidationError("multi-vector rows must use one vector representation")

    @property
    def estimated_bytes(self) -> int:
        return sum(row.estimated_bytes for row in self.rows)


VectorValue: TypeAlias = DenseVector | SparseVector | BinaryVector | MultiVector


@dataclass(frozen=True, slots=True)
class RecordScope:
    namespace: str | None = None
    tenant: str | None = None
    partition: str | None = None

    @property
    def key(self) -> str:
        return json.dumps(dataclasses.asdict(self), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class ScopedId:
    id: ExternalId
    scope: RecordScope = field(default_factory=RecordScope)

    @property
    def key(self) -> str:
        id_type = "i" if isinstance(self.id, int) else "s"
        return f"{self.scope.key}|{id_type}:{self.id}"


@dataclass(frozen=True, slots=True)
class VectorRecord:
    id: ExternalId
    vectors: Mapping[str, VectorValue]
    scope: RecordScope = field(default_factory=RecordScope)
    document: str | None = None
    metadata: JsonObject = field(default_factory=dict)
    source_version: str | int | None = None

    def __post_init__(self) -> None:
        if isinstance(self.id, bool) or not isinstance(self.id, (str, int)):
            raise RecordValidationError("record ID must be a string or integer")
        if not self.vectors:
            raise RecordValidationError("a vector record must contain at least one vector")
        if any(not name for name in self.vectors):
            raise RecordValidationError("vector names cannot be empty")
        if any(not isinstance(key, str) for key in self.metadata):
            raise RecordValidationError("metadata keys must be strings")
        try:
            json.dumps(self.metadata, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise RecordValidationError(f"metadata is not strict JSON: {error}") from error

    @property
    def scoped_id(self) -> ScopedId:
        return ScopedId(self.id, self.scope)

    @property
    def estimated_bytes(self) -> int:
        vector_bytes = sum(vector.estimated_bytes for vector in self.vectors.values())
        scalar_bytes = len(self.document.encode("utf-8")) if self.document else 0
        scalar_bytes += len(json.dumps(self.metadata, separators=(",", ":")).encode("utf-8"))
        return vector_bytes + scalar_bytes + len(str(self.id).encode("utf-8"))


@dataclass(frozen=True, slots=True)
class VectorFieldSpec:
    name: str
    kind: VectorKind
    dimension: int | None
    dtype: str
    metric: MetricSpec

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("vector field name cannot be empty")
        if self.dimension is not None and self.dimension <= 0:
            raise ValueError("vector field dimension must be positive")


@dataclass(frozen=True, slots=True)
class CollectionSpec:
    name: str
    vector_fields: Mapping[str, VectorFieldSpec]
    id_kind: IdKind
    scope_kinds: frozenset[str] = frozenset()
    supports_nested_metadata: bool = True
    supports_array_metadata: bool = True
    estimated_count: int | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("collection name cannot be empty")
        if not self.vector_fields:
            raise ValueError("collection must declare at least one vector field")
        if self.estimated_count is not None and self.estimated_count < 0:
            raise ValueError("estimated count cannot be negative")


@dataclass(frozen=True, slots=True)
class AdapterCapabilities:
    adapter_name: str
    source: bool
    destination: bool
    vector_kinds: frozenset[VectorKind]
    metrics: frozenset[MetricKind]
    id_kinds: frozenset[IdKind]
    metric_specs: Mapping[MetricKind, MetricSpec] = field(default_factory=dict)
    named_vectors: bool = False
    nested_metadata: bool = False
    array_metadata: bool = False
    namespaces: bool = False
    tenants: bool = False
    partitions: bool = False
    stable_cursor: bool = False
    snapshot_read: bool = False
    exact_count: bool = False
    read_by_id: bool = False
    idempotent_upsert: bool = False
    bulk_import: bool = False
    max_batch_records: int = 1_000
    max_batch_bytes: int = 16 * 1024 * 1024

    def __post_init__(self) -> None:
        if not self.adapter_name:
            raise ValueError("adapter name cannot be empty")
        if self.max_batch_records <= 0 or self.max_batch_bytes <= 0:
            raise ValueError("adapter batch limits must be positive")


@dataclass(frozen=True, slots=True)
class SourcePartition:
    key: str
    scope: RecordScope = field(default_factory=RecordScope)


@dataclass(frozen=True, slots=True)
class BatchLimit:
    max_records: int
    max_bytes: int

    def __post_init__(self) -> None:
        if self.max_records <= 0 or self.max_bytes <= 0:
            raise ValueError("batch limits must be positive")


@dataclass(frozen=True, slots=True)
class ReadBatch:
    records: Sequence[VectorRecord]
    next_cursor: JsonValue
    exhausted: bool


@dataclass(frozen=True, slots=True)
class BatchWriteResult:
    accepted: Sequence[ScopedId]
    provider_operation_id: str | None = None


@dataclass(frozen=True, slots=True)
class CountResult:
    value: int | None
    quality: CountQuality


@dataclass(frozen=True, slots=True)
class PlanFinding:
    code: str
    severity: FindingSeverity
    message: str


@dataclass(frozen=True, slots=True)
class MappingOptions:
    id_policy: IdPolicy = IdPolicy.PRESERVE
    uuid_namespace: str | None = None
    vector_name_map: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.id_policy is IdPolicy.DETERMINISTIC_UUID:
            if not self.uuid_namespace:
                raise ValueError("uuid_namespace is required for deterministic UUID mapping")
            uuid.UUID(self.uuid_namespace)


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    source: CollectionSpec
    target: CollectionSpec
    source_capabilities: AdapterCapabilities
    destination_capabilities: AdapterCapabilities
    mapping: MappingOptions
    findings: Sequence[PlanFinding]
    fingerprint: str

    @property
    def executable(self) -> bool:
        return not any(f.severity is FindingSeverity.ERROR for f in self.findings)


def to_jsonable(value: Any) -> Any:
    """Convert domain dataclasses and enums into deterministic JSON-compatible values."""
    if dataclasses.is_dataclass(value):
        return {
            field.name: to_jsonable(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(to_jsonable(item) for item in value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, bytes):
        return value.hex()
    return value


def stable_fingerprint(value: Any) -> str:
    encoded = json.dumps(to_jsonable(value), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
