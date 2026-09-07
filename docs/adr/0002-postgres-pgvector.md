# 0002 — PostgreSQL system of record, deferred pgvector, and the Phase-1 persistence foundation

Status: Accepted by Persons A, B, and C
Date: 2026-08-24
Amended: 2026-09-04 — narrowed from a direction ("use PostgreSQL and pgvector") into a
concrete Phase-1 persistence decision the team reviewed before any SQLAlchemy model,
Alembic migration, or database dependency is added.

## Status detail

Person A (ingestion review steward) authored this amendment because the first persistence
tables (`source_items`, `document_snapshots`) are ingestion-owned. Persons B (intelligence)
and C (delivery) are review stewards: the decision introduces a shared database kernel
(`shared/db/` — engine, session factory, `MetaData`; section 12.3) that all three modules
build on, fixes a shared contract (`shared/schemas.py` gains no new resource type here, but
the ORM must mirror its existing models exactly), touches a shared integration file boundary
(`delivery/api/app.py` wiring), and unblocks [ADR 0008](0008-cursor-pagination-contract.md)
PR 4 (`GET /v1/updates`), tracked in issue #47 with Person B as the active author and
Persons A and C as review stewards.

This amendment added **no code, no dependency, no migration, no route, and no collector.** It
records decisions and their rejected alternatives so the implementation PR builds a
pre-agreed shape instead of inventing one under review pressure — the same discipline
[ADR 0007](0007-uuid-v7-identifier-strategy.md) and ADR 0008 already used.

**Acceptance record.** All three module owners have explicitly accepted this ADR as it
stands on `main` today, including the Finding 1/Finding 2 concurrency-ordering corrections
merged through PR #59. The decisive, currently-standing evidence for Persons B and C is their
formal approval of PR #59 — the PR that produced the exact text now on `main` — not their
earlier reviews of PR #48, one of which (Person C's second PR #48 review) was superseded by a
later push and no longer stands as a formal approval on its own:

- **Person A / ingestion:** author and active implementer of this amendment and its PR #59
  correction; acceptance is recorded by authorship and by opening the persistence-foundation
  implementation work against this exact text.
