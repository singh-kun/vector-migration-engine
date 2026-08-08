# MVP1 implementation

MVP1 implements the first production-oriented slice of the [v1 architecture](design/v1-architecture.md): capability planning, a provider-neutral record model, bounded migration, durable recovery, and independent verification.

## Implemented

- Canonical dense, sparse, binary, and multi-vector domain types.
- Source/destination capability descriptors and plugin registry (`vme.adapters`).
- Compatibility findings for vector shape, metric, named vectors, IDs, scopes, metadata, snapshot support, and idempotence.
- Deterministic `preserve`, `stringify`, and UUIDv5 ID mapping.
- Deterministic vector-field renaming.
- Native Chroma source/destination adapter using collection pagination and upsert.
- Native Qdrant source/destination adapter using scroll, exact count, collection creation, and idempotent upsert.
- Record- and byte-bounded execution with partition and writer concurrency limits.
- Adaptive record-batch sizing based on observed destination latency.
- Typed transient/throttle/fatal failures with bounded exponential backoff and jitter.
- SQLite WAL job state, per-partition opaque cursors, transactional checkpoint-after-ack, and immutable plan fingerprints.
- Pre-copy exact source count capture and post-copy exact destination count verification.
- Bounded deterministic record sampling with destination read-back and canonical SHA-256 digests.
- CLI commands for adapter discovery, planning, running, resuming, and status.
- Buildable Python wheel with optional provider dependency groups.

## Safety semantics

1. The planner refuses unsupported vector kinds, metrics, ID mappings, scopes, and non-idempotent destinations.
2. Destination checkpoints advance only after the entire submitted ID set is acknowledged.
3. A crash after destination write but before checkpoint causes a safe replay using deterministic IDs and upsert.
4. Resume requires the same plan fingerprint and the source exact count must not change.
5. New runs refuse an existing collection by default. A known resume may reopen its target.
6. Secrets are resolved from `env:NAME` references only at runtime and are not part of plan or state serialization.
7. Source data is never deleted.

## Current certification boundary

MVP1 certifies the core through deterministic in-memory contract/fault-injection tests and exercises Chroma/Qdrant adapter behavior with SDK-compatible fakes. A live service integration environment is still required before declaring a specific Chroma and Qdrant server/SDK version pair production-certified.

The currently supported live feature intersection is:

- one collection per migration;
- dense float vectors;
- one Chroma vector field or one/more Qdrant named dense fields where the target supports them;
- Chroma string IDs mapped to deterministic Qdrant UUIDs when required;
- documents and JSON-compatible metadata;
- a quiesced source when no database snapshot is available;
- streaming upsert (not provider object-storage bulk import).

Namespaces, tenants, arbitrary reference graphs, sparse/binary/multi-vector provider adapters, bulk import, query-quality validation, alias cutover, and change-feed catch-up remain later milestones.

## Validation commands

```bash
PYTHONPATH=src python -m compileall -q src tests
PYTHONPATH=src python -m unittest discover -s tests -v
python -m pip wheel . --no-deps --no-build-isolation --no-cache-dir --wheel-dir .vme/dist
```

## Next implementation slice

1. Add Docker-backed Chroma and Qdrant integration tests pinned to supported SDK/server versions.
2. Add full-dataset bucket digests and semantic top-k overlap verification.
3. Add OpenTelemetry-compatible metrics/events and a structured Markdown/JSON final report.
4. Implement Weaviate and the versioned FAISS bundle adapters.
5. Add target alias cutover where the provider supports it.
