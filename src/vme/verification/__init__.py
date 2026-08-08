"""Destination verification."""

from .digests import record_digest
from .verifier import VerificationResult, Verifier

__all__ = ["VerificationResult", "Verifier", "record_digest"]
