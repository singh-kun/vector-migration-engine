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
- REST/OpenAPI resources for reusable connection profiles, migration definitions, immutable plans,
  durable jobs, controls, events, health, progress, and reports.
- SQLite service queues with worker leases, heartbeats, recovery state, and stale-worker fencing at
  checkpoint boundaries.
- Local bearer-token and OIDC workspace-role authentication.
- Environment/file secret references with plaintext-credential rejection and response redaction.
- Strict adapter/path/endpoint allowlists, TLS-by-default endpoint policy, request/host limits,
  security headers, hashed idempotency keys, and provider-error secret scrubbing.
- A non-root OCI image and Compose profile with independently restartable API and worker services.
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

MVP1 certifies the core through deterministic in-memory contract/fault-injection tests and
exercises Chroma/Qdrant adapter behavior with SDK-compatible fakes. An opt-in integration gate
also runs against real persistent Chroma and embedded Qdrant engines. It creates a 20-document
corpus, embeds it with Chroma's ONNX `all-MiniLM-L6-v2` model, migrates every 384-dimensional
vector through the public service API, verifies every record digest, and compares semantic top-5
results across both databases.

Embedded-engine success does not certify a particular remote Chroma or Qdrant server deployment.
Docker/service tests over HTTP, with pinned server images and supported authentication/TLS modes,
are still required before declaring a specific server/SDK combination production-certified.

The currently supported live feature intersection is:

- one collection per migration;
- dense float vectors;
- one Chroma vector field or one/more Qdrant named dense fields where the target supports them;
- Chroma string IDs mapped to deterministic Qdrant UUIDs when required;
- documents and JSON-compatible metadata;
- a quiesced source when no database snapshot is available;
- streaming upsert (not provider object-storage bulk import).

Namespaces, tenants, arbitrary reference graphs, sparse/binary/multi-vector provider adapters,
bulk import, engine-managed query-quality verification, alias cutover, and change-feed catch-up
remain later milestones. The integration gate checks query quality externally as a release test;
MVP1 does not yet execute application query suites as part of every migration job.

## Validation commands

```bash
PYTHONPATH=src python -m compileall -q src tests
PYTHONPATH=src python -m unittest discover -s tests -v
python -m pip wheel . --no-deps --no-build-isolation --no-cache-dir --wheel-dir .vme/dist
VME_RUN_LIVE_INTEGRATION=1 PYTHONPATH=src python -m pytest -m integration -s
```

The live command requires the `chroma`, `qdrant`, and `dev` optional dependency groups. The
embedding model is downloaded once into Chroma's standard model cache; no external database
service is required for this embedded gate.

## Next implementation slice

1. Add a PostgreSQL service/checkpoint store with replica-safe idempotency and HA lease tests.
2. Add network-layer egress templates, rate-limit examples, metrics export, and published API
   client commands/SDKs.
3. Add Docker-backed Chroma and Qdrant integration tests pinned to supported SDK/server versions.
4. Move semantic top-k overlap from the release gate into a configurable migration verifier and
   add full-dataset bucket digests.
5. Implement Weaviate and the versioned FAISS bundle adapters.
6. Add target alias cutover where the provider supports it.
