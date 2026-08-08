"""Independent destination count and deterministic read-back verification."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from vme.adapters.base import DestinationAdapter
from vme.domain.models import CountQuality, RecordScope
from vme.state.sqlite import SQLiteStateStore
from vme.verification.digests import record_digest


@dataclass(frozen=True, slots=True)
class VerificationResult:
    expected_count: int
    destination_count: int | None
    count_quality: CountQuality
    sampled: int
    missing_samples: int
    mismatched_samples: int

    @property
    def passed(self) -> bool:
        count_passed = (
            self.destination_count is None
            or self.count_quality is not CountQuality.EXACT
            or self.destination_count == self.expected_count
        )
        return count_passed and self.missing_samples == 0 and self.mismatched_samples == 0


class Verifier:
    async def verify(
        self,
        *,
        job_id: str,
        expected_count: int,
        scopes: Sequence[RecordScope],
        destination: DestinationAdapter,
        state: SQLiteStateStore,
    ) -> VerificationResult:
        unique_scopes = {scope.key: scope for scope in scopes}
        total = 0
        quality = CountQuality.EXACT
        for scope in unique_scopes.values():
            result = await destination.count(scope)
            if result.value is None:
                quality = CountQuality.UNKNOWN
                total_value: int | None = None
                break
            total += result.value
            if result.quality is CountQuality.UNKNOWN:
                quality = CountQuality.UNKNOWN
            elif result.quality is CountQuality.APPROXIMATE and quality is CountQuality.EXACT:
                quality = CountQuality.APPROXIMATE
        else:
            total_value = total

        expectations = state.samples(job_id)
        requested = [sample.scoped_id for sample in expectations]
        returned = await destination.read_by_ids(requested) if requested else []
        actual = {record.scoped_id.key: record_digest(record) for record in returned}
        missing = 0
        mismatched = 0
        for expectation in expectations:
            digest = actual.get(expectation.scoped_id.key)
            if digest is None:
                missing += 1
            elif digest != expectation.expected_digest:
                mismatched += 1

        return VerificationResult(
            expected_count=expected_count,
            destination_count=total_value,
            count_quality=quality,
            sampled=len(expectations),
            missing_samples=missing,
            mismatched_samples=mismatched,
        )
