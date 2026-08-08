# Vector database migration research

**Research snapshot:** 2026-08-08  
**Status:** Accepted input to the v1 architecture  
**Scope:** Logical data migration between heterogeneous vector stores. This is not a binary backup/restore design.

## Executive findings

There is no lossless, universal one-to-one model shared by vector databases. A useful migration engine must treat portability as a planning problem before it treats it as a copying problem.

The main incompatibilities are:

1. **Vectors are not always one dense float array.** Systems may support named vectors, sparse vectors, binary vectors, multi-vectors, quantized values, or multiple vector fields.
2. **Metric names do not imply identical score semantics.** Euclidean versus squared Euclidean produces the same ordering but different scores and thresholds; dot product may be exposed as a similarity or as a negated distance; cosine may cause automatic normalization.
3. **Record identity and isolation differ.** IDs may be strings, unsigned integers, UUIDs, SQL primary keys, or implicit ordinals. Isolation can be a namespace, tenant, partition, collection, table, or index.
4. **Metadata models differ.** Some systems accept arbitrary JSON, some use typed schemas, and some impose value-type or size restrictions. FAISS has no native document metadata model.
5. **Export guarantees differ.** Cursor/iterator APIs, offset pagination, reconstruct operations, SQL cursors, and point scrolling have different consistency properties while writes continue.
6. **Efficient ingest is target-specific.** Streaming upsert is the portable baseline, while some targets expose faster staged import paths through Parquet or object storage.
7. **The ANN index itself is not portable.** HNSW/IVF/PQ configuration can be mapped only as intent. The target must build its own physical index.

The architecture must therefore use native adapters, a canonical logical record model, runtime capability discovery, an explicit compatibility plan, bounded streaming, resumable idempotent writes, and independent verification.

## Capability matrix

The table describes capabilities that affect migration design, not every product feature. Product behavior is version-dependent, so an adapter must report what it actually supports at runtime rather than relying only on this matrix.

| Store | Logical boundary | Vector shapes relevant to migration | Identity and metadata | Export path | Efficient ingest path | Important portability constraints |
|---|---|---|---|---|---|---|
| **FAISS** | In-process `Index`; persistence is an index file plus application-owned sidecars | Dense float32 and binary indexes; index-specific reconstruction support | Basic indexes use ordinal IDs; explicit IDs require wrappers such as `IndexIDMap`. No native metadata/document store | Reconstruct vectors where the index supports it; application wrappers may hold a separate ID/docstore map | Build/train an index and add vectors; serialize the completed CPU index | The FAISS file alone may be insufficient to recover original IDs, documents, or metadata. Some compressed indexes cannot reproduce original vectors exactly. GPU indexes must be converted before serialization. |
| **Chroma** | Collection | Dense embeddings | Unique string IDs; documents, URIs, and typed metadata | `get()` with `limit`/`offset`, requesting embeddings, documents, and metadata | Batched `upsert`/`add`; HNSW construction is managed by Chroma | Offset pagination is not a stable snapshot during concurrent mutation. Embedding dimension must remain consistent. Distance space is collection configuration (`l2`, `cosine`, or `ip`). |
| **Qdrant** | Collection, optionally partitioned by payload conventions | Dense, sparse, named vectors, and multi-vectors | Point ID is uint64 or UUID; payload is JSON | Point `scroll` with payload and vectors enabled | Idempotent batch upsert; client helpers provide lazy batching, parallelism, and retries; collection indexing can be deferred/tuned during load | Every vector space has a dimension/metric schema. Cosine vectors are normalized on upload. Collection counters can be approximate; exact verification must use the exact count API or a scan. |
| **Weaviate** | Collection and optional tenant | Dense named vectors and version-dependent multi-vector features | UUID object IDs; typed properties and references | Collection iterator/cursor (`after`), with vectors explicitly included; tenants must be enumerated independently | Server-side or client-side batch import; supplied vectors bypass vectorization | A collection vectorizer cannot simply be changed in place. References and typed properties do not map cleanly to flat metadata. Multi-tenant reads and writes require tenant context. |
| **Milvus** | Collection and partition | Dense float/float16/bfloat16/int8, sparse, binary, and newer multi-vector/struct forms | INT64 or VARCHAR primary key; fixed schema with optional dynamic field | Query iterator with selected output fields and configurable batch size | Insert/upsert for streaming; staged object-storage bulk import for high volume | Vector dimensions, field types, metric, primary-key behavior, and schema must be planned before load. Imported data has explicit visibility/load lifecycle behavior. |
| **Pinecone** | Index and namespace | Dense or sparse in the established index model; newer API generations add document/ranking-field variants | String record IDs with metadata | Namespace-aware ID listing followed by fetch, subject to API generation and limits | Batch upsert; object-storage import is recommended for very large loads | Dimension/metric/vector type are index-level decisions. Sparse indexes require dot product. Bulk import has restrictions, including empty/new namespaces and schema/API-generation constraints. Cloud and region are creation-time choices. |
| **pgvector** | PostgreSQL table/partition and configured vector column(s) | `vector`, `halfvec`, `bit`, and `sparsevec`; a table can contain multiple vector columns | Any configured SQL primary key plus arbitrary relational/JSON columns | Server-side/keyset cursor in a repeatable-read transaction | PostgreSQL `COPY`, then build HNSW/IVFFlat indexes after loading | The adapter needs user-supplied table, PK, vector-column, and metadata mappings. Type/dimension and index operator class constrain the destination. Transactional snapshots are stronger than most vector-store iterators. |
| **Elasticsearch** | Index, alias, and document | Multiple `dense_vector` fields in a JSON document; float, byte, and bit element types | String `_id`; arbitrary mapped JSON fields | Point-in-time plus `search_after` for a stable deep scan | Bulk API, followed by refresh/index readiness | Dense-vector dimension and similarity belong to mappings; current dense dimensions and element types have product limits. `_source` and mappings may include far more than portable vector metadata. |

