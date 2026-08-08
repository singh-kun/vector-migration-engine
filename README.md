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
- a shared CLI and Python API.

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

## Development

Run the dependency-free core test suite:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

- [Architecture and implementation plan](docs/design/v1-architecture.md)
- [MVP1 implementation and certification boundary](docs/mvp1.md)
- [Vector database capability research](docs/research/vector-database-capability-matrix.md)
