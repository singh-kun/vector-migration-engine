"""Deterministic canonical record hashes."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import struct
from collections.abc import Iterable
from typing import Any

from vme.domain.models import BinaryVector, DenseVector, MultiVector, SparseVector, VectorRecord


def record_digest(record: VectorRecord) -> str:
    digest = hashlib.sha256()
    _write_json(
        digest,
        {
            "id_type": "integer" if isinstance(record.id, int) else "string",
            "id": record.id,
            "scope": dataclasses.asdict(record.scope),
            "document": record.document,
            "metadata": record.metadata,
        },
    )
    for name in sorted(record.vectors):
        digest.update(name.encode("utf-8"))
        vector = record.vectors[name]
        if isinstance(vector, DenseVector):
            digest.update(b"dense\0" + vector.dtype.encode("ascii") + b"\0")
            _write_floats(digest, vector.values)
        elif isinstance(vector, SparseVector):
            digest.update(b"sparse\0")
            for index, value in zip(vector.indices, vector.values, strict=True):
                digest.update(struct.pack("<Qd", index, float(value)))
            digest.update(struct.pack("<q", vector.dimension or -1))
        elif isinstance(vector, BinaryVector):
            digest.update(b"binary\0")
            digest.update(struct.pack("<Q", vector.dimension_bits))
            digest.update(vector.values)
        elif isinstance(vector, MultiVector):
            digest.update(b"multi\0")
            for row in vector.rows:
                if isinstance(row, DenseVector):
                    digest.update(row.dtype.encode("ascii") + b"\0")
                    _write_floats(digest, row.values)
                else:
                    digest.update(struct.pack("<Q", row.dimension_bits))
                    digest.update(row.values)
    return digest.hexdigest()


def _write_json(digest: Any, value: object) -> None:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    digest.update(encoded.encode("utf-8"))


def _write_floats(digest: Any, values: Iterable[float]) -> None:
    for value in values:
        digest.update(struct.pack("<d", float(value)))