## Metric and score compatibility

The engine must model the mathematical operation separately from the name returned by an SDK.

| Canonical operation | Example product spellings | Ordering | Migration implication |
|---|---|---|---|
| Cosine similarity | `COSINE`, `cosine` | Larger is closer | Some products expose `1 - cosine` as a distance. Preserve the configured vector space and translate verification scores. |
| Cosine distance | Weaviate/Chroma distance form | Smaller is closer | Ranking-equivalent to cosine similarity after score transformation, not numerically identical. |
| Dot product | `IP`, `dotproduct`, `dot` | Usually larger is closer | Weaviate exposes negative dot distance. Cosine and dot are ranking-equivalent only for unit-normalized vectors. |
| Euclidean (L2) | `L2`, `euclidean` | Smaller is closer | Some stores report Euclidean while others report squared Euclidean. Ranking is preserved, threshold values are not. |
| Squared Euclidean | `l2-squared`, Chroma `l2`, Pinecone Euclidean score | Smaller is closer | Never copy application score thresholds without translating them. |
| Manhattan/Hamming/Jaccard/BM25 | Product-specific | Metric-specific | A migration must fail or invoke an explicit transform/re-embedding policy when the target lacks the operation. |

Metric planning rules:

- Default to an exact metric mapping.
- Allow a **ranking-equivalent** mapping only when its preconditions are proven and recorded (for example, Euclidean to squared Euclidean, or cosine to dot on verified unit vectors).
- Never silently normalize, quantize, truncate, pad, or re-embed vectors.
- Record score transformations in the migration report so application thresholds can be updated during cutover.

## Export and consistency observations

### Stable scanning

- Weaviate documents cursor iteration specifically for copying/migrating objects and can include named vectors.
- Qdrant's scroll API can return IDs, payloads, and vectors and continue from an opaque offset.
- Milvus exposes query iterators with a batch size and requested output fields.
- Chroma exposes offset pagination. A mutable collection can shift under offset pagination, so strict migrations require a quiesced source or a source snapshot outside the adapter.
- PostgreSQL can supply a transactionally consistent snapshot. Elasticsearch can use a point-in-time view with `search_after`.
- FAISS is naturally static when reading a file, but successful extraction depends on the concrete index and any application sidecars.

No generic cursor establishes a universal database snapshot. The migration plan must classify source consistency as one of:

- `snapshot`: the adapter can hold or consume an immutable snapshot;
- `quiesced`: writes are stopped for the scan;
- `bounded`: the source exposes a high-water mark that bounds the scan;
- `best_effort`: concurrent changes may be missed or duplicated.

### Idempotence and resume

Qdrant documents point-loading APIs as idempotent for the same ID. Pinecone upsert overwrites an existing record ID. Other targets have an upsert operation or can be made idempotent by writing into a fresh staging collection. This supports a portable **at-least-once read/write, checkpoint-after-ack** execution model.

Targets that cannot safely upsert must be restricted to an empty staging target in v1. Resuming an append into a non-idempotent existing target is unsafe and should be rejected.

### Bulk ingestion

Two data paths are needed:

1. **Streaming upsert**, supported by every certified adapter and used for normal migrations and catch-up passes.
2. **Staged bulk import**, selected only when the sink advertises it. Pinecone and Milvus can import prepared object-storage files; pgvector can use `COPY`; FAISS can build locally. This path needs staging-file lifecycle, job polling, and target-specific validation.

The common orchestrator should not embed database-specific performance switches. Sink adapters should expose write strategies and tuning hints.

## Metadata, IDs, and isolation

### Metadata

