"""Compile source schema, destination capabilities, and policies into a safe plan."""

from __future__ import annotations

from dataclasses import replace

from vme.domain.models import (
    AdapterCapabilities,
    CollectionSpec,
    FindingSeverity,
    IdKind,
    IdPolicy,
    MappingOptions,
    MetricKind,
    MigrationPlan,
    PlanFinding,
    VectorFieldSpec,
    stable_fingerprint,
)


class MigrationPlanner:
    def build(
        self,
        *,
        source: CollectionSpec,
        source_capabilities: AdapterCapabilities,
        destination_capabilities: AdapterCapabilities,
        target_name: str,
        mapping: MappingOptions | None = None,
    ) -> MigrationPlan:
        mapping = mapping or MappingOptions()
        findings: list[PlanFinding] = []

        if not source_capabilities.source:
            findings.append(self._error("VME-ADAPTER-001", "configured source is not readable"))
        if not destination_capabilities.destination:
            findings.append(
                self._error("VME-ADAPTER-002", "configured destination is not writable")
            )
        if not destination_capabilities.idempotent_upsert:
            findings.append(
                self._error(
                    "VME-WRITE-001",
                    "MVP1 requires an idempotent destination upsert capability",
                )
            )
        if not source_capabilities.stable_cursor:
            findings.append(
                self._warning(
                    "VME-CONS-001",
                    "source does not advertise a stable cursor; the source must be quiesced",
                )
            )
        if not source_capabilities.snapshot_read:
            findings.append(
                self._warning(
                    "VME-CONS-002",
                    "source does not provide a snapshot; writes must be quiesced "
                    "for a consistent copy",
                )
            )

        target_fields: dict[str, VectorFieldSpec] = {}
        for source_name, source_field in source.vector_fields.items():
            target_field_name = mapping.vector_name_map.get(source_name, source_name)
            if target_field_name in target_fields:
                findings.append(
                    self._error(
                        "VME-VECTOR-004",
                        f"multiple source vectors map to target field {target_field_name!r}",
                    )
                )
                continue
            if source_field.kind not in destination_capabilities.vector_kinds:
                findings.append(
                    self._error(
                        "VME-VECTOR-001",
                        f"destination does not support {source_field.kind.value} vectors",
                    )
                )
            if source_field.metric.kind is MetricKind.UNKNOWN:
                findings.append(
                    self._error(
                        "VME-METRIC-001",
                        f"metric for vector field {source_name!r} could not be discovered",
                    )
                )
            elif source_field.metric.kind not in destination_capabilities.metrics:
                findings.append(
                    self._error(
                        "VME-METRIC-002",
                        f"destination does not support metric {source_field.metric.kind.value!r}",
                    )
                )
            destination_metric = destination_capabilities.metric_specs.get(
                source_field.metric.kind, source_field.metric
            )
            if (
                source_field.metric.kind is not MetricKind.UNKNOWN
                and destination_metric.order is not source_field.metric.order
            ):
                findings.append(
                    self._warning(
                        "VME-METRIC-003",
                        f"vector field {source_name!r} changes score ordering from "
                        f"{source_field.metric.order.value} to {destination_metric.order.value}; "
                        "application score thresholds must be reviewed",
                    )
                )
            target_fields[target_field_name] = replace(
                source_field,
                name=target_field_name,
                metric=destination_metric,
            )

        if len(target_fields) > 1 and not destination_capabilities.named_vectors:
            findings.append(
                self._error("VME-VECTOR-003", "destination does not support multiple named vectors")
            )

        target_id_kind = self._target_id_kind(source.id_kind, mapping)
        if target_id_kind not in destination_capabilities.id_kinds:
            findings.append(
                self._error(
                    "VME-ID-001",
                    f"ID policy produces {target_id_kind.value} IDs unsupported by destination",
                )
            )

        unsupported_scopes = {
            scope
            for scope in source.scope_kinds
            if not self._scope_supported(scope, destination_capabilities)
        }
        if unsupported_scopes:
            findings.append(
                self._error(
                    "VME-SCOPE-001",
                    "destination cannot preserve scopes: " + ", ".join(sorted(unsupported_scopes)),
                )
            )
        if source.supports_nested_metadata and not destination_capabilities.nested_metadata:
            findings.append(
                self._warning(
                    "VME-META-001",
                    "destination does not advertise nested metadata; populated values "
                    "are validated per batch",
                )
            )
        if source.supports_array_metadata and not destination_capabilities.array_metadata:
            findings.append(
                self._warning(
                    "VME-META-002",
                    "destination does not advertise array metadata; populated values "
                    "are validated per batch",
                )
            )

        target = CollectionSpec(
            name=target_name,
            vector_fields=target_fields,
            id_kind=target_id_kind,
            scope_kinds=source.scope_kinds,
            supports_nested_metadata=(
                source.supports_nested_metadata and destination_capabilities.nested_metadata
            ),
            supports_array_metadata=(
                source.supports_array_metadata and destination_capabilities.array_metadata
            ),
            estimated_count=source.estimated_count,
        )
        fingerprint_payload = {
            "source": source,
            "target": target,
            "source_adapter": source_capabilities.adapter_name,
            "destination_adapter": destination_capabilities.adapter_name,
            "mapping": mapping,
            "findings": findings,
        }
        return MigrationPlan(
            source=source,
            target=target,
            source_capabilities=source_capabilities,
            destination_capabilities=destination_capabilities,
            mapping=mapping,
            findings=tuple(findings),
            fingerprint=stable_fingerprint(fingerprint_payload),
        )

    @staticmethod
    def _target_id_kind(source_kind: IdKind, mapping: MappingOptions) -> IdKind:
        if mapping.id_policy is IdPolicy.PRESERVE:
            return source_kind
        if mapping.id_policy is IdPolicy.STRINGIFY:
            return IdKind.STRING
        return IdKind.UUID

    @staticmethod
    def _scope_supported(scope: str, capabilities: AdapterCapabilities) -> bool:
        return {
            "namespace": capabilities.namespaces,
            "tenant": capabilities.tenants,
            "partition": capabilities.partitions,
        }.get(scope, False)

    @staticmethod
    def _error(code: str, message: str) -> PlanFinding:
        return PlanFinding(code, FindingSeverity.ERROR, message)

    @staticmethod
    def _warning(code: str, message: str) -> PlanFinding:
        return PlanFinding(code, FindingSeverity.WARNING, message)
