from __future__ import annotations

import unittest

from vme.domain.models import DenseVector, RecordScope, VectorRecord, stable_fingerprint
from vme.errors import RecordValidationError, redact_text
from vme.verification.digests import record_digest


class DomainTests(unittest.TestCase):
    def test_record_digest_is_stable_across_mapping_order(self) -> None:
        left = VectorRecord(
            id="a",
            vectors={"b": DenseVector((1, 2)), "a": DenseVector((3, 4))},
            metadata={"z": 1, "a": "value"},
        )
        right = VectorRecord(
            id="a",
            vectors={"a": DenseVector((3, 4)), "b": DenseVector((1, 2))},
            metadata={"a": "value", "z": 1},
        )
        self.assertEqual(record_digest(left), record_digest(right))

    def test_record_digest_includes_scope(self) -> None:
        first = VectorRecord(id=1, vectors={"default": DenseVector((1, 2))})
        second = VectorRecord(
            id=1,
            vectors={"default": DenseVector((1, 2))},
            scope=RecordScope(namespace="tenant-a"),
        )
        self.assertNotEqual(record_digest(first), record_digest(second))

    def test_non_finite_vectors_are_rejected(self) -> None:
        with self.assertRaises(RecordValidationError):
            DenseVector((1.0, float("nan")))

    def test_fingerprint_is_deterministic(self) -> None:
        self.assertEqual(
            stable_fingerprint({"b": 2, "a": 1}),
            stable_fingerprint({"a": 1, "b": 2}),
        )

    def test_error_redaction_removes_common_credentials(self) -> None:
        value = redact_text("api_key=secret token:abc Authorization=Bearer-123 Bearer xyz.123")
        self.assertNotIn("secret", value)
        self.assertNotIn("abc", value)
        self.assertNotIn("xyz.123", value)


if __name__ == "__main__":
    unittest.main()