The canonical representation should accept JSON-compatible values, while the compatibility planner compares those values with destination rules. Typed schemas, nested JSON, arrays, nulls, blobs, and cross-references require explicit mapping policies.

Safe default: fail the plan when a populated source field cannot be represented. Dropping or stringifying data requires a field-level opt-in and must appear in the final loss report.

### IDs

The canonical model must retain the source ID type. Destination mappings can be:

- `preserve` (default when compatible),
- `stringify`,
- `deterministic_uuid`, or
- `surrogate_with_sidecar`.

Mappings must be deterministic for resume. A sidecar mapping belongs in the migration state/artifacts by default, not silently inside user metadata.

### Namespaces, tenants, and partitions

These are all isolation scopes, but they are not semantically identical. The canonical model must retain source scope separately from the record. The plan maps a source scope to a target collection/index, namespace, tenant, partition, or metadata discriminator. Flattening scopes can create ID collisions and must include a collision check.

## Product-specific notes for initial adapters

### FAISS

FAISS should be treated as a family of formats, not one uniform database adapter. The first adapter should support a declared bundle containing:

- a CPU FAISS index;
- a stable external-ID mapping;
- optional documents and metadata sidecars;
- a bundle manifest with index type, dimension, metric, and embedding provenance.

Reading an arbitrary `.faiss` file without sidecars can migrate only what can be reconstructed and identified. The planner must describe that limitation before execution.

### Chroma

Use the native Chroma client, request embeddings explicitly, and write with `upsert`. Do not use a LangChain vector-store wrapper as the adapter API because it hides collection configuration and full-record export behavior.

### Qdrant

Use `scroll` with both payload and vectors. Prefer SDK upload helpers or adaptive batched upsert. For large fresh loads, the sink can lower/defer indexing and restore the requested index configuration during finalize.

### Weaviate

Enumerate tenants and named vectors from schema discovery. Read through the collection iterator with vectors included. For imported vectors, configure the target to avoid accidental re-vectorization unless the plan explicitly selects re-embedding.

## Primary sources

- FAISS: [Getting started](https://github.com/facebookresearch/faiss/wiki/Getting-started), [index types and reconstruction](https://github.com/facebookresearch/faiss/wiki/Faiss-indexes), [index I/O](https://github.com/facebookresearch/faiss/wiki/Index-IO%2C-cloning-and-hyper-parameter-tuning)
- Chroma: [collection API](https://docs.trychroma.com/reference/python/collection), [query and get](https://docs.trychroma.com/docs/querying-collections/query-and-get), [collection configuration](https://docs.trychroma.com/docs/collections/configure), [adding data](https://docs.trychroma.com/docs/collections/add-data)
- Qdrant: [collections](https://qdrant.tech/documentation/manage-data/collections/), [points, scrolling, and upload](https://qdrant.tech/documentation/manage-data/points/), [payload](https://qdrant.tech/documentation/concepts/payload/), [bulk upload](https://qdrant.tech/documentation/database-tutorials/bulk-upload/)
- Weaviate: [read all objects](https://docs.weaviate.io/weaviate/manage-objects/read-all-objects), [batch import](https://docs.weaviate.io/weaviate/manage-objects/import), [bring your own vectors](https://docs.weaviate.io/weaviate/starter-guides/custom-vectors), [distance metrics](https://docs.weaviate.io/weaviate/config-refs/distances)
- Milvus: [create a collection](https://milvus.io/docs/create-collection.md), [iterators](https://milvus.io/docs/with-iterators.md), [bulk import](https://milvus.io/docs/import-data.md), [metric types](https://milvus.io/docs/metric.md)
- Pinecone: [create an index](https://docs.pinecone.io/guides/index-data/create-an-index), [indexing and namespaces](https://docs.pinecone.io/guides/index-data/indexing-overview), [list record IDs](https://docs.pinecone.io/guides/manage-data/list-record-ids), [upsert](https://docs.pinecone.io/guides/index-data/upsert-data), [bulk import](https://docs.pinecone.io/guides/index-data/import-data)
- pgvector/PostgreSQL: [pgvector project documentation](https://github.com/pgvector/pgvector), [PostgreSQL repeatable-read isolation](https://www.postgresql.org/docs/current/transaction-iso.html), [PostgreSQL `COPY`](https://www.postgresql.org/docs/current/sql-copy.html)
- Elasticsearch: [dense vector mapping](https://www.elastic.co/guide/en/elasticsearch/reference/current/dense-vector.html), [bulk API](https://www.elastic.co/guide/en/elasticsearch/reference/current/docs-bulk.html), [point-in-time reads](https://www.elastic.co/guide/en/elasticsearch/reference/current/point-in-time-api.html), [search and `search_after`](https://www.elastic.co/guide/en/elasticsearch/reference/current/search-search.html)