- **Person B / intelligence (`@SujinJK`):** formally approved PR #59's ADR 0002/0011
  concurrency-ordering correction — "All CI checks are green and the test suites are
  comprehensive. **Approved to merge!**" —
  <https://github.com/Patric-1613/Virtusa_CaPStone/pull/59#pullrequestreview-5116759813>.
  (Earlier, also formally approved the original amendment on PR #48:
  <https://github.com/Patric-1613/Virtusa_CaPStone/pull/48#pullrequestreview-5112901833>, and
  recorded "Decision: Accept." in review comments at
  <https://github.com/Patric-1613/Virtusa_CaPStone/pull/48#issuecomment-5539469734>.)
- **Person C / delivery (`@chamath-wijayasundara`):** formally approved PR #59's
  concurrency-ordering correction — "The documentation is consistent with the transaction,
  immutability, pagination and delivery boundaries... Approved from the Delivery review
  perspective." —
  <https://github.com/Patric-1613/Virtusa_CaPStone/pull/59#pullrequestreview-5116058377>.
  (Earlier, reviewed the original PR #48 amendment across two rounds — first requesting five
  changes, then confirming all five were resolved — at
  <https://github.com/Patric-1613/Virtusa_CaPStone/pull/48#pullrequestreview-5112076827> and
  <https://github.com/Patric-1613/Virtusa_CaPStone/pull/48#pullrequestreview-5112827871>.)

No content in this ADR has changed since the PR #59 commit both reviewers approved
(`git log -- docs/adr/0002-postgres-pgvector.md` shows `006965f` as the latest commit
touching this file); this status update records that already-given acceptance, not a new
round of review.

## Phase-1 decision summary

The following is the exact Phase-1 decision Persons A, B, and C have accepted:

| Concern | Phase-1 decision |
|---|---|
| Database and vector search | PostgreSQL 17 is the system of record; pgvector remains planned but is not part of the first persistence PR. |
| Runtime database access | SQLAlchemy 2 async sessions over Psycopg 3. One shared async engine, `async_sessionmaker`, and `MetaData` live in `shared/db/`; every module registers its ORM models against that one metadata. Alembic uses the same driver through the standard async migration bridge. |
| Initial tables | `source_items` and immutable `document_snapshots` only. |
| IDs and identity | Application-generated UUID v7. Five `source_items` fields are immutable after insertion and enforced by a storage trigger: `id`, `first_fetched_at`, `dedupe_key`, `source_id`, `canonical_url`. |
| Transactions | The ingestion service owns one `AsyncSession` transaction per ingested item; the repository is bound to it, may flush, and never commits or rolls back on its own. The service commits once, after all three writes succeed; any exception rolls the whole item back. |
| Snapshot ownership/latest pointer | Composite FK proves ownership; a single conditional update advances `latest_snapshot_id` **and** the allowed-mutable source-item metadata together, only when the candidate snapshot's `(fetched_at, id)` is newer than the currently selected snapshot. Finding the source item on an existing `dedupe_key` never writes metadata by itself — an older or duplicate candidate updates neither the pointer nor the metadata. |
| Lists | `authors` and normalized `tags` use PostgreSQL `text[]` in Phase 1; the normalization rules are new behaviour implemented in the persistence PR. |
| Immutability | Restricted repository methods plus PostgreSQL triggers: a row-level `BEFORE UPDATE` guard on the five `source_items` identity fields, and row-level `BEFORE UPDATE`/`BEFORE DELETE` plus a statement-level `BEFORE TRUNCATE` guard on `document_snapshots`. Triggers cover ordinary DML while enabled; a restricted runtime database role is a production gate. |
| Cross-module reads | A narrow async read Protocol lives in `shared/`; its PostgreSQL adapter remains ingestion-owned. |
| Database-infrastructure ownership | `shared/db/` owns the engine, session factory, and `MetaData`/naming convention (no auto-connect on import). Each module owns its own `<module>/db/models.py` + `repository.py`; repositories take an injected `AsyncSession` and never build a competing engine or pool. `alembic/` and the single `alembic_version` history are shared infrastructure; Person A authors the first migration (ingestion tables), later module owners author theirs. |
| Configuration | Typed `DatabaseConfig` lives in `shared/config.py`; secrets remain environment-only. |
| CI | The existing test job gets a pinned `postgres:17` service and runs migrations plus real integration tests. |
| API pagination support | The foundation migration includes `(first_fetched_at DESC, id DESC)`. The endpoint remains a separate PR. |

## Context

### What already exists (verified against the tree at merge `06d11c9`)

| Fact | Evidence |
|---|---|
| No persistence code of any kind. No SQLAlchemy, Alembic, or driver dependency. | `pyproject.toml` `[project].dependencies` — `PyYAML`, `pydantic`, `anthropic`, `langgraph`, `uuid-utils`, `fastapi`; no `sqlalchemy`/`alembic`/`psycopg`. `src/ai_daily_digest/ingestion/` contains only `__init__.py` and `README.md`. |
| The shared contract is Pydantic-only today. | `src/ai_daily_digest/shared/schemas.py` — `SourceItem`, `DocumentSnapshot`, `Change`, `Digest`, etc. `SourceItem` has 14 fields including `first_fetched_at: OrderingTimestamp = Field(frozen=True)` (`schemas.py:116`), `authors`/`tags: list[str]` (`schemas.py:125-126`), `latest_snapshot_id: Uuid7Id \| None` (`schemas.py:117`). |
| IDs are already application-generated UUID v7 via one factory. | `src/ai_daily_digest/shared/ids.py::new_id()` (returns stdlib `uuid.UUID` from `uuid_utils.compat.uuid7()`); `type Uuid7Id = UUID7`. [ADR 0007](0007-uuid-v7-identifier-strategy.md) Decision: "PostgreSQL native `uuid` column storage for every UUID-typed field, once tables exist … PostgreSQL ≤17 has no native `uuidv7()`; … generation is application-side." |
| Model-level immutability of the six protected ordering columns already ships. | `schemas.py:100,116` (`SourceItem.id`, `first_fetched_at`), `:439,447` (`Change.id`, `detected_at`), `:520,521` (`Digest.id`, `digest_date`) all `Field(frozen=True)`. `tests/unit/test_protected_ordering_fields.py` (merged in PR #45) statically forbids `model_copy(update=...)` on any of them. |
| ADR 0008 requires three immutability layers and explicitly defers the database one. | [ADR 0008](0008-cursor-pagination-contract.md) §5.D: model freezing (done), repository restriction (no update path), and **storage-level enforcement** — "A normal PostgreSQL `CHECK` constraint is **not sufficient**: it … cannot compare `OLD` with `NEW`." "a `BEFORE UPDATE` trigger … or appropriately restricted column-level `UPDATE` privileges … The exact mechanism is chosen in the future database schema / migration PR." |
| `/v1/updates` sorts by `(first_fetched_at DESC, id DESC)`, filters on `publisher` and `source_id` only, and reads `limit + 1` rows. | ADR 0008 §4, §8, §10. `tags` and any `published_*` range filter for `/v1/updates` are deferred (§8). |
| The pagination codec is pure and framework-free; the repository takes canonical typed filters, never raw request strings. | `src/ai_daily_digest/delivery/api/pagination.py` — `CanonicalFilters`, `canonicalize_filters`, `CursorCodec`, `Page[T]`. ADR 0008 §7. |
| The FastAPI foundation exists with a typed, injected readiness boundary. | `src/ai_daily_digest/delivery/api/dependencies.py` — `ReadinessProbe` Protocol (`async def is_ready() -> bool`), `ReadinessRegistry.evaluate()` catches every probe exception and logs only `exception_type` + `request_id` (`dependencies.py:42-73`), `build_readiness_registry()` raises when a required dependency has no probe (`dependencies.py:90-93`). `routes/health.py` exposes only `{name, status}` publicly. |
| ADR 0010 governs how infrastructure enters routes and what the first persistence PR may contain. | [ADR 0010](0010-fastapi-openapi-contract-authority.md): "Infrastructure enters route handlers through typed FastAPI dependencies and narrow protocols." "Do not add empty repositories, services, adapters, or other speculative abstractions." "Naming a dependency as required without providing its probe is a configuration error and must prevent normal startup." "First domain vertical slice … no Postgres adapter exists merely to raise `NotImplementedError`; no public route merges without a functional configured data source." |
| A synchronous cross-module boundary precedent already lives in `shared/`. | `src/ai_daily_digest/shared/snapshot_resolver.py` — a `Protocol` in `shared/` "a protocol ingestion is eventually expected to provide a real implementation of cannot live inside an intelligence-private module." Its docstring: "a future database-backed implementation may need to be asynchronous — that is a separate design decision this module does not make or preclude." |
| `DATABASE_URL` is already an empty-placeholder environment variable. | `.env.example:2` — `DATABASE_URL=`. `ANTHROPIC_API_KEY` is the only env var any code reads today (`intelligence/llm.py:59`). |
| CI runs unit + contract + integration tests in one job; there is no database service. | `.github/workflows/ci.yml` — `tests` job runs `uv run pytest -m "not live"` (which **includes** `integration` and `e2e`). `Makefile:22` inner loop excludes `integration and e2e and live`. `tests/integration/` contains only `__init__.py`. `pytest` markers are declared in `pyproject.toml:114-119`. |
| ADR 0002's own original text already flags the operational follow-ups. | This file's prior "Deferred operational decisions" — backups, retention, ordinary indexes, pgvector metric/index type, vector rebuild procedure. Preserved and extended below. |

### The decision this amendment must make

`GET /v1/updates` (ADR 0008 PR 4) cannot ship without "a functional configured repository
adapter" (ADR 0008 §13; ADR 0010 first-vertical-slice rule). That adapter needs: a database,
an ORM, a migration tool, a driver, a repository protocol, a configuration boundary, a
readiness probe, and a real integration test suite. This ADR fixes each of those before the
implementation PR opens.

## Decision

### 1. PostgreSQL remains the durable system of record

Unchanged from the original decision. Normalized records, constraints, relationships,
provenance, and (later) subscription data live in PostgreSQL. The vector store is a derived
index and is never the source of truth (`AGENTS.md`; `docs/ARCHITECTURE.md` Storage rules).

### 2. pgvector stays the future derived vector index, deferred from the first persistence PR

pgvector remains the intended embedding index, in the same PostgreSQL instance, per the
original decision. It is **out of scope for the first persistence PR**:

- the first Alembic migration must **not** run `CREATE EXTENSION vector` and must **not**
  create any embedding table;
- keeping it out means the migration applies cleanly on a stock `postgres:17` image, with no
  `pgvector/pgvector` image required in CI or local development yet;
- when embeddings land, that PR either switches the test image to `pgvector/pgvector:pg17`
  or adds `CREATE EXTENSION IF NOT EXISTS vector` as its own reviewed migration, and chooses
  the distance metric and index type from measured corpus behaviour (already a deferred
  operational decision below).

### 3. Use SQLAlchemy 2 and Alembic

Confirmed from `docs/ARCHITECTURE.md`'s technology baseline. SQLAlchemy 2.0 typed ORM
(`DeclarativeBase`, `Mapped[...]`, `mapped_column(...)`) for table definitions and queries,
with one application-wide `MetaData`/naming convention in `shared/db/` (section 12.3); Alembic
for every schema change, as shared infrastructure with a single migration timeline
(section 12.5). No raw-SQL schema management; no ORM-less query builder. `AGENTS.md`:
"Database changes use Alembic migrations; never edit production tables manually."

### 4. Asynchronous SQLAlchemy engine — selected

**Decision: use SQLAlchemy's async engine (`create_async_engine`,
`async_sessionmaker`, `AsyncSession`) for the application runtime path** — the Delivery API
and the async collectors — with synchronous Alembic driven through the standard
`connection.run_sync(...)` bridge in `alembic/env.py`.

Why, against the standard "prefer async unless repository evidence shows a simpler reliable
alternative":

- **FastAPI route handlers here are already `async def`** (`routes/health.py`). A synchronous
  database call inside an async handler blocks the event loop; ADR 0010 and this task both
  forbid that.
- **Collectors are async by design.** `docs/ARCHITECTURE.md` selects `httpx` for "async
  support, timeouts, connection pooling"; the collection flow is concurrent with bounded
  parallelism. A synchronous session in that path would need a threadpool hop on every
  write.
- **The "simpler alternative" is not actually simpler here.** Synchronous SQLAlchemy behind
  `fastapi.concurrency.run_in_threadpool` still splits the mental model ("the ORM is sync but
  every call is awaited through a thread") and still needs a driver that also does async for
  the collectors, or a second driver. Psycopg 3 (section 5) gives one driver for both sync
  and async, so choosing async is **not** more dependencies than choosing sync.
- **The one genuinely synchronous consumer is Alembic**, and that is a solved problem: an
  async `env.py` calls `asyncio.run(...)` and `connection.run_sync(context.run_migrations)`.
  Autogenerate and `upgrade`/`downgrade` run exactly as they do in a sync project.
- The existing `shared/snapshot_resolver.py` precedent — deliberately synchronous "for this
  phase" — **explicitly reserves** the async decision for "a future database-backed
  implementation." This is that decision. `SnapshotResolver` itself stays synchronous until a
  database-backed resolver is actually built (not in this PR); it is not retrofitted here.

**Rejected — synchronous SQLAlchemy + threadpool offload:** viable and lower learning curve,
but pushes an executor hop into every request and every collector write, and still needs
psycopg 3 for the collectors, so it trades a small conceptual simplification for a worse
runtime shape.

**Rejected — sync in ingestion, async in delivery, over one URL:** two session factories and
two mental models over one engine; the collector path does not benefit from staying sync once
`httpx` is already async.

The worker/collector uses an `async def main()` entry point invoked with `asyncio.run()`. It
builds one engine and `async_sessionmaker` from `shared/db/engine.py` (section 12.3) — the
same construction the API uses, one pool per process. The existing synchronous intelligence
pipeline may run as an in-process step in that dedicated worker after database inputs have
been loaded; it must not perform synchronous database I/O from an async FastAPI request
handler.

### 5. Psycopg 3 as the PostgreSQL driver — selected

**Decision: `psycopg` version 3** (`postgresql+psycopg://` DSN), installed as
`psycopg[binary]` for CI and local development.

- Psycopg 3 has **native sync and async** support in one package — the async runtime and the
  synchronous Alembic step use the same driver.
- It is the driver SQLAlchemy 2.0's own documentation now leads with for PostgreSQL.
- Actively maintained; `psycopg2` is in maintenance mode and is sync-only; `asyncpg` is
  async-only (forcing a second sync driver for Alembic) and uses a different parameter style.
- `psycopg[binary]` ships self-contained wheels — no system `libpq` needed in CI or on a
  developer laptop.

**Rejected — `asyncpg`:** fastest raw async driver, but async-only, so Alembic would need
`psycopg2`/`psycopg` anyway; two drivers for one database.

**Rejected — `psycopg2`:** sync-only, maintenance mode.

**Deployment follow-up (not this PR):** a production container may prefer `psycopg[c]` or a
system-`libpq` build over `psycopg[binary]`. That belongs to the deployment ADR, alongside
the `uuid-utils` deployment-platform validation ADR 0007 already defers.

### 6. Application generates UUID v7; PostgreSQL stores, never generates

- Every `id` and every UUID foreign key is generated by `shared.ids.new_id()` in application
  code **before** the row is written (ADR 0007).
- The database columns are **native `uuid`** (SQLAlchemy `Uuid` / `sqlalchemy.dialects.
  postgresql.UUID(as_uuid=True)`; Python attribute is a `uuid.UUID`).
- Columns have **no `DEFAULT`**, **no `server_default`**, **no `gen_random_uuid()`**, and
  **no `uuidv7()`** (PostgreSQL 18 only, and irrelevant — generation is application-side).
- A row inserted without an `id` is a bug, not a database-filled blank.
- Integration tests assert a `new_id()` value round-trips through the `uuid` column unchanged
  and reads back as canonical lowercase-hyphenated form (ADR 0007 validation expectations).

### 7. Phase-1 database scope

The first persistence PR creates **only**:

- the `source_items` table;
- the `document_snapshots` table;
- their integrity constraints, foreign keys, indexes required by their documented sort key,
  and the Phase-1 immutability triggers (section 11);
- Alembic configuration and the single initial migration. Alembic creates and owns its own
  `alembic_version` table; this revision neither creates nor drops it.

It creates **no** `facts`, `changes`, `change_sets`, `digests`, `digest_claims`,
`subscriptions`, `email_deliveries`, `collection_runs`, `events`, `chat_sessions`,
`embeddings`, or any vector table. Each of those arrives with the feature that needs it, in
its own migration, owned by the module that owns it.

### 8. `source_items` schema

Covers the current `SourceItem` Pydantic contract (`schemas.py:86-127`) field-for-field.

| Column | PostgreSQL type | Constraints / notes |
|---|---|---|
| `id` | `uuid` | `PRIMARY KEY`. No default. **Immutable** (section 11). |
| `dedupe_key` | `text` | `NOT NULL`, `UNIQUE`. sha256 of the normalized canonical URL. Immutable after insertion in Phase 1. A future canonicalization-policy change requires an explicit migration/backfill plan rather than an ordinary upsert rewriting identity. |
| `source_id` | `text` | `NOT NULL`. `sources.yaml` slug (e.g. `openai_news`) — a config key, never a UUID (ADR 0007). Immutable after insertion in Phase 1 (section 11). |
| `publisher` | `text` | `NOT NULL`. |
| `title` | `text` | `NOT NULL`. |
| `canonical_url` | `text` | `NOT NULL`. Validated as a URL by the Pydantic boundary (`HttpUrl`); stored as `text`. Immutable after insertion in Phase 1 because it is the input to `dedupe_key`. |
| `published_at` | `timestamptz` | `NULL`. Publisher-controlled; may be corrected by the publisher. |
| `updated_at` | `timestamptz` | `NULL`. Mutable. |
| `first_fetched_at` | `timestamptz` | `NOT NULL`. UTC, microsecond precision. **Immutable** (section 11). The `/v1/updates` business sort value. |
| `latest_snapshot_id` | `uuid` | `NULL`. Part of the composite ownership FK described in section 9. **Mutable** — advances only when a newer snapshot for this same source item arrives. |
| `event_id` | `text` | `NULL`. Human-readable grouping key; not a generated resource id (ADR 0007). |
| `authors` | `text[]` | `NOT NULL DEFAULT '{}'`. See section 10. |
| `tags` | `text[]` | `NOT NULL DEFAULT '{}'`. See section 10. |
| `language` | `text` | `NOT NULL DEFAULT 'en'`. |

Indexes:

- `PRIMARY KEY (id)`;
- `UNIQUE (dedupe_key)`;
- **`(first_fetched_at DESC, id DESC)` btree** — the exact keyset order `/v1/updates` scans
  (ADR 0008 §4). Included in the foundation migration because PR 4 cannot serve pages without
  it and this ADR keeps persistence to one foundation PR.
- Filter-supporting indexes on `publisher` / `source_id` (or a composite
  `(publisher, first_fetched_at DESC, id DESC)`) are **deferred** to a measured follow-up —
  consistent with this ADR's existing "ordinary PostgreSQL indexes … chosen from measured
  … query behavior" deferral.

### 9. `document_snapshots` schema and foreign-key behaviour

Covers the current `DocumentSnapshot` contract (`schemas.py:130-143`).

| Column | PostgreSQL type | Constraints / notes |
|---|---|---|
| `id` | `uuid` | `PRIMARY KEY`. No default. **Immutable** (section 11). |
| `source_item_id` | `uuid` | `NOT NULL`. `FK → source_items(id) ON DELETE RESTRICT ON UPDATE RESTRICT`. |
| `fetched_at` | `timestamptz` | `NOT NULL`. UTC, microsecond precision. |
| `content_hash` | `text` | `NOT NULL`. sha256 of the cleaned content — a hash, never a UUID. |
| `content_text` | `text` | `NOT NULL`. Delivery projections may omit it from public list responses; omission from an API response is not a nullable storage value. Intelligence grounding requires stored content. |
| `raw_location` | `text` | `NULL`. Internal storage reference — never exposed in a list response. |
| `etag` | `text` | `NULL`. |
| `last_modified` | `text` | `NULL`. |
| `collector_version` | `text` | `NULL`. |

Constraints and indexes:

- `PRIMARY KEY (id)`;
- **`UNIQUE (source_item_id, content_hash)`** — the snapshot idempotency key; a re-fetch that
  produces identical content must not create a second row;
- the foreign key above;
- `UNIQUE (id, source_item_id)`, which is the target of the composite latest-snapshot ownership
  foreign key below (the primary key already makes `id` unique, but PostgreSQL requires a matching
  unique key for this two-column reference);
- **whole-row immutability** via a row-level `BEFORE UPDATE` trigger, plus a row-level
  `BEFORE DELETE` trigger that always raises and a statement-level `BEFORE TRUNCATE` trigger
  that always raises (section 11);
- a `(source_item_id, fetched_at DESC)` history index is **deferred** — no Phase-1 endpoint
  reads snapshot history.

**Foreign-key behaviour — historical snapshots are never silently deleted:**

- `document_snapshots.source_item_id → source_items.id` is `ON DELETE RESTRICT`
  (**not** `CASCADE`). A `source_item` that still has snapshots cannot be deleted; deleting a
  source item is not a Phase-1 operation at all (provenance is immutable — `AGENTS.md`), and
  if one is ever attempted it must fail loudly rather than take the snapshots with it.
- `(source_items.latest_snapshot_id, source_items.id) →
  (document_snapshots.id, document_snapshots.source_item_id)` is a composite `ON DELETE
  RESTRICT ON UPDATE RESTRICT` foreign key. It proves at the storage layer that an item's
  `latest_snapshot_id` belongs to that same item; a single-column reference to
  `document_snapshots.id` would only prove that some snapshot exists and could silently attach
  another item's snapshot.
- The two tables reference each other. The migration resolves this without a `DEFERRABLE`
  constraint by ordering DDL: create `source_items` **without** the `latest_snapshot_id` FK →
  create `document_snapshots` with its FK to `source_items` → `ALTER TABLE source_items ADD
  CONSTRAINT … FOREIGN KEY (latest_snapshot_id, id) REFERENCES
  document_snapshots(id, source_item_id)`. At
  runtime the write order (section 13) inserts the item first with `latest_snapshot_id NULL`,
  so no deferral is needed.

`DocumentSnapshot` is tightened in the implementation PR: `fetched_at` rejects naive
datetimes and normalizes aware values to UTC; `content_text` becomes a required `str`; and
the model is frozen as a whole. This aligns the shared boundary with the existing contract
statement that every stored snapshot has content and with the database's immutable, non-null
row. Delivery list schemas remain separate projections and therefore do not expose snapshot
bodies.

### 10. `authors` and `tags` — PostgreSQL `text[]` arrays

**Decision: store `authors` and `tags` as `text[]` array columns**, `NOT NULL DEFAULT
'{}'`, element order preserved to match `list[str]` and the JSON contract.

Trade-off, stated rather than assumed:

| Option | For | Against | Verdict |
|---|---|---|---|
| **`text[]` array** | One row, no join; order preserved; native array operators (`@>`, `&&`) and a future GIN index if a `tags` filter is added; element typing kept. | No referential integrity; no cross-item tag dimension (rename/merge/counts) without a later migration. | **Chosen for Phase 1.** |
| `jsonb` | Flexible; GIN-indexable. | Untyped elements; loose duplicate/order semantics; overkill for a homogeneous string list. | Rejected — these are string lists, not documents. |
| `authors` / `tags` child tables (or a `tags` dimension + `source_item_tags` join) | Proper normalization; FK integrity; easy dedup and faceting. | Extra tables, joins, and repository code for a capability **no Phase-1 endpoint needs** — ADR 0008 §8 defers the `tags` filter and never filters `authors`. | Rejected for Phase 1; revisit if tags become a managed, filterable dimension. |

Element normalization will be implemented at the ingestion normalization boundary in the
implementation PR — not as a database constraint. It is **new Phase-1 behaviour, not existing
executable functionality**: `src/ai_daily_digest/ingestion/` currently holds only stubs, so the
rules below are a fresh specification for Person A's normalizer, not a description of code that
already runs. Authors are Unicode-NFC-normalized, trimmed, emptied values removed, and exact
duplicates removed while first-seen order and case are preserved. Tags get the same treatment and
are additionally case-folded before exact deduplication. Migrating `tags` to a dimension + join
table later is a localized, additive migration (new tables, backfill, swap the repository read
path) — it does not disturb any `source_items` identity field.

### 11. Ordering-column and snapshot immutability — Phase-1 storage mechanism

ADR 0008 §5.D requires three independent layers. Layer 1 already ships and layer 2's design
is fixed in section 12; this section fixes layer 3.

1. **Model-level freezing — already shipped.** `Field(frozen=True)` on `SourceItem.id`,
   `SourceItem.first_fetched_at`, `Change.id`, `Change.detected_at`, `Digest.id`,
   `Digest.digest_date` (`schemas.py`); `tests/unit/test_protected_ordering_fields.py`
   statically forbids `model_copy(update=...)` on any of them. `SourceItem.dedupe_key`,
   `source_id`, and `canonical_url` are **not** Pydantic-frozen — they are identity fields
   rather than ADR 0008 ordering-tuple components, so layers 2 and 3 hold them immutable.
2. **Repository restriction — this PR.** The ingestion write protocol (section 12) exposes
   **no method that updates a protected column.** `find_or_create_source_item` never writes
   metadata for an existing row and sets identity fields exactly once, at insert;
   `advance_latest_snapshot_and_metadata` writes only the allowed-mutable set, and only under
   its newest-wins condition. Neither exposes a setter for any of the five `source_items`
   identity fields, a generic `update(**fields)` passthrough, or `id` reassignment. This is
   application-level defence in depth — a direct SQL statement bypasses it, which is why
   layer 3 exists.
3. **Storage-level enforcement — PL/pgSQL triggers created in the initial Alembic migration,
   with a matching `downgrade()` that drops them.**

**`source_items` — row-level `BEFORE UPDATE`.** One trigger function, `FOR EACH ROW`, raising
`RAISE EXCEPTION 'source_items.% is immutable', <column>` when **any** of the five identity
fields changes:

- `id`
- `first_fetched_at`
- `dedupe_key`
- `source_id`
- `canonical_url`

The check is `OLD.<col> IS DISTINCT FROM NEW.<col>` per column, so an `UPDATE` that leaves all
five byte-identical is permitted (an idempotent rewrite of the mutable metadata), which a
column privilege cannot express. The trigger definition lists the five columns, so adding one
later is a one-line change in a new migration.

**`document_snapshots` — row-level `BEFORE UPDATE`, row-level `BEFORE DELETE`, and
statement-level `BEFORE TRUNCATE`.** The `BEFORE UPDATE` trigger raises on **any** column
change (whole-row immutability — `AGENTS.md`: "Raw source snapshots are immutable"); the
`BEFORE DELETE` and `BEFORE TRUNCATE` triggers always raise ("Corrections create a new
version and retain provenance"). `TRUNCATE` is a separate statement the row-level `DELETE`
trigger never sees, so it needs its own statement-level trigger.

**Scope and limits of trigger-backed immutability — stated accurately:**

- The triggers protect **ordinary database operations while they are enabled** — every
  `UPDATE` / `DELETE` / `TRUNCATE` the application, a migration, or a test issues.
- A **table owner or superuser can disable a trigger** (`ALTER TABLE … DISABLE TRIGGER`) or
  otherwise bypass it. In CI the connecting role **is** the owner/superuser, so the
  integration suite drives the triggers against ordinary DML but could also turn them off —
  the guarantee is "ordinary DML is rejected", not "cannot be bypassed".
- The triggers **do not** protect against administrative DDL such as `DROP TABLE` or
  `ALTER TABLE`.
- **Phase 1 does not claim protection against a malicious database administrator.**
- **Before production**, the API and the worker must connect with a **restricted runtime
  role** that has **no** schema ownership, DDL, `TRUNCATE`, trigger-management, or migration
  privileges — only `SELECT`/`INSERT` and column-scoped `UPDATE` on the mutable columns.
  Migrations run under a **separate owner/migration role**. This role split is a **production
  gate**, not optional hardening (see Consequences; "Deferred operational decisions" narrows
  only its exact host provisioning).

**Why triggers rather than column-level `UPDATE` privileges as the Phase-1 mechanism:**

| | `BEFORE UPDATE` / `DELETE` / `TRUNCATE` triggers | Restricted column `UPDATE` privilege |
|---|---|---|
| Exercised by the CI database role | **Yes** — the triggers fire for ordinary DML issued by the CI owner/superuser, so the integration suite drives them directly. (That same role could also disable them — see the limits above.) | **No** — `GRANT`/`REVOKE` do not restrict a table owner or superuser, so a single-role CI database cannot exercise the restriction without provisioning a separate limited role first. |
| Distinguishes "unchanged" from "changed" | **Yes** — compares `OLD`/`NEW`, so an identical rewrite is a no-op. | No — forbids naming the column in `UPDATE` at all. |
| Covers `DELETE` and `TRUNCATE` | **Yes** — dedicated `BEFORE DELETE` and `BEFORE TRUNCATE` triggers. | No — per-statement-type only, and cannot compare values. |
| Ships and rolls back in one migration | **Yes** — `op.execute(CREATE FUNCTION … CREATE TRIGGER …)`, dropped in `downgrade()`. | Partly — needs the runtime role and its `GRANT`s managed in migrations. |

Both are used: the triggers are the Phase-1 test-enforced mechanism, and the restricted
runtime role (which also removes `UPDATE` on the immutable columns and `DELETE`/`TRUNCATE`
on `document_snapshots`) is the production gate above.

**Rejected — a normal `CHECK` constraint:** cannot compare `OLD` and `NEW`; ADR 0008 §5.D
already rules it out.

**How a rejected write is rolled back, and how tests confirm the value survives:**

1. The trigger's `RAISE EXCEPTION` aborts the statement; PostgreSQL marks the surrounding
   transaction aborted; `psycopg` raises `psycopg.errors.RaiseException`, which SQLAlchemy
   surfaces as `DBAPIError`.
2. The service (or the test) rolls back its transaction. Nothing was committed, so the
   on-disk row is untouched.
3. Integration tests (section 15): one **independent** test per protected `source_items`
   field (`id`, `first_fetched_at`, `dedupe_key`, `source_id`, `canonical_url`) and one each
   for snapshot `UPDATE`, `DELETE`, and `TRUNCATE`. Each test:
   - reads the row and keeps the original value;
   - issues the raw statement directly (e.g.
     `text("UPDATE source_items SET dedupe_key = :v WHERE id = :id")`), bypassing the
     repository on purpose — this exercises layer 3, not layer 2;
   - asserts `DBAPIError`;
   - rolls back and confirms the session is usable again;
   - re-`SELECT`s and asserts the field — and the whole row — is unchanged, timestamps to the
     microsecond;
   - asserts the repository exposes no ordinary method that could have issued that write.

The four `Change` / `Digest` protected-column storage-level tests from ADR 0008 §14 ("Later —
persistence-adapter integration PR") land with the migration that creates the `changes` and
`digests` tables (intelligence persistence — out of scope here), so the gap is deferred, not
skipped.

### 12. Repository protocols and module boundaries

Two narrow, typed protocols. No speculative abstraction — each has exactly one production
implementation and one caller (ADR 0010).

**12.1 Ingestion write protocol — `ingestion/`-private.**

Lives next to its only implementer and only caller, in `src/ai_daily_digest/ingestion/`
(e.g. `ingestion/persistence.py`). Not in `shared/` — no other module calls it.

Responsibilities:

- `find_or_create_source_item(...) -> SourceItem` — create-or-find by canonical `dedupe_key`.
  **On find, it writes nothing** — it never updates the allowed-mutable metadata set
  (`publisher`, `title`, `published_at`, `updated_at`, `authors`, `tags`, `language`,
  `event_id`) for an existing row, only reads it back. **On create**, the new row is inserted
  with its initial metadata — set once, before the item has any snapshot. The five identity
  fields (`id`, `first_fetched_at`, `dedupe_key`, `source_id`, `canonical_url`) are written
  exactly once, at that insert, and never again; the section 11 trigger backstops that. This
  method's contract is deliberately **not** "upsert" — an upsert implies find-and-write, and
  this repository operation must never write on find (see section 13 for why).
- `add_snapshot_if_new(source_item_id, content_hash, ...) -> DocumentSnapshot` — insert only
  when `(source_item_id, content_hash)` is new; return the existing row otherwise. Never a
  second row for identical content.
- `advance_latest_snapshot_and_metadata(source_item_id, snapshot_id, metadata) -> bool` —
  after the snapshot row exists, **conditionally advances `latest_snapshot_id` and the
  allowed-mutable metadata together, under the exact same condition**: only when the
  candidate belongs to the item and its `(fetched_at, id)` tuple is newer than the currently
  referenced snapshot's tuple (same transaction, section 13). An older or equal candidate
  advances **neither** the pointer nor the metadata — there is no path where one moves without
  the other. Return whether the update applied. The method's name and signature exist
  specifically to make that joint condition part of the contract, replacing the earlier
  `advance_latest_snapshot(source_item_id, snapshot_id) -> bool` shape, which advanced the
  pointer conditionally but left metadata writes to a separate, unconditional step — the
  regression this section corrects (see section 13).
- Every method runs inside the **caller-supplied `AsyncSession` transaction** (section 13). A
  method may `flush()`, but **never `commit()` or `rollback()`** — transaction control belongs
  to the ingestion service alone.
- All three are **idempotent under retries and concurrent duplicate ingestion**
  (`INSERT … ON CONFLICT DO NOTHING` + re-`SELECT`; unique-violation-safe).

**12.2 Delivery read protocol — cross-module, in `shared/`.**

`/v1/updates` needs a query capability that ingestion's tables provide. That is a genuine
cross-module seam — delivery depends on a capability ingestion supplies — so the **Protocol**
belongs in `shared/` (e.g. `src/ai_daily_digest/shared/repositories.py`), mirroring the
`shared/snapshot_resolver.py` precedent exactly. The concrete PostgreSQL adapter stays
**ingestion-owned**.

Responsibilities (`SourceItemFeedRepository` or similar):

- one async method, e.g.
  `async def list_source_items(*, publisher: str | None, source_id: str | None,
  after: tuple[datetime, uuid.UUID] | None, limit: int) -> Sequence[SourceItem]`;
- `publisher` / `source_id` are **already-canonical** values (trimmed, NFC-normalized,
  case-sensitive) exactly as `canonicalize_filters` produced them — the protocol takes
  canonical **values**, never raw request strings or the delivery-owned `CanonicalFilters`
  object (which `shared/` must not import);
- `after` is the keyset position `(first_fetched_at, id)`; the query applies the row-value
  predicate `(first_fetched_at, id) < (after_ts, after_id)` (ADR 0008 §4);
- ordering is fixed `first_fetched_at DESC, id DESC`;
- the method fetches **`limit + 1`** rows so the route can detect a further page (ADR 0008
  §10) — the route trims to `limit` and builds `next_cursor` from the last **returned** row;
- it returns the shared `SourceItem` domain model. The route projects that to the
  ADR 0008 §12 `UpdateSummary`, which **omits `dedupe_key`** and other internal fields. The
  repository returns domain records; the public omission happens at the projection boundary,
  under `delivery/api/`.

**Rejected — read Protocol under `delivery/api/`, adapter structurally typed in ingestion:**
technically works with `typing.Protocol` structural subtyping and mypy conformance-checked at
the `create_app` call site, and avoids a `shared/` change. Rejected as the recommendation
because it contradicts the `shared/snapshot_resolver.py` precedent (a real cross-module
contract is stated explicitly in `shared/`, not left implicit). The shared placement is the
Phase-1 decision, not an implementation-time choice.

**12.3 Shared database kernel — `src/ai_daily_digest/shared/db/`.**

One PostgreSQL database backs ingestion, intelligence, and — through the read Protocol —
delivery. The engine, connection pool, session factory, and the SQLAlchemy `MetaData` are
therefore **not** ingestion-private; they are a shared kernel. Placing them in `ingestion/db/`
would force the later intelligence-persistence ADR to either import `ingestion.db` internals
(a module-boundary violation) or stand up a **second competing engine and pool** against the
same database (no single source of truth). Both are rejected.

Proposed files, created in the implementation PR:

- `shared/db/__init__.py`;
- `shared/db/metadata.py` — the one shared SQLAlchemy `DeclarativeBase` (or bare `MetaData`)
  and the deterministic constraint/index **naming convention**. **Every** module-owned ORM
  model in the application registers against this single `MetaData` object;
- `shared/db/engine.py` — construction of the async engine (`create_async_engine`) and the
  `async_sessionmaker`. A process has **one** configured engine, **one** connection pool, and
  **one** session-factory boundary.

`DatabaseConfig` stays in `shared/config.py` (section 14); `shared/db/engine.py` takes it as
an argument. **Importing or constructing anything in `shared/db/` performs no I/O and opens
no connection** — the engine is created by the composition root (section 12.6); a bare import
in a test or a tool never touches PostgreSQL.

**12.4 Module-owned database code.**

Domain-specific persistence stays inside the owning module:

- `ingestion/db/models.py` — `SourceItemRow`, `DocumentSnapshotRow` (this PR);
- `ingestion/db/repository.py` — `PostgresSourceItemRepository` (this PR);
- `intelligence/db/models.py` — future `fact`/`change`/`digest` rows (a later
  intelligence-persistence ADR + PR, **not this one**);
- `intelligence/db/repository.py` — future (same later PR).

Rules:

- Ingestion owns the source-item and snapshot models and repositories. Intelligence will own
  the fact/change/digest models and repositories under its own ADR.
- Every module's ORM models register against `shared.db.metadata` — never a module-local
  `MetaData`.
- `delivery/` route modules **must not** import either module's private database
  implementation (`ingestion.db`, `intelligence.db`). Delivery depends only on `shared/` — the
  read Protocol (section 12.2) and `SourceItem`.
- A repository receives an **injected `AsyncSession`** (or the shared session-factory
  boundary); it never creates its own engine or pool.
- No module creates a competing engine, pool, or session factory — exactly one of each per
  process.

**12.5 Alembic ownership — shared infrastructure.**

`alembic/`, `alembic.ini`, and the migration history are **shared infrastructure**, not
ingestion-owned:

- `alembic/env.py` sets `target_metadata = shared.db.metadata` and imports **every** approved
  module's ORM model modules so they are all registered against that single `MetaData` before
  autogenerate or `upgrade` runs.
- The **first** migration (`0001_source_items_and_document_snapshots.py`) is **authored by
  Person A** because it creates ingestion-owned tables and their triggers.
- **Future migrations** may be authored by the relevant module owner (intelligence's
  fact/change/digest tables under its own ADR).
- A migration that touches more than one module, or any shared contract, requires peer review
  from another steward (`AGENTS.md`: "Before editing … database migrations … request review
  from another review steward").
- There is **exactly one `alembic_version` history** for the application database — one
  migration timeline, not one per module. The initial revision still neither creates nor drops
  that table (section 7).

**12.6 Composition root.**

- The **process composition root** — the API's ASGI entrypoint, or the worker's
  `async def main()` — constructs the shared engine and `async_sessionmaker` **once**, from
  `DatabaseConfig`.
- It injects sessions (or the session-factory boundary) and the concrete repositories into
  ingestion, intelligence, and delivery wiring. For delivery this is the existing
  `create_app()` DI-parameter pattern (the same shape as `readiness_probes`); the read
  repository is passed in typed as the `shared/` Protocol.
- **HTTP route modules never construct an engine** and never import a private ORM adapter.
  Infrastructure reaches a handler only through a typed FastAPI dependency (ADR 0010).
- The **worker** and **API** are separate processes and may each hold their **own
  process-local pool**. Within a single process, modules **must not** create separate
  competing pools — they share the one the composition root built.
- Unit tests pass an in-memory fake of the read Protocol (ADR 0010: "tests use an in-memory
  fake"; "no Postgres adapter exists merely to raise `NotImplementedError`").

These edits all require coordination, from two different sources:

- `delivery/api/app.py` is explicitly on ADR 0010's "Parallel-work boundaries" enumerated
  list (which names `docs/API_CONTRACT.md`, `shared/schemas.py`, `delivery/api/app.py`,
  `pyproject.toml`, and `uv.lock`).
- `shared/db/`, `shared/config.py`, and `shared/repositories.py` are **new** files ADR 0010
  did not — and could not — name. They are peer-reviewed shared infrastructure under
  `AGENTS.md`'s rule ("Before editing shared contracts, database migrations, or public API
  schemas … request review from another review steward") and `docs/ARCHITECTURE.md`'s rule
  that `shared/` structures "require one teammate review because they may block all three
  people".

The implementation PR therefore rebases on current `main`, reruns `make ci`, and takes peer
review on every `shared/` addition and on `delivery/api/app.py`.

### 13. Transactions and concurrency

**Transaction ownership.**

- The ingestion application/service owns exactly **one `AsyncSession` transaction per ingested
  item** (inside a collection run).
- The PostgreSQL repository is **bound to that session** — the service passes the session in;
  the repository never opens its own.
- Repository methods may **run queries and `flush()`**; they **must never independently
  `commit()` or `rollback()`**.
- The service **commits once**, only after the source-item find-or-create, the snapshot
  insertion, and the combined pointer-and-metadata update have **all** succeeded.
- Any exception rolls back the **service-owned** transaction (see "Rollback behaviour" below).

**Per-item write sequence** (all inside that one transaction):

1. **Find or create the source item, writing no metadata on find:**
   `INSERT INTO source_items (id, dedupe_key, source_id, publisher, title, canonical_url,
   first_fetched_at, published_at, updated_at, authors, tags, language, event_id) VALUES (…)
   ON CONFLICT (dedupe_key) DO NOTHING RETURNING *`; if no row is returned (the item already
   existed), `SELECT * FROM source_items WHERE dedupe_key = :key`. **Neither branch writes the
   allowed-mutable metadata columns for an existing row.** A brand-new item's initial metadata
   is set exactly once, at this insert, before it has any snapshot — matching the requirement
   that "a new item may be inserted with its initial metadata before its first snapshot is
   attached." The five identity fields are never named on any later write; the trigger
   backstops that.
2. If the content hash is new: `INSERT INTO document_snapshots (…) ON CONFLICT
   (source_item_id, content_hash) DO NOTHING RETURNING id`; if no row is returned, `SELECT
   id` for the existing `(source_item_id, content_hash)`.
3. **If, and only if, a new snapshot was inserted**, run **one** conditional `UPDATE
   source_items SET latest_snapshot_id = :snap_id, publisher = :publisher, title = :title,
   published_at = :published_at, updated_at = :updated_at, authors = :authors, tags = :tags,
   language = :language, event_id = :event_id WHERE id = :item_id AND (the new snapshot's
   (fetched_at, id) is newer than the currently referenced snapshot's, or latest_snapshot_id
   IS NULL)` in the **same** transaction. **The pointer and the eight allowed-mutable metadata
   columns advance together, gated by the exact same condition — there is no path where one
   updates and the other does not.** A conditional subquery/join enforces the `(fetched_at,
   id)` comparison in PostgreSQL. This is the corrected Phase-1 decision: the earlier design
   advanced `latest_snapshot_id` conditionally (step 3) but wrote metadata unconditionally on
   every upsert (old step 1), so an **older** concurrent fetch's metadata write could commit
   after, and silently overwrite, a **newer** fetch's metadata — even though the older fetch's
   own pointer update correctly lost the comparison. Combining pointer and metadata into one
   conditionally-gated statement removes that window entirely: an older or duplicate candidate
   updates **neither**. The composite FK from section 9 proves the selected snapshot belongs to
   this item.
4. The service issues its **single commit**. The source-item row (found or inserted), the
   snapshot row, and the combined pointer-and-metadata update become visible **atomically**.

**Phase-1 limitation — stated explicitly, not hidden.** A duplicate fetch that produces **no**
new snapshot (step 2 finds the content hash already recorded) never reaches step 3, and
therefore does **not** independently refresh source-item metadata: a re-fetch of unchanged
content never updates `publisher`/`title`/`authors`/etc. even if the source's metadata (not its
content) changed since the last fetch — for example a publisher rename observed only through a
byte-identical re-fetch. This is a deliberate MVP trade-off, not an oversight: metadata refresh
is tied to "a new snapshot arrived," never to "a fetch happened, content or not." **Deferred
decision:** whether a metadata-only change (no content-hash change) should trigger its own,
separately-conditioned metadata-refresh path is left to a future ADR/PR once a real need is
observed; Phase 1 does not build that path speculatively.

**Uniqueness-conflict handling:** the two `UNIQUE` constraints (`source_items.dedupe_key`,
`document_snapshots (source_item_id, content_hash)`) are the single source of truth.
Concurrent runs race on the `INSERT`; `ON CONFLICT DO NOTHING` makes the loser converge on the
winner's row via the `RETURNING`/re-`SELECT` — step 1 never uses `DO UPDATE`, so there is no
conflict-time metadata write to race on at all. No duplicate row; no error surfaced to the run.

**Retry / idempotency:** re-running the same item recomputes the same `dedupe_key` and
`content_hash`, hits the same conflict paths, and converges to the same rows.
`advance_latest_snapshot_and_metadata` is conditional and idempotent: the same snapshot is a
no-op, an older snapshot can move **neither** the pointer **nor** the metadata backwards, and
only a newer same-item snapshot advances either — because they are the same `UPDATE`, "either"
always means "both."

**No partial state — pointer, metadata, or otherwise:** steps 2 and 3 are in one transaction
and land on the service's single commit; step 3's pointer and metadata columns are written by
**one** `UPDATE` statement, so a losing concurrent transaction cannot advance one and not the
other, and a crash before commit rolls both back together. At every commit boundary,
`latest_snapshot_id` is either `NULL` or points at a real, existing snapshot of that item, and
the mutable metadata always corresponds to whichever snapshot `latest_snapshot_id` currently
names — never to an older, already-superseded fetch.

**Rollback behaviour:** any exception in the per-item transaction (an unexpected unique
violation, a trigger `RAISE`, a lost connection) makes the **service** roll back its one
transaction:

- **for a new item**, the failed attempt persists **no rows** — no `source_items` row and no
  `document_snapshots` row;
- **for an existing item**, rollback **preserves the rows committed by earlier runs** and
  discards **only** the changes this attempt made in this transaction (a new snapshot insert,
  and the combined pointer-and-metadata update if the candidate was newer);
- the collection run records the item as failed and continues (`docs/ARCHITECTURE.md`: "One
  source failure does not abort others").

**Isolation level:** default `READ COMMITTED` is sufficient for Phase 1 — `ON CONFLICT`
idempotency and keyset pagination do not need `REPEATABLE READ` or `SERIALIZABLE`. Stated
explicitly so a later change is a reviewed decision.

**No database transaction is held across HTTP pagination requests.** Each `GET /v1/updates`
request opens a short-lived session (a FastAPI dependency that yields one session per
request), runs one `SELECT … ORDER BY first_fetched_at DESC, id DESC LIMIT :limit + 1`, and
closes it. Continuity between pages is the signed keyset cursor (ADR 0008), never a
server-side transaction, cursor, or scroll. There is no `as_of` snapshot in Phase 1
(ADR 0008 §11).

### 14. Configuration and readiness

- **`DATABASE_URL` stays an environment setting with no committed secret.** Already present
  as an empty placeholder in `.env.example`. The canonical application form is
  `postgresql+psycopg://USER:PASSWORD@HOST:5432/DBNAME`. Managed-provider connection strings
  using the driver-neutral `postgres://` or `postgresql://` scheme are normalized to that
  psycopg-explicit form at the typed configuration boundary; all other schemes fail closed.
  Alembic reads the same normalized value. The configuration layer confirms the value is set
  when a database-backed feature is configured and never logs it or any component of it.
- **A configured database-backed API must install a real readiness probe.** A
  `DatabaseReadinessProbe` implementing the existing `ReadinessProbe` Protocol
  (`dependencies.py:20-24`) whose `is_ready()` runs a **bounded `SELECT 1`** on a pooled
  connection with a short per-statement timeout, returns `True`/`False`, and catches its own
  driver errors specifically (`SQLAlchemyError` / `psycopg.Error`) → `False` on failure.
- When `DATABASE_URL` is configured and any route needs the database, `"database"` goes into
  `required_dependencies`, and `build_readiness_registry()` **already raises** if that name
  has no probe (`dependencies.py:90-93`) — exactly ADR 0010's "Naming a dependency as
  required without providing its probe … must prevent normal startup." A foundation-only app
  with no database keeps `required_dependencies` empty and `/v1/health/ready` returns `200`.
- **Probe errors are sanitized through the merged PR #44 readiness boundary.**
  `ReadinessRegistry.evaluate()` catches every probe exception, logs only `exception_type`
  and `request_id`, and yields `ready=False` (`dependencies.py:42-73`); the public response
  carries only `{name, status}` (`routes/health.py`). The probe must never let a DSN, host,
  driver message, or connection string escape — it returns a bool; the registry boundary is
  the backstop. `GET /v1/health/ready` returns `503` + `service_unavailable` when the
  database is not ready.
- **Pool and timeout settings are bounded Phase-1 defaults:** `pool_size=5`,
  `max_overflow=5`, `pool_pre_ping=True`, `pool_recycle=1800` seconds, driver
  `connect_timeout=5` seconds, server-side `statement_timeout=10_000` milliseconds, and a
  2-second readiness-probe timeout. `DatabaseConfig` exposes typed overrides so deployment
  can tune them without code edits; values and connection URLs are never logged.

### 15. Testing and CI

**PostgreSQL integration tests run against a real PostgreSQL — never a SQLite stand-in.**
Arrays (`text[]`), the native `uuid` type, `timestamptz` microsecond precision, `ON
CONFLICT`, row-level triggers, and FK `RESTRICT` are not faithfully reproduced by SQLite.

- **CI:** the `tests` job in `.github/workflows/ci.yml` gains a `postgres:17` **service
  container** (with a health check) and sets `DATABASE_URL` to it. The integration harness
  (below) creates the run's temporary database and runs `alembic upgrade head` against it; the
  integration suite (`-m integration`, currently already inside `-m "not live"`) then runs as
  part of the same job. **CI always runs the real integration suite; it is never skipped
  there,** and the job **fails** if the database cannot be provisioned or torn down. The
  `quality` and `security` jobs are unchanged.
- **Postgres version:** pin the image tag (`postgres:17`) as the tested version; 18 is not
  required and its `uuidv7()` is irrelevant (generation is application-side).
- **Local:** the implementation PR adds a `compose.yaml` with one `postgres` service and
  `Makefile` targets (`db-up`, `db-migrate`, `db-revision`, `test-integration`), and a short
  "running migrations and integration tests locally" doc. A developer may also point
  `DATABASE_URL` at any local PostgreSQL.
- **When local PostgreSQL is absent, behaviour is explicit, never silent:** integration tests
  carry `@pytest.mark.integration`; they **skip with a clear reason**
  (`pytest.skip("set DATABASE_URL to run PostgreSQL integration tests")`) when `DATABASE_URL`
  is unset, and a collection-time guard **fails** (not skips) if `-m integration` was
  explicitly selected with no `DATABASE_URL`. `make check` (inner loop) already excludes
  `integration` (`Makefile:22`). No test ever silently passes without touching the database,
  and there is no SQLite fallback path.
- **Unit tests** for delivery and ingestion logic use the in-memory fake repositories — no
  database at all.

**Database-test isolation.** A single per-test rollback fixture does **not** isolate every
integration test — the mandatory duplicate-ingestion and latest-pointer tests need genuinely
concurrent transactions on separate connections, and those connections must commit. The
strategy:

- the integration run **creates a temporary PostgreSQL database** (name unique to the run);
- Alembic runs `upgrade head` against it **once**, before any test;
- **ordinary, non-committing tests** run inside a per-test transaction that is rolled back at
  teardown — fast, with no cross-test residue;
- **concurrency tests** (duplicate ingestion, latest-snapshot ordering) open their **own
  connections** and perform **real commits**; a single outer rollback cannot unwind another
  connection's committed work, so these tests do not rely on one;
- every concurrency test uses **freshly generated UUID v7** records and asserts only on the
  rows it created — no test assumes the tables are globally empty;
- teardown **disposes all connections**, runs `alembic downgrade base` where practical to
  exercise the down-path, then **drops the temporary database**;
- **CI fails the job** if the PostgreSQL service, the `upgrade`, or the teardown/drop cannot
  complete — a half-provisioned database is never reported as "tests skipped".

This strategy supports the required genuine concurrent tests for duplicate ingestion and
latest-snapshot ordering.

**Integration tests the implementation PR must include:**

- **unique constraints** — `dedupe_key`; `(source_item_id, content_hash)`;
- **idempotency** — re-running an item insert produces the same rows and raises nothing;
  concurrent duplicate insert (separate connections, real commits) converges to one row;
- **transaction ownership** — the repository methods never commit; nothing persists until the
  service commits; a **new** item whose attempt fails leaves **no rows**; an **existing** item
  whose attempt fails keeps its previously committed rows and loses **only** this attempt's
  changes;
- **migration lifecycle** — `upgrade`→`downgrade`→`upgrade`: after `downgrade` the two tables,
  the constraints, the row-level and statement-level triggers, the trigger functions and the
  keyset index are gone; after the second `upgrade` they are back identically; the revision
  never drops `alembic_version` and Alembic keeps managing it;
- **foreign keys** — a snapshot for a missing `source_item_id` is rejected; a `source_item`
  with snapshots cannot be deleted (`RESTRICT`); a source item cannot point at another
  item's snapshot;
- **timestamp precision** — `first_fetched_at` and `fetched_at` round-trip through
  `timestamptz` to the microsecond;
- **UUID v7 storage** — a `new_id()` value stored in a native `uuid` column reads back equal
  and canonical;
- **snapshot contract and immutability** — naive `fetched_at` and null `content_text` are
  rejected at the model/storage boundaries; ordinary `UPDATE`, `DELETE`, **and `TRUNCATE`** on
  `document_snapshots` are each rejected by their trigger, the transaction rolls back, and the
  pre-existing rows are unchanged on re-read;
- **latest-pointer-and-metadata concurrency** (the Finding 1 regression fix) — four required
  cases:
  1. two different snapshots, carrying different mutable metadata, committed **out of arrival
     order** (on separate connections) leave **both** `latest_snapshot_id` **and** the mutable
     source-item metadata (`publisher`, `title`, `published_at`, `updated_at`, `authors`,
     `tags`, `language`, `event_id`) corresponding to the **same** greatest `(fetched_at, id)`
     candidate — never one advanced and the other stale;
  2. an **older retry** (a candidate whose `(fetched_at, id)` loses the comparison) regresses
     **neither** the pointer **nor** the metadata — both remain exactly as the newer candidate
     left them;
  3. a **losing concurrent transaction cannot partially update metadata** — because the pointer
     and metadata columns are written by one conditional `UPDATE`, a transaction whose
     candidate loses the `(fetched_at, id)` comparison writes **zero** of the eight mutable
     columns, not some;
  4. a duplicate-content fetch that carries **different** metadata than what is stored (no new
     `content_hash`, so no new snapshot) leaves the stored metadata **unchanged** — this is the
     documented Phase-1 limitation above, proven by a test rather than left implicit;
- **list normalization** (new Phase-1 behaviour, section 10) — author/tag trimming, Unicode
  NFC normalization, empty removal, stable deduplication, and tag case-folding are
  deterministic;
- **source-identity immutability** — one **independent** test for each of the five protected
  `source_items` fields (`id`, `first_fetched_at`, `dedupe_key`, `source_id`, `canonical_url`):
  a direct SQL `UPDATE` of that one field raises `DBAPIError`, the transaction rolls back, and
  a re-`SELECT` shows the original value byte-for-byte (timestamps to the microsecond). The
  `Change`/`Digest` protected-column tests land with those tables (ADR 0008 §14).

### 16. The persistence-foundation implementation PR

One focused PR, opened only **after this ADR is Accepted by Persons A, B, and C.** It
contains exactly:

- **`pyproject.toml`** — add production dependencies **with written per-dependency
  justification** (`AGENTS.md` rule): `sqlalchemy>=2.0`, `alembic>=1.13`,
  `psycopg[binary]>=3.2`. Justification: no standard-library or existing dependency provides
  a typed ORM, reviewable migrations, or a PostgreSQL wire driver.
- **`uv.lock`** — regenerated; prove `uv lock --check` and a locked non-editable install
  (`uv sync --locked --no-editable`), and run `pip-audit` after resolution (same bar
  ADR 0007 / ADR 0010 set).
- **`alembic.ini` + `alembic/`** — **shared infrastructure** (top-level, `script_location`
  configured). `env.py` (async, via `connection.run_sync`) sets
  `target_metadata = shared.db.metadata` and imports every approved module's ORM model modules
  so all tables are registered against that one `MetaData`. One initial migration
  `alembic/versions/0001_source_items_and_document_snapshots.py` — **authored by Person A**
  because it creates ingestion-owned tables — hand-written, not autogenerated (autogenerate
  does not reliably emit `text[]` columns or trigger DDL). Exactly one `alembic_version`
  history for the database (section 12.5).
- **`src/ai_daily_digest/shared/db/`** — `__init__.py`; `metadata.py` (the one shared
  `DeclarativeBase`/`MetaData` plus deterministic naming convention that every module's ORM
  models register against); `engine.py` (`create_async_engine` + `async_sessionmaker` from a
  `DatabaseConfig` — one engine, one pool, one session-factory boundary per process). No
  import connects to the database (section 12.3).
- **`src/ai_daily_digest/ingestion/db/`** — `models.py` (`SourceItemRow`,
  `DocumentSnapshotRow`, SQLAlchemy 2.0 mapped classes registered against
  `shared.db.metadata`, mirroring the Pydantic contract), `repository.py`
  (`PostgresSourceItemRepository` implementing the ingestion write protocol and the shared
  read protocol, taking an injected `AsyncSession` — never its own engine).
- **`src/ai_daily_digest/ingestion/persistence.py`** — the ingestion write `Protocol`.
- **`src/ai_daily_digest/shared/repositories.py`** — the cross-module read `Protocol`
  returning `SourceItem` (peer-reviewed `shared/` change).
- **`src/ai_daily_digest/shared/config.py`** — `DatabaseConfig` (`from_env()` reads and
  validates `DATABASE_URL` plus the bounded pool/timeout overrides in section 14; no secret
  or URL component is logged).
- **`src/ai_daily_digest/shared/schemas.py`** — tighten `DocumentSnapshot` exactly as
  section 9 specifies: aware UTC `fetched_at`, required `content_text`, frozen model.
- **`src/ai_daily_digest/delivery/api/` readiness** — `DatabaseReadinessProbe` (implements
  `ReadinessProbe`, runs bounded `SELECT 1`).
- **`src/ai_daily_digest/delivery/api/app.py`** — `create_app()` gains the optional injected
  read-repository parameter and wires `"database"` into readiness when configured.
- **`tests/integration/`** — `conftest.py` (temporary-database lifecycle per section 15:
  create → `alembic upgrade head` → run tests → dispose connections → `alembic downgrade base`
  where practical → drop the database; a per-test transaction-rollback fixture for
  non-committing tests; a separate-connection helper for concurrency tests),
  `test_source_item_repository.py` (find-vs-create metadata behaviour; the four
  latest-pointer-and-metadata concurrency cases from section 15), `test_migrations.py`
  (`upgrade`→`downgrade`→`upgrade`; the application objects vanish then reappear identically
  while Alembic keeps its own `alembic_version` table), `test_immutability_triggers.py` (all
  five `source_items` identity fields; snapshot `UPDATE` / `DELETE` / `TRUNCATE`),
  `test_ingestion_transaction.py` (service-owned commit/rollback for new and existing items;
  the combined pointer-and-metadata `UPDATE` never applies partially).
- **`tests/unit/ingestion/`** — the in-memory fake repository and its tests.
- **`.github/workflows/ci.yml`** — `postgres:17` service on the `tests` job, `DATABASE_URL`
  pointed at it, and the job configured to fail if the integration harness cannot provision or
  drop its temporary database (the `upgrade`/`downgrade` themselves run inside the harness —
  section 15).
- **`compose.yaml`** — local PostgreSQL.
- **`Makefile`** — `db-up`, `db-migrate`, `db-revision`, `test-integration`.
- **A short local-database doc** — running migrations and integration tests.
- **`.env.example`** — unchanged (`DATABASE_URL` already present); no new variable in
  Phase 1.

**Explicitly excluded from that PR:**

- FastAPI domain routes; `GET /v1/updates`; cursor route integration;
- collectors and any external network call;
- pgvector, embeddings, or any vector table;
- intelligence persistence — `intelligence/db/` models/repositories and any
  `facts`/`changes`/`digests` tables (a later intelligence-persistence ADR + PR);
- subscriptions and email tables;
- production deployment or cloud provisioning.

### 17. Dependency recommendation (no installation in this PR)

| Package | Role | Why not stdlib / an existing dependency | Recommended constraint |
|---|---|---|---|
| `sqlalchemy` | Typed ORM + query layer | No stdlib ORM; `docs/ARCHITECTURE.md` baseline | `>=2.0` |
| `alembic` | Reviewable schema migrations | No stdlib migration tool; `AGENTS.md` requires migrations | `>=1.13` |
| `psycopg[binary]` | PostgreSQL driver (sync + async) | No stdlib PostgreSQL driver; section 5 | `>=3.2` |

`greenlet` arrives transitively with SQLAlchemy's async support — expected, not a separate
direct dependency. No dependency is added by this ADR; the table is the pre-agreed set for
the implementation PR.

## Consequences

- One database serves structured queries now and semantic retrieval later; provenance
  survives a vector-index rebuild (unchanged from the original decision).
- `GET /v1/updates` (ADR 0008 PR 4) becomes implementable: a real configured repository
  adapter exists, satisfying ADR 0010's "no public route merges without a functional
  configured data source."
- The team manages schema migrations and (still-deferred) backups.
- Async SQLAlchemy keeps the FastAPI event loop unblocked and matches the async collector
  path, at the cost of an async `env.py` for Alembic and the `greenlet` transitive
  dependency.
- Psycopg 3 is one driver for both the async runtime and synchronous Alembic.
- `text[]` for `authors`/`tags` keeps Phase 1 simple; a future tag dimension is an additive
  migration that never touches an ordering column.
- Immutability triggers make ADR 0008 §5.D layer 3 real and CI-testable against the ordinary
  DML the application issues. They do **not** constrain a table owner, a superuser, or
  `DROP`/`ALTER`/`TRUNCATE` by a privileged role; a **restricted runtime database role** (no
  schema ownership, DDL, `TRUNCATE`, trigger management, or migration rights), with migrations
  run under a separate owner role, is a **production gate**, not optional hardening.
- The cross-module read Protocol in `shared/` follows the `snapshot_resolver` precedent and
  keeps `delivery/api/` free of any `ingestion` import.
- The engine, session factory, and `MetaData` live in `shared/db/`, so the later
  intelligence-persistence ADR reuses the same connection pool and metadata instead of
  importing `ingestion.db` internals or standing up a second competing engine. Concrete models
  and repositories stay module-owned, and Alembic keeps one migration timeline for the whole
  database.
- Integration tests need a real PostgreSQL in CI and (optionally) locally; the inner
  `make check` loop stays database-free and fast.
- A separate vector service remains an option if measured scale or features require it
  (unchanged).

## Risks, rollback, and migration considerations

- **No data migration** — the schema is greenfield; ADR 0007 already established "No
  persistent production data exists yet." The initial revision's `downgrade()` drops **only
  the application objects it created** — the constraints, the row-level and statement-level
  triggers, the trigger functions, the keyset index, then `document_snapshots`, then
  `source_items`. It does **not** touch `alembic_version`, which Alembic creates and manages
  itself. Once real rows exist, a table drop is destructive and requires the (still-deferred)
  backup procedure.
- **Autogenerate blind spots** — Alembic autogenerate does not reliably emit `text[]` columns
  or trigger DDL. The initial migration is hand-written; `test_migrations.py` runs
  `upgrade`→`downgrade`→`upgrade` on a real database and asserts the application objects
  disappear on `downgrade` and are recreated identically on the second `upgrade`, while
  Alembic keeps managing its own version table.
- **Trigger maintenance** — every future protected column must be added to its table's
  trigger in the same migration that adds the column. Recorded as a checklist item for
  reviewers.
- **Circular FK** — handled by DDL ordering (section 9) and runtime write ordering
  (section 13); no `DEFERRABLE` constraint.
- **Driver packaging** — `psycopg[binary]` wheels suit CI and local development; a production
  image may want `psycopg[c]` or system `libpq`. Flagged for the deployment ADR.
- **Async learning curve** — `AsyncSession`, `async_sessionmaker`, and `run_sync` in
  `env.py` are well-documented SQLAlchemy 2.0 patterns; the risk is familiarity, not
  stability.
- **Coordination-controlled files** — `delivery/api/app.py` is on ADR 0010's "Parallel-work
  boundaries" enumerated list; the new `shared/db/`, `shared/config.py`, and
  `shared/repositories.py` are peer-reviewed shared infrastructure under `AGENTS.md`
  (shared-contract / database-migration review) and `docs/ARCHITECTURE.md` (`shared/`
  structures need a teammate review) — not files ADR 0010 itself enumerates. The
  implementation PR rebases on current `main`, reruns `make ci`, and takes peer review on
  each.
- **Shared-kernel registration discipline** — Alembic's `env.py` must import every module's
  ORM model modules; a module that adds tables without registering them against
  `shared.db.metadata` breaks autogenerate and risks a divergent schema. Recorded as a
  reviewer checklist item alongside "trigger maintenance" above.

## Team acceptance checklist

The implementation choices are no longer left for the coding PR to invent. Reviewers
confirmed the items below, requesting specific changes along the way (see "Acceptance
record" above and PR #48/PR #59), before this ADR reached Accepted status:

1. **Person A / ingestion:** confirm source-item identity and mutable columns, list
   normalization, per-item transaction behaviour, immutable snapshot semantics, and authoring
   the first (ingestion-table) Alembic migration.
2. **Person B / intelligence:** confirm stored snapshot content is mandatory, provenance
   cannot be deleted or overwritten, the future database-backed snapshot resolver is a
   separate implementation concern, and — resolving the §12.3 review comment — that the
   `shared/db/` engine/session-factory/`MetaData` kernel lets the later intelligence-
   persistence ADR reuse one connection pool and one metadata without importing `ingestion.db`
   or building a second engine.
3. **Person C / delivery:** confirm async repository access, the shared read Protocol, the
   composition root injecting sessions into `create_app()` (routes construct no engine),
   readiness wiring, and the exact ordering index required by `GET /v1/updates`.
4. **All reviewers:** confirm SQLAlchemy 2 + Alembic + Psycopg 3, PostgreSQL 17 in the
   existing CI test job, the `shared/db/` kernel (one engine / pool / session factory /
   `MetaData` per process) with shared Alembic ownership and a single `alembic_version`
   history, the PL/pgSQL immutability triggers and their stated limits, the
   restricted-runtime-role production gate, the temporary-database integration-test isolation
   strategy, and the Phase-1 pool/timeouts.
5. **ADR placement:** these details remain an amendment to ADR 0002 rather than creating
   ADR 0011. No reviewer requested a split before acceptance.

Acceptance approves the decisions, boundaries, implementation scope, and mandatory tests
above. It does not approve pgvector, collectors, API routes, cloud deployment, or any item
listed as deferred below.

## Deferred operational decisions

These do not block the first persistence PR, but must be resolved before deployment
(original list, extended):

- backup frequency, retention period, restore owner, and a tested restore procedure;
- retention/deletion rules for raw content and subscriber personal data;
- ordinary PostgreSQL indexes for source freshness, `publisher` / `source_id` filtering,
  snapshot history, publication status, and email idempotency — chosen from measured query
  behaviour (the `/v1/updates` keyset index in section 8 is the one exception, promoted into
  the foundation migration because the endpoint cannot page without it);
- the pgvector distance metric and index type, chosen from measured corpus size / query
  behaviour;
- migration and rebuild procedures that prove the derived vector index can be deleted and
  restored without losing source history;
- the **exact host provisioning** of the production PostgreSQL roles — the restricted runtime
  role and the separate owner/migration role are a required **production gate** (section 11,
  Consequences), not deferred; what stays deployment-specific is only how each role is created
  and granted on the chosen host, and whether column-level `UPDATE`/`DELETE`/`TRUNCATE`
  privileges are additionally revoked from the runtime role as defence in depth on top of the
  triggers;
- connection-pool sizing, `statement_timeout`, and connect-timeout values, tuned to the
  deployment;
- the production `psycopg` packaging choice (`[binary]` vs `[c]` vs system `libpq`);
- **independent metadata refresh for a duplicate-content fetch** (section 13) — Phase 1
  deliberately does not refresh `publisher`/`title`/etc. when a re-fetch produces no new
  snapshot; whether that gap is worth a separately-conditioned metadata-only refresh path is
  left for a future ADR/PR once a real need is observed, not built speculatively here.

## References

- [ADR 0001](0001-modular-monolith.md) — module boundaries; ingestion / delivery / shared.
- [ADR 0003](0003-quality-gates.md) — the local and CI check loops these tests join.
- [ADR 0007](0007-uuid-v7-identifier-strategy.md) — application-side UUID v7, native `uuid`
  columns, the dependency-validation bar reused in section 16.
- [ADR 0008](0008-cursor-pagination-contract.md) — the `(first_fetched_at, id)` ordering
  tuple, the `publisher` / `source_id` filters, `limit + 1` reads, and §5.D's three
  immutability layers.
- [ADR 0010](0010-fastapi-openapi-contract-authority.md) — how infrastructure enters routes,
  the readiness-probe requirement, the "no `NotImplementedError` adapter" rule, and its
  "Parallel-work boundaries" enumeration (`docs/API_CONTRACT.md`, `shared/schemas.py`,
  `delivery/api/app.py`, `pyproject.toml`, `uv.lock`) — which predates and does not name the
  new `shared/db/` files.
- `AGENTS.md` — the shared-contract / database-migration / public-API review rule (line 19)
  that governs the new `shared/db/`, `shared/config.py`, and `shared/repositories.py` files.
- `docs/ARCHITECTURE.md` — technology baseline (SQLAlchemy 2 + Alembic, PostgreSQL,
  pgvector), storage rules, collection flow, and the rule that `shared/` structures require a
  teammate review.
- `docs/API_CONTRACT.md` — the `SourceItem` and `DocumentSnapshot` wire contracts this schema
  mirrors.
- `src/ai_daily_digest/shared/snapshot_resolver.py` — the `shared/` cross-module Protocol
  precedent, and its explicit deferral of the sync-vs-async database decision.
