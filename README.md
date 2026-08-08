# Vector Migration Engine (VME) 🚀

VME is a capability-aware framework for moving embeddings, documents, metadata, and IDs between vector databases without loading an entire dataset into memory.

MVP1 provides:

- preflight schema and capability planning;
- native Chroma and Qdrant adapters;
- bounded, adaptive batch execution;
- deterministic ID and vector-name transformations;
- SQLite WAL checkpoints and safe resume;
- transient-error retry and throttling backoff;
- exact count and deterministic read-back verification;
- a shared CLI and Python API;
- an asynchronous REST/OpenAPI service with durable plan/job resources;
- bearer-token or OIDC workspace RBAC, secret references, worker leases, and audit events;
- a non-root container and separate API/worker Compose profile.

## Install

```bash
pip install -e .
pip install -e ".[chroma,qdrant]"  # install the provider SDKs you need
```

Python 3.11 or newer is required.

## Run the local MVP example

```bash
vme adapters
vme plan --config examples/migration.memory.json --out .vme/memory-plan.json
vme run \
  --config examples/migration.memory.json \
  --plan .vme/memory-plan.json \
  --state .vme/memory-state.sqlite3
vme status <job-id> --state .vme/memory-state.sqlite3
```

For service-to-service configuration, start with [the Chroma-to-Qdrant example](examples/migration.chroma-to-qdrant.yaml). Secret values use `env:VARIABLE_NAME` references and are never stored in a plan or checkpoint.

MVP1 assumes the source is quiesced when an adapter cannot provide a true snapshot. It never deletes source data, and it refuses an existing destination unless it is resuming a known job or `allow_existing` is explicitly enabled.

## Run as a service

Install service dependencies and start the API plus its durable worker:

```bash
pip install -e ".[server,chroma,qdrant]"
export VME_API_TOKEN="replace-with-a-long-random-token"
docker compose up --build
```

The API listens on `http://127.0.0.1:8080`; interactive OpenAPI documentation is available at
`http://127.0.0.1:8080/docs`. Create resources in this order:

1. `POST /v1/connection-profiles` for the source and destination.
2. `POST /v1/migrations` to bind profiles, collections, mappings, and limits.
3. `POST /v1/plans`, then poll its `Location` until the immutable assessment is ready.
4. `POST /v1/jobs`, then poll status/events and fetch `/v1/jobs/{id}/report`.

Mutating requests require an `Idempotency-Key`; authenticated requests use
`Authorization: Bearer $VME_API_TOKEN`. Credentials must be `env:NAME` or
`file:/mounted/path#key` references—plaintext secret-looking fields are rejected.

MVP1 is production-oriented for a single-node, self-hosted data plane. The Compose profile keeps
API lifecycle separate from migration execution and persists both service resources and
record-level checkpoints. PostgreSQL shared state, multiple replicas, endpoint egress policy, and
HA enterprise certification remain the next deployment slice; see the
[service architecture](docs/design/mvp1-service-architecture.md) for the exact boundary.

## Development

Run the fast test suite:

```bash
PYTHONPATH=src python -m pytest -q
```

- [Architecture and implementation plan](docs/design/v1-architecture.md)
- [MVP1 service architecture and implementation boundary](docs/design/mvp1-service-architecture.md)
- [MVP1 implementation and certification boundary](docs/mvp1.md)
- [Vector database capability research](docs/research/vector-database-capability-matrix.md)
