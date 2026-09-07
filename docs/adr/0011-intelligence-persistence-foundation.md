# 0011 — Intelligence persistence foundation (facts, changes, digests)

Status: Accepted by Person A (Patric, 2026-09-05), Person B (author), and Person C (Chamath, 2026-09-07). Person C's deferred review was completed through issue #68 after verifying the corrected equal-timestamp ordering and merged persistence implementation in PRs #59 and #67.
Date: 2026-09-04

> **Architecture alignment with ADR 0002 (amendment merged in PR #48; Status: Accepted by Persons A, B, and C via PR #65):**
> This ADR aligns with the Phase-1 database kernel architecture specified in [ADR 0002](0002-postgres-pgvector.md) §12.3: one shared async engine (`create_async_engine`), one `async_sessionmaker`, and one shared `MetaData`/naming convention (`shared.db.metadata.metadata`) residing in `src/ai_daily_digest/shared/db/`. All intelligence ORM models register against that single shared `MetaData`, and intelligence repositories receive an injected `AsyncSession`. The ADR 0002 documentation amendment merged into `main` via PR #48, and the team recorded formal acceptance via PR #65 (`Status: Accepted by Persons A, B, and C`); the persistence-implementation blocker this section previously described is resolved.
>
> **Governance alignment across modules:** Person A (ingestion review steward) authored ADR 0002 because the first persistence tables (`source_items`, `document_snapshots`) are ingestion-owned; Person B (intelligence) authored this ADR and the intelligence models/store built on that shared kernel; Person C (delivery) owns the API-layer integration (`/v1/changes`, `/v1/digests`) that consumes both. All three are review stewards of ADR 0002's shared kernel per its own §12.3, matching the module-ownership boundary this ADR's §9 table already establishes for intelligence-owned code.

---

## 1. Context & Rationale

The intelligence module currently holds its operational state in-memory:
- `FactStore` tracks per-field current observations and history in `intelligence/facts.py`.
- `Change` and `ChangeSet` aggregates are computed during pipeline execution (`intelligence/change_sets.py`, `intelligence/daily_run.py`).
- `Digest` and `DigestClaim` records are generated, validated, and evaluated in `intelligence/assemble_digest.py` and `intelligence/validate.py`.

To support end-to-end trace journeys, historical auditability, and the `/v1/changes` and `/v1/digests` endpoints defined in `docs/API_CONTRACT.md`, intelligence requires a concrete PostgreSQL persistence foundation.

This design mirrors the architectural rigor and discipline of the persistence foundation established in [ADR 0002](0002-postgres-pgvector.md) (Status: Accepted by Persons A, B, and C):
- **Business chronology ordering tuples:** [ADR 0007](0007-uuid-v7-identifier-strategy.md) establishes that business chronology derives exclusively from an explicit business timestamp, with UUIDv7 serving solely as an opaque tie-breaker. Keyset pagination ordering tuples mandated by [ADR 0008](0008-cursor-pagination-contract.md) (`(detected_at DESC, id DESC)` for `/v1/changes`, `(digest_date DESC, id DESC)` for `/v1/digests`) adhere strictly to this `(business_timestamp, id)` paradigm. Current-fact resolution uses the four-part tuple `(observed_at DESC, snapshot_id DESC, extraction_version DESC, id DESC)`: `observed_at` is the sole business chronology; `snapshot_id` is an opaque deterministic tie-breaker — never decoded, never independent business chronology, exactly like `id` elsewhere — used only when two distinct snapshots share one `observed_at`; `extraction_version` is compared strictly after `snapshot_id`, so it orders corrections *within* one already-identified snapshot and never lets a higher version number from a different, unrelated snapshot outrank a lower one from the correct snapshot; `id` remains the final unique tie-breaker.
- **Canonical subject identity:** Following `FactStore._subject_key()` (`intelligence/facts.py:115-118`), subjects are keyed in the database by canonical normalized strings (`company_key`, `product_key`) computed via `normalise_name()`, preventing duplicate records from incidental casing, whitespace, or punctuation differences.
- **Cross-table relational consistency:** Composite foreign keys guarantee that a `Change` cannot reference a `ChangeSet` belonging to a different subject, and that `current_facts` cannot reference an `extracted_facts` row with mismatched subject, field, timestamp, or extraction version (mirroring the referential integrity pattern in ADR 0002 §9).
- **Three-layer immutability enforcement:** As established in [ADR 0008](0008-cursor-pagination-contract.md) §5.D and ADR 0002 §11, immutability is guaranteed across model freezing, repository method restriction, and database storage-level trigger enforcement.
- **Trigger-based storage mechanism:** Rather than managing complex column-level privileges, `BEFORE UPDATE`, `BEFORE DELETE`, and `BEFORE TRUNCATE` database triggers are used to reject mutations on protected ordering columns, Change history, and evidence attributes. On `changes`, every column except `review_status` is immutable against update, and rows cannot be deleted or truncated. Once a `Digest` enters `published` status, its title, claims, and claim citations are permanently locked against insert, update, reassignment, or deletion.
- **Whole-row immutability for source evidence:** `extracted_facts` mirror `document_snapshots` from ADR 0002 §11 — once written, facts are immutable evidence. Ordinary `UPDATE`, `DELETE`, and `TRUNCATE` operations are rejected via triggers. Corrections or updates append a new row and retain full provenance.
- **Referential integrity across module boundaries:** All snapshot citations reference `document_snapshots(id)` with `ON DELETE RESTRICT` foreign keys. `ChangeSet` citation lists are derived on read from immutable `changes` rows ordered by first occurrence (`MIN(position) ASC`), guaranteeing zero drift by construction without redundant join tables. `digest_claim_citations` uses a normalized join table with parent-scoped position ordering.
- **Explicit position columns for all child collections:** Child collections with pipeline sequence (`changes` within a `change_set`, `digest_claims` within a `digest`, and `digest_claim_citations` within claims) carry explicit parent-scoped `position INTEGER NOT NULL CHECK (position >= 0)` columns (`UNIQUE (parent_id, position)`), replacing heuristic UUIDv7 sequence reconstruction.

---

## 2. What already exists — verified against the tree

The shared contracts and intelligence domain models already define the required fields, invariants, and validation boundaries. The citations below for `shared/schemas.py`, `intelligence/facts.py`, and `docs/adr/0008-cursor-pagination-contract.md` have been opened and confirmed in the repository tree. Citations referencing `ADR 0002` have been verified against `docs/adr/0002-postgres-pgvector.md` on `main` (commit `181c025` at the time of original citation; the document was subsequently accepted via PR #65, commit `336cf48`; Status: Accepted by Persons A, B, and C):

| Component | Existing definition / location | Status & Invariants |
|---|---|---|
| `Change.id` | `src/ai_daily_digest/shared/schemas.py:439` | `id: Uuid7Id = Field(frozen=True)` ([ADR 0007](0007-uuid-v7-identifier-strategy.md) / [ADR 0008](0008-cursor-pagination-contract.md)) |
| `Change.detected_at` | `src/ai_daily_digest/shared/schemas.py:447` | `detected_at: OrderingTimestamp = Field(frozen=True)` (UTC, microseconds preserved, naive rejected) |
| `Change` shape validator | `src/ai_daily_digest/shared/schemas.py:327-409` | `validate_change_shape()` strictly enforces observation shapes per `change_type` |
| `ChangeSet` aggregate | `src/ai_daily_digest/shared/schemas.py:461-472` | `id`, `subject: Subject`, `changes: list[Change]`, `previous_snapshot_ids`, `current_snapshot_ids`, `review_status` |
| `Digest.id` | `src/ai_daily_digest/shared/schemas.py:520` | `id: Uuid7Id = Field(frozen=True)` |
| `Digest.digest_date` | `src/ai_daily_digest/shared/schemas.py:521` | `digest_date: date = Field(frozen=True)` (Native calendar date, YYYY-MM-DD on wire) |
| `DigestClaim` | `src/ai_daily_digest/shared/schemas.py:500-509` | `id: Uuid7Id`, `text: str`, `citation_snapshot_ids: list[Uuid7Id]`, `validation_status: ClaimValidationStatus` |
| `ExtractedFact` | `src/ai_daily_digest/shared/schemas.py:195-298` | `id: Uuid7Id`, `snapshot_id: Uuid7Id`, `field: str`, `value: str \| None`, `disclosure_status`, `extraction_method`, `extraction_model`, `prompt_version`, `quoted_span`, `confidence`; evidence invariants enforced via model validators ([ADR 0004](0004-extracted-fact-keeps-evidence.md), [ADR 0006](0006-disclosure-status-semantics.md)) |
| `Subject` value type | `src/ai_daily_digest/shared/schemas.py:151-165` | `company: str`, `product: str`; frozen, hashable value object without a surrogate ID |
| `FactStore._subject_key` | `src/ai_daily_digest/intelligence/facts.py:115-118` | `(normalise_name(company), normalise_name(product))` defines the canonical identity in-memory |
| `FactStore._FieldRecord` | `src/ai_daily_digest/intelligence/facts.py:162-175` | In-memory state: `current: ExtractedFact \| None`, provenance (`current_snapshot_id`, `current_source_url`, `current_observed_at`), and `history: list[ExtractedFact]`. In storage, `observed_at` is stored directly on `extracted_facts` and `changes` and validated against `document_snapshots.fetched_at`, while `source_url` is derived via snapshot join (`document_snapshots` → `source_items.canonical_url`). |
| Ordering tuple protection | `docs/adr/0008-cursor-pagination-contract.md:212-219` | Exactly 6 protected columns across the service: `SourceItem.id`, `SourceItem.first_fetched_at`, `Change.id`, `Change.detected_at`, `Digest.id`, `Digest.digest_date` |
| Protected-column test suite | `docs/adr/0008-cursor-pagination-contract.md:767-775` | Mandatory 4-part test pattern: reject update, safe rollback, stored value survives, no repository mutation path |
| Keyset pagination index | ADR 0002 §9.1 | Composite descending index `(first_fetched_at DESC, id DESC)` precedent |
| Composite FK consistency | ADR 0002 §9 | Composite FK constraint precedent (`source_items.latest_snapshot_id` → `document_snapshots(id, source_item_id)`) |
| Trigger-based immutability | ADR 0002 §11 | Precedent: `BEFORE UPDATE` trigger function raising exception on protected column modification |
| Immutable snapshots | ADR 0002 §11 | Precedent: row-level `UPDATE`/`DELETE` and statement-level `TRUNCATE` triggers protecting raw source snapshots |
| Shared database kernel | ADR 0002 §12.3 | `src/ai_daily_digest/shared/db/` provides engine, sessionmaker, and naming convention/`MetaData` |

---

## 3. Architectural Decisions

| # | Decision | Selected Approach | Rationale | Rejected Alternatives |
|---|---|---|---|---|
| 1 | **`changes` table schema & observation contract** | Flattened typed columns (`previous_value`, `previous_observed_at`, `previous_snapshot_id`, and `current_*`). Both `current_value` and `previous_value` are nullable (`TEXT`). **Formal amendment:** `FactObservation.observed_at` is required (`datetime`). | Follows ADR 0002 §10's rationale against `jsonb`: yields strict database type-checking and direct SQL indexing. Per [ADR 0006](0006-disclosure-status-semantics.md), `not_disclosed` transitions legitimately produce `current_value=None` while retaining real citation evidence. This decision **formally amends ADR 0008 §5.A and docs/API_CONTRACT.md** to make `FactObservation.observed_at: datetime` required (non-nullable; `previous` is null only on first-ever disclosure). Updating `shared/schemas.py`, `docs/API_CONTRACT.md`, [ADR 0008](0008-cursor-pagination-contract.md), and unit test fixtures is formally included in the persistence implementation scope. `review_status` remains an unconstrained open string (`TEXT NOT NULL DEFAULT 'pending'`) per [ADR 0009](0009-shared-enum-policy.md). | Storing observations as `jsonb`. Rejected: sacrifices schema-level type enforcement. Leaving `FactObservation.observed_at` optional while the database requires it. Rejected: leaves a permanent contract mismatch. Adding an ad-hoc 3-value CHECK constraint on `review_status`. Rejected: violates ADR 0009's open-set decision. |
| 2 | **`changes` immutability & shape enforcement** | `BEFORE UPDATE` trigger on `changes` enforcing immutability of **all columns except `review_status`**; `BEFORE DELETE` and `BEFORE TRUNCATE` triggers rejecting removal. Database `CHECK` constraint mirroring `validate_change_shape()`. | Fulfills [ADR 0008](0008-cursor-pagination-contract.md) §5.D storage-level immutability for the keyset ordering tuple `(detected_at DESC, id DESC)` and protects historical Change evidence against deletion or truncation. Mirroring `validate_change_shape()` in SQL prevents contradictory or corrupt rows (e.g. `not_disclosed` with a non-null current value, or partial observations without timestamps) from being permanently frozen into storage. Only `review_status` transitions are mutable. | Protecting only `(id, detected_at)`. Rejected: allows silent mutation or deletion of historical evidence. Omitting shape CHECK constraints. Rejected: allows invalid rows to be inserted and permanently locked. |
| 3 | **`digests` table, immutability & publish uniqueness** | Native `DATE` column for `digest_date`; `BEFORE UPDATE` trigger on `(id, digest_date)`. Once `status = 'published'`, `title` is permanently frozen, and status cannot transition away from `published`. Unique partial index `uq_digests_one_published_per_date` on `(digest_date) WHERE status = 'published'`. | Fulfills [ADR 0008](0008-cursor-pagination-contract.md) §5.B, §5.D, and §12 for `/v1/digests` ordering `(digest_date DESC, id DESC)`. Retried or reprocessed daily pipeline runs require publish idempotency: multiple draft attempts for a date are legitimate during drafting/review, but exactly one published digest may exist per calendar date. An attempt to publish a second digest fails closed at the database level. | Unconstrained updates or string-based date storage. Rejected: string dates permit invalid days and mutable ordering fields break keyset pagination traversals. Omitting the unique publish index was rejected because it allows duplicate published digests on retry. |
| 4 | **Referential integrity & provenance derivation** | `ChangeSet.previous_snapshot_ids` and `current_snapshot_ids` are derived on read from immutable `changes` rows ordered by first occurrence (`MIN(position) ASC`). `digest_claim_citations` uses a normalized join table with `ON DELETE RESTRICT`. `source_url` is derived via JOIN (`document_snapshots` → `source_items.canonical_url`) rather than stored denormalized on `extracted_facts` and `changes`. `observed_at` is stored on `extracted_facts` and `changes` and strictly enforced against `document_snapshots.fetched_at` via `INSERT` triggers. Once a digest is published, its child claims and citations are frozen against insert, update, reassignment, or delete. | Deriving ChangeSet citations directly from `changes` eliminates a redundant join table (`change_set_snapshot_citations`), avoids position/reassignment maintenance, and guarantees zero drift and first-occurrence order preservation by construction. Deriving `source_url` at read time via the immutable snapshot chain (`document_snapshots` and `source_items` are immutable per ADR 0002 §11) eliminates redundant text storage and mathematically precludes drift. Retaining `observed_at` on `extracted_facts` and `changes` is strictly required for keyset indexing and composite FK checks with `current_facts`. | PostgreSQL `UUID[]` arrays. Rejected: arrays cannot enforce foreign-key constraints in PostgreSQL, allowing dangling snapshot IDs or silent deletion of primary evidence. Storing an unenforced denormalized `source_url` on `extracted_facts` and `changes` was rejected because it duplicates immutable parent data and risks drift. |
| 5 | **Current-fact resolution, consistency & advancement** | Current fact resolved via explicit `(observed_at DESC, snapshot_id DESC, extraction_version DESC, id DESC)` ordering — `observed_at` is business chronology; `snapshot_id` is the opaque deterministic tie-breaker when two distinct snapshots share one `observed_at`; `extraction_version` orders corrections only *within* one already-matched snapshot; `id` is the final unique tie-breaker. Dedicated `current_facts` pointer table stores `fact_id`, `snapshot_id`, and `extraction_version` (integer revision). Updated conditionally: `(current_facts.observed_at, current_facts.snapshot_id, current_facts.extraction_version, current_facts.fact_id) < (EXCLUDED.observed_at, EXCLUDED.snapshot_id, EXCLUDED.extraction_version, EXCLUDED.fact_id)` with `RETURNING fact_id`. Composite FK `(fact_id, company_key, product_key, field, snapshot_id, observed_at, extraction_version)` proves pointer consistency, including that the pointer's `snapshot_id` matches its referenced `extracted_facts` row. Batch-level advisory locking on `(subject, field)` in lexicographical order. | Conforms strictly to [ADR 0007](0007-uuid-v7-identifier-strategy.md) lines 154-161 (UUID is an opaque tie-breaker, never primary chronology) — `snapshot_id` is used exactly the same way `id` already is, compared only after the business timestamp, never decoded. Within the same snapshot, higher `extraction_version` supersedes older versions; across *different* snapshots, `extraction_version` is never compared at all, because a version number carries no meaning outside the snapshot that produced it. Advisory locking on `(subject, field)` serializes read-compare-advance sequences, eliminating first-write races and ensuring a concurrent writer computes its Change against the newly committed state rather than a stale read. Using `RETURNING fact_id` directly controls Change emission: if zero rows return, no pointer advanced, and no Change is produced. | Resolving current fact by highest `id`. Rejected: violates ADR 0007. Allowing UUID order to determine which extraction version wins. Rejected: UUID has no version semantics. Comparing `extraction_version` before, or without, `snapshot_id`. Rejected: `(observed_at, extraction_version, id)` alone lets a higher version number from an unrelated (possibly older-arriving) snapshot outrank a lower version from the correct, newer snapshot merely because the number is larger — extraction_version has no cross-snapshot ordering meaning. Leaving `current_facts` with only a single-column FK on `fact_id`. Rejected: allows direct SQL writes to create pointer rows with mixed identity or divergent timestamps. |
| 6 | **Canonical subject identity & consistency** | `subjects` primary key is `(company_key, product_key)` derived via `normalise_name()`, storing original `company`/`product` display values (first-seen wins). Child tables (`extracted_facts`, `current_facts`, `change_sets`, `changes`) key on `(company_key, product_key)`. `change_sets` enforces `UNIQUE (id, company_key, product_key)`; `changes` references it via composite FK `(change_set_id, company_key, product_key)`. | Replicates `FactStore._subject_key()` in SQL so that equivalent inputs (`"OpenAI"` / `"openai"` / `"OpenAI."`) collide deterministically into a single subject. The composite FK on `changes` mirrors ADR 0002 §9 (`source_items.latest_snapshot_id` → `document_snapshots(id, source_item_id)`), ensuring a Change cannot reference a ChangeSet belonging to a different subject. | Raw `PRIMARY KEY (company, product)`. Rejected: allows duplicate subject rows due to casing or punctuation differences. Plain `change_set_id` FK on `changes`. Rejected: permits a Change to reference a ChangeSet with a mismatched subject. |
| 7 | **Correction policy & versioned re-extraction** | Integer `extraction_version >= 1` assigned by the pipeline. `uq_extracted_facts_attempt` on `(snapshot_id, company_key, product_key, field, extraction_version)`. Correcting the current snapshot updates `current_facts` to the new version but **emits NO Change**. Correcting an older snapshot appends to history without advancing `current_facts` and emits no Change. | When an extractor is corrected (e.g. parser bug fix or prompt improvement), the underlying source observation did not change; emitting a business Change would mislead subscribers into believing pricing or features changed. A `Change` requires distinct snapshots across time. Identical retries re-read and verify all 10 immutable fact attributes, failing closed on discrepancy. | Emitting a normal Change on extraction correction. Rejected: generates false business changes. Scoping uniqueness only to model/prompt. Rejected: deterministic extractions have null models/prompts, causing permanent conflicts on code updates. |
| 8 | **Explicit position columns for child list reconstruction** | Explicit `position INTEGER NOT NULL CHECK (position >= 0)` on `changes` (`UNIQUE (change_set_id, position)`), `digest_claims` (`UNIQUE (digest_id, position)`), and `digest_claim_citations` (`UNIQUE (claim_id, position)`). Reconstructed via `ORDER BY position ASC`. | Domain collections have deterministic sequence assigned by the pipeline during construction. Storing explicit 0-indexed or 1-indexed `position` columns guarantees exact sequence reproduction across database round-trips without relying on UUIDv7 timestamps or heuristic creation order. | Reconstructing order via `ORDER BY id ASC`. Rejected: conflates opaque identifier generation with pipeline sequence, creating ungrounded chronological assumptions. |
| 9 | **Module ownership & shared kernel connection** | Intelligence-owned under `src/ai_daily_digest/intelligence/db/`. Shared engine, sessionmaker, and metadata imported from `shared/db/` per ADR 0002 §12.3. | Maintains strict modular boundaries: intelligence defines its own ORM models and repository implementations. Shared database connectivity lives in `shared/db/` per ADR 0002 §12.3. | Placing intelligence tables or repositories in `ingestion/db/` or `shared/`. Rejected: violates module review stewardship (Person B owns intelligence). |

> **Risk note on canonical subject keys:** Consistent with ADR 0002's treatment of `source_items.dedupe_key`, `company_key` and `product_key` are generated application-side by `normalise_name()` before persistence. Any future modification to `normalise_name()`'s normalization rules represents a canonicalization policy change and requires an explicit migration and backfill plan, not an uncoordinated code change.

---

## 4. Proposed Database Schema (PostgreSQL DDL)

The schema definitions below will be managed via Alembic migrations owned by the intelligence module. The DDL is presented in strictly executable order.

```sql
-- -----------------------------------------------------------------------------
-- Shared Trigger Helper Functions (Defined before first trigger reference)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION reject_row_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'Table % is append-only: updates and deletes are prohibited', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION reject_table_truncate()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'Table % is append-only: truncate is prohibited', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

-- -----------------------------------------------------------------------------
-- Subjects Registry (Canonical Subject Identity)
-- -----------------------------------------------------------------------------
CREATE TABLE subjects (
    company_key         TEXT NOT NULL,
    product_key         TEXT NOT NULL,
    company             TEXT NOT NULL, -- original display name (first-seen wins)
    product             TEXT NOT NULL, -- original display name (first-seen wins)
    created_at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (company_key, product_key)
);

-- -----------------------------------------------------------------------------
-- Extracted Facts (Whole-row immutable evidence)
-- -----------------------------------------------------------------------------
CREATE TABLE extracted_facts (
    id                  UUID PRIMARY KEY,
    snapshot_id         UUID NOT NULL REFERENCES document_snapshots(id) ON DELETE RESTRICT,
    company_key         TEXT NOT NULL,
    product_key         TEXT NOT NULL,
    field               TEXT NOT NULL,
    value               TEXT,          -- NULL when disclosure_status = 'not_disclosed'
    disclosure_status   TEXT NOT NULL CHECK (disclosure_status IN ('disclosed', 'not_disclosed')),
    extraction_method   TEXT NOT NULL CHECK (extraction_method IN ('deterministic', 'llm_structured_output')),
    extraction_model    TEXT,          -- required for llm_structured_output per schemas.py:195-200
    prompt_version      TEXT,          -- required for llm_structured_output per schemas.py:195-200
    extraction_version  INTEGER NOT NULL CHECK (extraction_version >= 1), -- explicit integer revision
    quoted_span         TEXT,
    confidence          DOUBLE PRECISION CHECK (
                            confidence IS NULL OR
                            (confidence >= 0 AND confidence <= 1 AND confidence = confidence)
                        ),
    -- Business chronology timestamp from snapshot (enforced by trigger against document_snapshots.fetched_at)
    observed_at         TIMESTAMPTZ NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (company_key, product_key) REFERENCES subjects(company_key, product_key) ON DELETE RESTRICT,

    -- Stable extraction attempt identity (deduplicates identical attempts; permits versioned corrections):
    CONSTRAINT uq_extracted_facts_attempt
        UNIQUE (snapshot_id, company_key, product_key, field, extraction_version),

    -- Target of composite FK from current_facts proving pointer-row consistency
    -- (snapshot_id included so the pointer's snapshot cannot be spoofed independently
    -- of observed_at/extraction_version -- see the corrected ordering tuple below):
    CONSTRAINT uq_extracted_facts_composite_identity
        UNIQUE (id, company_key, product_key, field, snapshot_id, observed_at, extraction_version),

    -- Mirror Python-side evidence invariants from shared/schemas.py:
    CONSTRAINT chk_extracted_facts_disclosure_value CHECK (
        (disclosure_status = 'disclosed' AND value IS NOT NULL AND trim(value) != '') OR
        (disclosure_status = 'not_disclosed' AND value IS NULL AND quoted_span IS NOT NULL AND trim(quoted_span) != '')
    ),
    CONSTRAINT chk_extracted_facts_llm_evidence CHECK (
        extraction_method != 'llm_structured_output' OR (
            quoted_span IS NOT NULL AND trim(quoted_span) != '' AND
            confidence IS NOT NULL AND
            extraction_model IS NOT NULL AND trim(extraction_model) != '' AND
            prompt_version IS NOT NULL AND trim(prompt_version) != ''
        )
    )
);

-- Chronological business ordering index -- observed_at is business chronology; snapshot_id
-- is the opaque tie-breaker for equal observed_at; extraction_version orders corrections only
-- within one already-matched snapshot; id is the final unique tie-breaker:
CREATE INDEX idx_extracted_facts_subject_field_chronology
    ON extracted_facts (company_key, product_key, field, observed_at DESC, snapshot_id DESC, extraction_version DESC, id DESC);

CREATE INDEX idx_extracted_facts_snapshot_id
    ON extracted_facts (snapshot_id);

-- Storage-level provenance verification: observed_at must match snapshot fetched_at
CREATE OR REPLACE FUNCTION validate_fact_observed_at()
RETURNS TRIGGER AS $$
DECLARE
    snapshot_fetched_at TIMESTAMPTZ;
BEGIN
    SELECT fetched_at INTO snapshot_fetched_at
    FROM document_snapshots
    WHERE id = NEW.snapshot_id;

    IF snapshot_fetched_at IS NULL THEN
        RAISE EXCEPTION 'Referenced document snapshot % does not exist', NEW.snapshot_id;
    END IF;

    IF NEW.observed_at IS DISTINCT FROM snapshot_fetched_at THEN
        RAISE EXCEPTION 'extracted_facts.observed_at (%) does not match document_snapshots.fetched_at (%)',
            NEW.observed_at, snapshot_fetched_at;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_validate_extracted_fact_observed_at
BEFORE INSERT ON extracted_facts
FOR EACH ROW
EXECUTE FUNCTION validate_fact_observed_at();

CREATE TRIGGER trg_protect_extracted_facts_immutable_update
BEFORE UPDATE ON extracted_facts
FOR EACH ROW
EXECUTE FUNCTION reject_row_mutation();

CREATE TRIGGER trg_protect_extracted_facts_immutable_delete
BEFORE DELETE ON extracted_facts
FOR EACH ROW
EXECUTE FUNCTION reject_row_mutation();

CREATE TRIGGER trg_protect_extracted_facts_immutable_truncate
BEFORE TRUNCATE ON extracted_facts
FOR EACH STATEMENT
EXECUTE FUNCTION reject_table_truncate();

-- -----------------------------------------------------------------------------
-- Current Facts Pointer Table (Fast O(1) current state resolution)
-- -----------------------------------------------------------------------------
CREATE TABLE current_facts (
    company_key         TEXT NOT NULL,
    product_key         TEXT NOT NULL,
    field               TEXT NOT NULL,
    fact_id             UUID NOT NULL,
    -- snapshot_id: opaque deterministic tie-breaker when two distinct snapshots share one
    -- observed_at. Compared BEFORE extraction_version in the ordering tuple below, so a
    -- correction's extraction_version only ever orders revisions within this one snapshot.
    snapshot_id         UUID NOT NULL,
    observed_at         TIMESTAMPTZ NOT NULL,
    extraction_version  INTEGER NOT NULL CHECK (extraction_version >= 1),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (company_key, product_key, field),
    FOREIGN KEY (company_key, product_key) REFERENCES subjects(company_key, product_key) ON DELETE RESTRICT,
    -- Composite ownership FK proving pointer-row consistency with extracted_facts --
    -- including that this pointer's snapshot_id genuinely matches the referenced fact's
    -- snapshot_id, not just its observed_at/extraction_version:
    FOREIGN KEY (fact_id, company_key, product_key, field, snapshot_id, observed_at, extraction_version)
        REFERENCES extracted_facts(id, company_key, product_key, field, snapshot_id, observed_at, extraction_version) ON DELETE RESTRICT
);

-- -----------------------------------------------------------------------------
-- ChangeSets (Batch-level grouping of changes)
-- -----------------------------------------------------------------------------
CREATE TABLE change_sets (
    id                  UUID PRIMARY KEY,
    company_key         TEXT NOT NULL,
    product_key         TEXT NOT NULL,
    review_status       TEXT NOT NULL DEFAULT 'pending', -- open string per ADR 0009
    created_at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (company_key, product_key) REFERENCES subjects(company_key, product_key) ON DELETE RESTRICT,
    -- Composite unique constraint required for composite FK reference from changes:
    UNIQUE (id, company_key, product_key)
);

-- -----------------------------------------------------------------------------
-- Changes (Individual field-level detected changes)
-- -----------------------------------------------------------------------------
CREATE TABLE changes (
    id                      UUID PRIMARY KEY,
    detected_at             TIMESTAMPTZ NOT NULL,
    change_set_id           UUID NOT NULL,
    position                INTEGER NOT NULL CHECK (position >= 0),
    company_key             TEXT NOT NULL,
    product_key             TEXT NOT NULL,
    field                   TEXT NOT NULL,
    change_type             TEXT NOT NULL, -- open string per ADR 0009
    confidence              DOUBLE PRECISION NOT NULL CHECK (
                                confidence >= 0 AND confidence <= 1 AND confidence = confidence
                            ),
    review_status           TEXT NOT NULL DEFAULT 'pending', -- open string per ADR 0009

    -- Previous observation (NULL if first disclosure; timestamps enforced when snapshot present)
    previous_value          TEXT,
    previous_observed_at    TIMESTAMPTZ,
    previous_snapshot_id    UUID REFERENCES document_snapshots(id) ON DELETE RESTRICT,

    -- Current observation (value is NULL if change_type = 'not_disclosed')
    current_value           TEXT,
    current_observed_at     TIMESTAMPTZ NOT NULL,
    current_snapshot_id     UUID NOT NULL REFERENCES document_snapshots(id) ON DELETE RESTRICT,

    created_at              TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    -- Enforce that a Change references a valid Subject:
    FOREIGN KEY (company_key, product_key) REFERENCES subjects(company_key, product_key) ON DELETE RESTRICT,
    -- Enforce that a Change shares the exact subject of its parent ChangeSet (ADR 0002 §9 pattern):
    FOREIGN KEY (change_set_id, company_key, product_key)
        REFERENCES change_sets(id, company_key, product_key) ON DELETE RESTRICT,
    -- Explicit position within the parent change set:
    CONSTRAINT uq_changes_change_set_position
        UNIQUE (change_set_id, position),

    -- Mirror validate_change_shape() cross-column invariants (shared/schemas.py:327-409):
    CONSTRAINT chk_changes_shape CHECK (
        (change_type = 'not_disclosed' AND
            previous_value IS NOT NULL AND previous_snapshot_id IS NOT NULL AND previous_observed_at IS NOT NULL AND
            current_value IS NULL AND current_snapshot_id IS NOT NULL AND current_observed_at IS NOT NULL)
        OR
        (change_type = 'disclosed' AND
            current_value IS NOT NULL AND current_snapshot_id IS NOT NULL AND current_observed_at IS NOT NULL AND
            previous_value IS NULL AND
            ((previous_snapshot_id IS NULL AND previous_observed_at IS NULL) OR
             (previous_snapshot_id IS NOT NULL AND previous_observed_at IS NOT NULL)))
        OR
        (change_type NOT IN ('not_disclosed', 'disclosed') AND
            previous_value IS NOT NULL AND previous_snapshot_id IS NOT NULL AND previous_observed_at IS NOT NULL AND
            current_value IS NOT NULL AND current_snapshot_id IS NOT NULL AND current_observed_at IS NOT NULL)
    )
);

-- Keyset pagination index for GET /v1/changes (ADR 0008 §4):
CREATE INDEX idx_changes_pagination
    ON changes (detected_at DESC, id DESC);

-- Query filter indexes:
CREATE INDEX idx_changes_subject_field
    ON changes (company_key, product_key, field);

CREATE INDEX idx_changes_change_set_id
    ON changes (change_set_id);

CREATE INDEX idx_changes_previous_snapshot_id
    ON changes (previous_snapshot_id);

CREATE INDEX idx_changes_current_snapshot_id
    ON changes (current_snapshot_id);

-- Storage-level provenance verification on changes
CREATE OR REPLACE FUNCTION validate_change_provenance()
RETURNS TRIGGER AS $$
DECLARE
    current_fetched_at TIMESTAMPTZ;
    prev_fetched_at TIMESTAMPTZ;
BEGIN
    SELECT fetched_at INTO current_fetched_at
    FROM document_snapshots
    WHERE id = NEW.current_snapshot_id;

    IF current_fetched_at IS NULL THEN
        RAISE EXCEPTION 'Current snapshot % does not exist', NEW.current_snapshot_id;
    END IF;

    IF NEW.current_observed_at IS DISTINCT FROM current_fetched_at THEN
        RAISE EXCEPTION 'changes.current_observed_at (%) does not match document_snapshots.fetched_at (%)',
            NEW.current_observed_at, current_fetched_at;
    END IF;

    IF NEW.previous_snapshot_id IS NOT NULL THEN
        SELECT fetched_at INTO prev_fetched_at
        FROM document_snapshots
        WHERE id = NEW.previous_snapshot_id;

        IF prev_fetched_at IS NULL THEN
            RAISE EXCEPTION 'Previous snapshot % does not exist', NEW.previous_snapshot_id;
        END IF;

        IF NEW.previous_observed_at IS DISTINCT FROM prev_fetched_at THEN
            RAISE EXCEPTION 'changes.previous_observed_at (%) does not match document_snapshots.fetched_at (%)',
                NEW.previous_observed_at, prev_fetched_at;
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_validate_change_provenance
BEFORE INSERT ON changes
FOR EACH ROW
EXECUTE FUNCTION validate_change_provenance();

CREATE OR REPLACE FUNCTION check_changes_immutability()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id OR
       NEW.detected_at IS DISTINCT FROM OLD.detected_at OR
       NEW.change_set_id IS DISTINCT FROM OLD.change_set_id OR
       NEW.position IS DISTINCT FROM OLD.position OR
       NEW.company_key IS DISTINCT FROM OLD.company_key OR
       NEW.product_key IS DISTINCT FROM OLD.product_key OR
       NEW.field IS DISTINCT FROM OLD.field OR
       NEW.change_type IS DISTINCT FROM OLD.change_type OR
       NEW.confidence IS DISTINCT FROM OLD.confidence OR
       NEW.previous_value IS DISTINCT FROM OLD.previous_value OR
       NEW.previous_observed_at IS DISTINCT FROM OLD.previous_observed_at OR
       NEW.previous_snapshot_id IS DISTINCT FROM OLD.previous_snapshot_id OR
       NEW.current_value IS DISTINCT FROM OLD.current_value OR
       NEW.current_observed_at IS DISTINCT FROM OLD.current_observed_at OR
       NEW.current_snapshot_id IS DISTINCT FROM OLD.current_snapshot_id OR
       NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'Cannot update immutable columns on changes (only review_status may be updated)';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_protect_changes_immutability
BEFORE UPDATE ON changes
FOR EACH ROW
EXECUTE FUNCTION check_changes_immutability();

CREATE TRIGGER trg_protect_changes_delete
BEFORE DELETE ON changes
FOR EACH ROW
EXECUTE FUNCTION reject_row_mutation();

CREATE TRIGGER trg_protect_changes_truncate
BEFORE TRUNCATE ON changes
FOR EACH STATEMENT
EXECUTE FUNCTION reject_table_truncate();

-- -----------------------------------------------------------------------------
-- Digests (Published or draft daily updates)
-- -----------------------------------------------------------------------------
CREATE TABLE digests (
    id                  UUID PRIMARY KEY,
    digest_date         DATE NOT NULL,
    status              TEXT NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft', 'review', 'published')),
    title               TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

-- Unique index enforcing at most one published digest per calendar date (idempotent retries):
CREATE UNIQUE INDEX uq_digests_one_published_per_date
    ON digests (digest_date)
    WHERE status = 'published';

-- Keyset pagination index for GET /v1/digests (ADR 0008 §4 & §12: published-only in Phase 1):
CREATE INDEX idx_digests_pagination
    ON digests (digest_date DESC, id DESC)
    WHERE status = 'published';

CREATE OR REPLACE FUNCTION check_digests_immutability()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id THEN
        RAISE EXCEPTION 'Cannot update immutable column digests.id';
    END IF;
    IF NEW.digest_date IS DISTINCT FROM OLD.digest_date THEN
        RAISE EXCEPTION 'Cannot update immutable column digests.digest_date';
    END IF;
    -- Once published, title is locked and status cannot be reverted:
    IF OLD.status = 'published' AND NEW.title IS DISTINCT FROM OLD.title THEN
        RAISE EXCEPTION 'Cannot update title of an already published digest';
    END IF;
    IF OLD.status = 'published' AND NEW.status IS DISTINCT FROM 'published' THEN
        RAISE EXCEPTION 'Cannot unpublish an already published digest';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_protect_digests_immutability
BEFORE UPDATE ON digests
FOR EACH ROW
EXECUTE FUNCTION check_digests_immutability();

-- -----------------------------------------------------------------------------
-- Digest Claims (Factual claims linked to a digest)
-- -----------------------------------------------------------------------------
CREATE TABLE digest_claims (
    id                  UUID PRIMARY KEY,
    digest_id           UUID NOT NULL REFERENCES digests(id) ON DELETE RESTRICT,
    position            INTEGER NOT NULL CHECK (position >= 0),
    text                TEXT NOT NULL,
    validation_status   TEXT NOT NULL DEFAULT 'pending'
                        CHECK (validation_status IN ('pending', 'supported', 'unsupported')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    -- Explicit position within the parent digest:
    CONSTRAINT uq_digest_claims_digest_position
        UNIQUE (digest_id, position)
);

CREATE INDEX idx_digest_claims_digest_id
    ON digest_claims (digest_id);

-- Lock claims of published digests against INSERT, UPDATE, REASSIGNMENT, and DELETE:
CREATE OR REPLACE FUNCTION check_published_digest_claims_immutability()
RETURNS TRIGGER AS $$
DECLARE
    parent_status TEXT;
BEGIN
    IF TG_OP = 'INSERT' THEN
        SELECT status INTO parent_status FROM digests WHERE id = NEW.digest_id FOR KEY SHARE;
        IF parent_status = 'published' THEN
            RAISE EXCEPTION 'Cannot insert claims into an already published digest (%)', NEW.digest_id;
        END IF;
        RETURN NEW;
    ELSIF TG_OP = 'DELETE' THEN
        SELECT status INTO parent_status FROM digests WHERE id = OLD.digest_id FOR KEY SHARE;
        IF parent_status = 'published' THEN
            RAISE EXCEPTION 'Cannot delete claims from an already published digest (%)', OLD.digest_id;
        END IF;
        RETURN OLD;
    ELSIF TG_OP = 'UPDATE' THEN
        -- Verify old parent (prevents reassignment out of published digest)
        SELECT status INTO parent_status FROM digests WHERE id = OLD.digest_id FOR KEY SHARE;
        IF parent_status = 'published' THEN
            RAISE EXCEPTION 'Cannot reassign or modify claims of an already published digest (%)', OLD.digest_id;
        END IF;
        -- Verify new parent (prevents reassignment into published digest)
        IF NEW.digest_id IS DISTINCT FROM OLD.digest_id THEN
            SELECT status INTO parent_status FROM digests WHERE id = NEW.digest_id FOR KEY SHARE;
            IF parent_status = 'published' THEN
                RAISE EXCEPTION 'Cannot reassign claims into an already published digest (%)', NEW.digest_id;
            END IF;
        END IF;
        RETURN NEW;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_protect_published_digest_claims_insert
BEFORE INSERT ON digest_claims
FOR EACH ROW
EXECUTE FUNCTION check_published_digest_claims_immutability();

CREATE TRIGGER trg_protect_published_digest_claims_update
BEFORE UPDATE ON digest_claims
FOR EACH ROW
EXECUTE FUNCTION check_published_digest_claims_immutability();

CREATE TRIGGER trg_protect_published_digest_claims_delete
BEFORE DELETE ON digest_claims
FOR EACH ROW
EXECUTE FUNCTION check_published_digest_claims_immutability();

CREATE TRIGGER trg_protect_published_digest_claims_truncate
BEFORE TRUNCATE ON digest_claims
FOR EACH STATEMENT
EXECUTE FUNCTION reject_table_truncate();

-- -----------------------------------------------------------------------------
-- Normalized Citations for Digest Claims
-- -----------------------------------------------------------------------------
CREATE TABLE digest_claim_citations (
    claim_id            UUID NOT NULL REFERENCES digest_claims(id) ON DELETE RESTRICT,
    snapshot_id         UUID NOT NULL REFERENCES document_snapshots(id) ON DELETE RESTRICT,
    position            INTEGER NOT NULL CHECK (position >= 0),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (claim_id, snapshot_id),
    CONSTRAINT uq_digest_claim_citations_position
        UNIQUE (claim_id, position)
);

CREATE INDEX idx_digest_claim_citations_snapshot_id
    ON digest_claim_citations (snapshot_id);

CREATE OR REPLACE FUNCTION check_published_digest_claim_citations_immutability()
RETURNS TRIGGER AS $$
DECLARE
    parent_status TEXT;
BEGIN
    IF TG_OP = 'INSERT' THEN
        SELECT d.status INTO parent_status
        FROM digests d
        JOIN digest_claims dc ON dc.digest_id = d.id
        WHERE dc.id = NEW.claim_id
        FOR KEY SHARE;
        IF parent_status = 'published' THEN
            RAISE EXCEPTION 'Cannot insert citations for an already published digest';
        END IF;
        RETURN NEW;
    ELSIF TG_OP = 'DELETE' THEN
        SELECT d.status INTO parent_status
        FROM digests d
        JOIN digest_claims dc ON dc.digest_id = d.id
        WHERE dc.id = OLD.claim_id
        FOR KEY SHARE;
        IF parent_status = 'published' THEN
            RAISE EXCEPTION 'Cannot delete citations from an already published digest';
        END IF;
        RETURN OLD;
    ELSIF TG_OP = 'UPDATE' THEN
        -- Verify old parent claim
        SELECT d.status INTO parent_status
        FROM digests d
        JOIN digest_claims dc ON dc.digest_id = d.id
        WHERE dc.id = OLD.claim_id
        FOR KEY SHARE;
        IF parent_status = 'published' THEN
            RAISE EXCEPTION 'Cannot reassign or modify citations of an already published digest';
        END IF;
        -- Verify new parent claim
        IF NEW.claim_id IS DISTINCT FROM OLD.claim_id THEN
            SELECT d.status INTO parent_status
            FROM digests d
            JOIN digest_claims dc ON dc.digest_id = d.id
            WHERE dc.id = NEW.claim_id
            FOR KEY SHARE;
            IF parent_status = 'published' THEN
                RAISE EXCEPTION 'Cannot reassign citations into an already published digest';
            END IF;
        END IF;
        RETURN NEW;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_protect_published_digest_claim_citations_insert
BEFORE INSERT ON digest_claim_citations
FOR EACH ROW
EXECUTE FUNCTION check_published_digest_claim_citations_immutability();

CREATE TRIGGER trg_protect_published_digest_claim_citations_update
BEFORE UPDATE ON digest_claim_citations
FOR EACH ROW
EXECUTE FUNCTION check_published_digest_claim_citations_immutability();

CREATE TRIGGER trg_protect_published_digest_claim_citations_delete
BEFORE DELETE ON digest_claim_citations
FOR EACH ROW
EXECUTE FUNCTION check_published_digest_claim_citations_immutability();

CREATE TRIGGER trg_protect_published_digest_claim_citations_truncate
BEFORE TRUNCATE ON digest_claim_citations
FOR EACH STATEMENT
EXECUTE FUNCTION reject_table_truncate();

-- -----------------------------------------------------------------------------
-- Publication Gate Function & Trigger
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION check_digest_publication_prerequisites()
RETURNS TRIGGER AS $$
DECLARE
    unsupported_count INTEGER;
    claim_count INTEGER;
    uncited_count INTEGER;
BEGIN
    IF (TG_OP = 'INSERT' AND NEW.status = 'published') OR
       (TG_OP = 'UPDATE' AND NEW.status = 'published' AND OLD.status IS DISTINCT FROM 'published') THEN

        SELECT COUNT(*) INTO claim_count
        FROM digest_claims
        WHERE digest_id = NEW.id;

        IF claim_count = 0 THEN
            RAISE EXCEPTION 'Cannot publish digest %: digest has no claims', NEW.id;
        END IF;

        SELECT COUNT(*) INTO unsupported_count
        FROM digest_claims
        WHERE digest_id = NEW.id AND validation_status != 'supported';

        IF unsupported_count > 0 THEN
            RAISE EXCEPTION 'Cannot publish digest %: contains % unsupported claims', NEW.id, unsupported_count;
        END IF;

        SELECT COUNT(*) INTO uncited_count
        FROM digest_claims dc
        WHERE dc.digest_id = NEW.id
          AND NOT EXISTS (
              SELECT 1 FROM digest_claim_citations dcc WHERE dcc.claim_id = dc.id
          );

        IF uncited_count > 0 THEN
            RAISE EXCEPTION 'Cannot publish digest %: contains % claims without citations', NEW.id, uncited_count;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_enforce_digest_publication
BEFORE INSERT OR UPDATE ON digests
FOR EACH ROW
EXECUTE FUNCTION check_digest_publication_prerequisites();
```

---

## 5. Repository Protocols, Transaction Boundaries, and Concurrency Controls

Following the structure of ADR 0002 §12–13, intelligence persistence defines clear transaction boundaries, idempotency semantics, and publication gates.

### 5.1 Batch Transaction Granularity and Read/Compare Serialization

Repository methods **never** issue commits or rollbacks independently. The service or orchestrator layer (`intelligence/daily_run.py`, `intelligence/graph.py`) owns the transaction boundary via an injected `AsyncSession` context.

Observations for a subject are processed as a **batch unit**. To eliminate race conditions and avoid deadlocks across multiple concurrent workers:

1. **Deterministic Lexicographical Advisory Locking:**
   Before reading current state or processing observations for a subject batch, the transaction acquires PostgreSQL transaction-scoped advisory locks for each target field in strictly **lexicographical order** (`field ASC`):
   ```sql
   SELECT pg_advisory_xact_lock(hashtext(:company_key || '/' || :product_key || '/' || :field));
   ```
   This lock ordering:
   - Eliminates deadlock potential between concurrent transactions touching overlapping fields.
   - Serializes all concurrent workers before reading current state.
   - Eliminates first-write races when `current_facts` holds no row for a subject+field yet.
   - Automatically releases upon transaction `COMMIT` or `ROLLBACK`.

2. **Locked Read of Current State:**
   Inside the advisory lock, the worker queries the up-to-date current pointer and fact for each field:
   ```sql
   SELECT cf.fact_id, cf.snapshot_id, cf.observed_at, cf.extraction_version,
          ef.value, ef.quoted_span, ef.disclosure_status
   FROM current_facts cf
   JOIN extracted_facts ef ON ef.id = cf.fact_id
   WHERE cf.company_key = :company_key
     AND cf.product_key = :product_key
     AND cf.field = :field;
   ```
   If a prior concurrent worker committed an update while this worker was waiting for the lock, this query reads the freshly committed state (e.g. State B), never a stale pre-lock read (State A).

3. **Atomic Batch Write Path:**
   - **Append Facts to History:** Insert all extracted facts into `extracted_facts` with idempotent retry/conflict handling.
   - **Conditional Pointer Updates:**
     ```sql
     INSERT INTO current_facts (company_key, product_key, field, fact_id, snapshot_id, observed_at, extraction_version, updated_at)
     VALUES (:company_key, :product_key, :field, :fact_id, :snapshot_id, :observed_at, :extraction_version, clock_timestamp())
     ON CONFLICT (company_key, product_key, field) DO UPDATE
     SET fact_id            = EXCLUDED.fact_id,
         snapshot_id        = EXCLUDED.snapshot_id,
         observed_at        = EXCLUDED.observed_at,
         extraction_version = EXCLUDED.extraction_version,
         updated_at         = clock_timestamp()
     WHERE (current_facts.observed_at, current_facts.snapshot_id, current_facts.extraction_version, current_facts.fact_id) <
           (EXCLUDED.observed_at, EXCLUDED.snapshot_id, EXCLUDED.extraction_version, EXCLUDED.fact_id)
     RETURNING fact_id;
     ```
     `snapshot_id` is compared immediately after `observed_at` and before `extraction_version`
     in this row-value comparison, exactly matching the ordering tuple in Decision 5: two
     candidates sharing one `observed_at` resolve deterministically by `snapshot_id`, and
     `extraction_version` only breaks a tie *after* `snapshot_id` has already matched (i.e.
     within one snapshot) — never across two different snapshots.
   - **Change Decision & Emission:**
     - For each field, if the conditional update returns 0 rows (the observation is older or an identical revision), or if the value matches current state: **no Change is produced** for that field.
     - If the conditional update returns `fact_id` and the observation represents a genuine delta across different source snapshots: a `Change` is constructed.
     - If at least one Change is emitted for the subject, the worker inserts the parent `change_sets` row (`ON CONFLICT (id) DO NOTHING` for parent idempotency) and appends all `changes` rows with explicit `position` (0, 1, 2...).
     - The entire batch commits together. If any statement fails, the transaction rolls back cleanly, ensuring current state never advances without its corresponding Change rows.

### 5.2 Correction Policy vs. Replay Idempotency

1. **Explicit, Snapshot-Scoped `extraction_version` Assignment:**
   The extraction pipeline (`intelligence/extract_facts.py`) passes an explicit `extraction_version: int`. Standard extraction passes version `1`. When an extractor is corrected (e.g. deterministic parser fix, revised prompt, or updated model), the orchestrator assigns the next revision as `MAX(extraction_version) + 1` **scoped to that one `snapshot_id`** (`(snapshot_id, company_key, product_key, field)` — the same grouping `uq_extracted_facts_attempt` enforces), never as a global maximum across every snapshot for the subject/field. `extraction_version` is therefore only ever deterministically ordered *within* one snapshot; comparing it across two different snapshots is meaningless, which is exactly why the current-fact ordering tuple in Decision 5 compares `snapshot_id` first.

2. **Correction Policy (No False Business Changes):**
   - When a correction is re-extracted against the **current snapshot**, it carries the **same** `snapshot_id` and identical `observed_at` as the fact `current_facts` already points to, but an incremented `extraction_version`. Because `snapshot_id` matches exactly and `extraction_version` is strictly greater, `(observed_at, snapshot_id, extraction_version, id)` is strictly greater at the `extraction_version` position — the conditional update advances `current_facts` to point to the corrected fact. `extraction_version` is compared only *after* `snapshot_id` has already tied, so this comparison is scoped entirely to corrections of that one snapshot.
   - **Crucial Invariant:** Correcting an extractor's interpretation of an existing snapshot **emits NO product Change**. The real-world source document did not change. Emitting a `Change` would mislead subscribers and corrupt chronological change feeds. A `Change` requires distinct snapshots over time.
   - When an **older historical snapshot** is corrected, its `observed_at` is older than `current_facts.observed_at` (or, on a rarer tie, its `snapshot_id` loses the tie-break). The conditional update returns 0 rows, leaving `current_facts` untouched while the corrected fact is preserved in append-only history.
   - **Cross-snapshot non-comparability, stated explicitly:** `extraction_version` carries **no meaning across different snapshots**. A `snapshot_id` for a different, unrelated snapshot with `extraction_version = 5` can never outrank a lower `extraction_version` on the correct, current snapshot merely because `5 > 1` — the tuple comparison never reaches `extraction_version` unless `snapshot_id` already matched. This is the concrete failure mode the corrected `(observed_at, snapshot_id, extraction_version, id)` ordering prevents; the earlier `(observed_at, extraction_version, id)` tuple (without `snapshot_id`) did not prevent it.

3. **Replay Verification on Same Attempt:**
   Re-running the pipeline on the same snapshot with an identical `extraction_version` hits `ON CONFLICT (snapshot_id, company_key, product_key, field, extraction_version) DO NOTHING`:
   ```sql
   INSERT INTO extracted_facts (
       id, snapshot_id, company_key, product_key, field, value,
       disclosure_status, extraction_method, extraction_model, prompt_version,
       extraction_version, quoted_span, confidence, observed_at
   )
   VALUES (...)
   ON CONFLICT (snapshot_id, company_key, product_key, field, extraction_version)
   DO NOTHING
   RETURNING id;
   ```
   If no row is returned, the repository executes a verification re-read:
   ```sql
   SELECT id, observed_at, value, quoted_span, confidence, disclosure_status,
          extraction_method, extraction_model, prompt_version, extraction_version
   FROM extracted_facts
   WHERE snapshot_id = :snapshot_id
     AND company_key = :company_key
     AND product_key = :product_key
     AND field = :field
     AND extraction_version = :extraction_version;
   ```
   The repository asserts that every immutable attribute matches the attempted extraction. If any attribute differs on the exact same attempt key, the repository fails closed (raises an exception) to prevent silent acceptance of corrupted data.

### 5.3 Canonical Reconstruction Order for Child Lists

In the shared Python contract, `ChangeSet.changes`, `Digest.claims`, and `digest_claim_citations` are represented as ordered Python lists:
- **`ChangeSet.changes`:** Reconstructed via:
  ```sql
  SELECT * FROM changes WHERE change_set_id = :change_set_id ORDER BY position ASC;
  ```
  `position` is a 0-indexed integer assigned by the repository at insertion time directly from the pipeline's deterministic execution order. `UNIQUE (change_set_id, position)` guarantees no duplicate or colliding slots.
- **`ChangeSet.previous_snapshot_ids` and `current_snapshot_ids`:** Reconstructed dynamically via deterministic query grouped by snapshot ID and ordered by first occurrence (`MIN(position) ASC`) to preserve the domain pipeline's first-occurrence order:
  ```sql
  SELECT current_snapshot_id
  FROM changes
  WHERE change_set_id = :id
  GROUP BY current_snapshot_id
  ORDER BY MIN(position) ASC;

  SELECT previous_snapshot_id
  FROM changes
  WHERE change_set_id = :id AND previous_snapshot_id IS NOT NULL
  GROUP BY previous_snapshot_id
  ORDER BY MIN(position) ASC;
  ```
  Because `changes` rows are permanent and protected against deletion/truncation, ChangeSet citation evidence is immutable by construction with zero risk of drift, and ordering strictly respects the pipeline's Change sequence rather than relying on UUID sort order.
- **`Digest.claims`:** Reconstructed via `SELECT * FROM digest_claims WHERE digest_id = :digest_id ORDER BY position ASC`.
- **`digest_claim_citations`:** Reconstructed via `SELECT * FROM digest_claim_citations WHERE claim_id = :claim_id ORDER BY position ASC`.

### 5.4 Fail-Closed Publication Transition and Child Mutation Mutual Exclusion

Per the core project invariant ("LLM output is never treated as evidence; claims must cite stored source records"), a digest cannot enter `published` status unless every claim is supported and cited.

Under the default `READ COMMITTED` isolation established in ADR 0002, counting claims and citations during a publication transition could race with concurrent transactions adding, modifying, reassigning, or deleting child claims. To serialize concurrent operations and eliminate races:

1. **Mutual Exclusion Locking Protocol:**
   - **Child Mutation Path:** Any transaction inserting, updating, or deleting rows in `digest_claims` or `digest_claim_citations` must first acquire a shared key lock on the parent `digests` row:
     ```sql
     SELECT id FROM digests WHERE id = :digest_id FOR KEY SHARE;
     ```
   - **Publication Transition Path:** The publication transaction acquires an exclusive row lock on the parent `digests` row before running validation checks:
     ```sql
     SELECT id FROM digests WHERE id = :digest_id FOR UPDATE;
     ```
   - Because `FOR UPDATE` and `FOR KEY SHARE` conflict in PostgreSQL, any active child transaction blocks the publish attempt until child writes finish. Conversely, once publication locks the parent, child transactions block until publication completes, upon which they observe `status = 'published'` and fail immediately.

2. **Storage-Level Publication Gate Trigger (Covering `INSERT` and `UPDATE`):**
   A direct `INSERT INTO digests (..., status) VALUES (..., 'published')` or `UPDATE` bypassing the service layer is caught and rejected by `trg_enforce_digest_publication`: on a direct `INSERT` with `status = 'published'`, child claims cannot yet exist because the `digest_claims.digest_id` foreign key requires the parent `digests` row to exist first. Consequently, `claim_count = 0` raises an exception and cleanly prevents unverified publication on insert.

---

## 6. Storage-Level Immutability and Provenance Enforcement

To satisfy [ADR 0008](0008-cursor-pagination-contract.md) §5.D and ADR 0002 §11, the DDL in Section 4 enforces immutability and provenance through database triggers:

1. **`changes` Immutability & Provenance:**
   - Every column on `changes` except `review_status` is locked against modification via `trg_protect_changes_immutability`.
   - Historical change rows cannot be deleted (`trg_protect_changes_delete`) or truncated (`trg_protect_changes_truncate`).
   - `trg_validate_change_provenance` verifies that `current_observed_at` and `previous_observed_at` strictly match `document_snapshots.fetched_at`.
   - `chk_changes_shape` mirrors `validate_change_shape()`, requiring valid observation timestamp and snapshot combinations across all change types.

2. **`digests` and Child Immutability:**
   - `digests(id, digest_date)` are locked against update. Once `published`, `title` is frozen and unpublishing is prohibited (`trg_protect_digests_immutability`).
   - `digest_claims` and `digest_claim_citations` are locked against insertion, modification, reassignment, deletion, and truncation once the parent digest is published.

3. **`extracted_facts` Immutability & Provenance:**
   - Whole-row immutability: updates and deletions are rejected via `trg_protect_extracted_facts_immutable_update` and `trg_protect_extracted_facts_immutable_delete`.
   - Statement-level truncation is rejected via `trg_protect_extracted_facts_immutable_truncate`.
   - `trg_validate_extracted_fact_observed_at` asserts that `observed_at` matches `document_snapshots.fetched_at`.

### Scope and Limitations of Storage Enforcement

Consistent with ADR 0002 §11:
- Database triggers protect ordinary application DML while enabled.
- Triggers do **not** prevent a PostgreSQL superuser or table owner from running `ALTER TABLE ... DISABLE TRIGGER`, dropping tables (`DROP TABLE`), or executing catalog updates.
- Operational security requires that the application runtime connects via a restricted PostgreSQL role granted only `SELECT`, `INSERT`, and constrained `UPDATE` privileges (with no `DELETE`, `TRUNCATE`, or DDL privileges). Restricting application database privileges is a required production deployment gate.

---

## 7. Testing and Verification Requirements

Per [ADR 0008](0008-cursor-pagination-contract.md) §14, any persistence implementation PR based on this ADR must implement the mandatory 4-part immutability test suite for each protected table:

1. **Reject update:** Directly executing an `UPDATE` on protected columns (`changes` evidence fields, `changes.position`, `changes.detected_at`, `digests.id`, `digests.digest_date`, or any column in `extracted_facts`) raises a database error.
2. **Safe rollback:** The failed update aborts the transaction cleanly without partial writes or database corruption.
3. **Value survival:** Re-reading the record in a new transaction confirms the original value is preserved.
4. **No repository update path:** The repository protocol and implementation expose no methods for mutating protected columns.

Additional required integration test suites:
- **`current_facts` pointer-row consistency test:** Directly attempting to insert into `current_facts` with a `fact_id` whose `observed_at`, `snapshot_id`, `extraction_version`, `company_key`, `product_key`, or `field` differs from the referenced `extracted_facts` row fails with a foreign key violation — specifically including a `current_facts` row that names the *correct* `fact_id` but a **mismatched `snapshot_id`** (a candidate composite pointer consistency cannot silently accept).
- **Two-connection concurrent read/compare & advance tests:**
  - *Case 1 (Waiting writer is newer):* Connection 1 processes fact A → B. Connection 2 processes fact C (newer than B). Connection 2 blocks on the advisory lock. Upon waking, Connection 2 reads committed state B and records Change as B → C with exact values.
  - *Case 2 (Waiting writer is older):* Connection 1 processes fact B. Connection 2 processes fact A (older). Connection 2 blocks on the advisory lock. Upon waking, Connection 2's conditional update returns 0 rows, advances nothing, and emits no Change.
  - *Case 3 (First-write race):* Two connections attempt the initial observation for a subject+field concurrently. Both serialize on the advisory lock without failing on an empty pointer table.
- **Correction vs. Replay semantics tests:**
  - *Correction of current snapshot:* Incrementing `extraction_version` on the current snapshot (same `snapshot_id`, same `observed_at`) advances `current_facts` to the corrected row but emits **no Change**.
  - *Correction of older snapshot:* Incrementing `extraction_version` on an older snapshot appends to `extracted_facts` history, updates 0 rows in `current_facts`, and emits no Change.
  - *Replay verification:* Replaying identical extraction version hits `ON CONFLICT DO NOTHING`, re-reads all 10 attributes, and succeeds. Replaying with mismatched attributes fails closed.
  - *Higher extraction version wins for the same snapshot:* two extraction attempts against the identical `snapshot_id` with `extraction_version = 1` then `extraction_version = 2` leave `current_facts` pointing at version 2.
  - *Cross-snapshot version non-comparability:* `extraction_version = 5` recorded against an **older or unrelated** `snapshot_id` does **not** outrank `extraction_version = 1` on the current, correct `snapshot_id` merely because `5 > 1` — the conditional update returns 0 rows and `current_facts` is unchanged, proving `snapshot_id` is compared before `extraction_version`, not the reverse.
  - *Equal `observed_at`, distinct `snapshot_id`:* two facts for the same `(company_key, product_key, field)`, from two different snapshots that happen to share an identical `observed_at`, resolve deterministically via `snapshot_id` — never via `extraction_version` or `id` — as the tie-breaker; running the pair in both arrival orders produces the same winner.
- **ChangeSet citation derivation test:** Verify that querying `ChangeSet.previous_snapshot_ids` and `current_snapshot_ids` dynamically from `changes` produces exact, deduplicated snapshot ID lists preserving domain first-occurrence order (`MIN(position) ASC`), specifically tested using repeated snapshot IDs whose UUID sort order intentionally differs from Change position.
- **Merged transaction atomicity test:** An intentional error during Change insertion rolls back the fact append, pointer advancement, changeset, and changes together, ensuring zero partial writes.
- **`changes` immutability, deletion, and truncation protection tests:**
  - Directly executing `DELETE FROM changes WHERE id = ...` raises an exception from `trg_protect_changes_delete`.
  - Directly executing `TRUNCATE TABLE changes` raises an exception from `trg_protect_changes_truncate`.
  - Stored records survive both attempted operations.
- **`changes` cross-column shape & provenance constraint tests:**
  - Inserting a `change_type = 'not_disclosed'` row with a non-null `current_value` or missing `previous_observed_at` fails with `chk_changes_shape`.
  - Inserting a `change_type = 'disclosed'` row with a non-null `previous_value` fails with `chk_changes_shape`.
  - Inserting a `change_type = 'changed'` row with null previous observation fields fails with `chk_changes_shape`.
  - Inserting a `changes` row where `current_observed_at` or `previous_observed_at` does not match `document_snapshots.fetched_at` fails with an exception from `trg_validate_change_provenance`.
- **Publication gate `INSERT`-bypass & child lock race tests:**
  - Directly executing `INSERT INTO digests (..., status) VALUES (..., 'published')` is rejected because claim count is zero.
  - Directly executing `INSERT INTO digest_claims` referencing an already-published digest is rejected.
  - Concurrently executing a claim update/delete while a publication transaction holds `FOR UPDATE` on the parent digest blocks until publish finishes and fails closed.
  - Attempting to reassign a claim out of a published digest (`UPDATE digest_claims SET digest_id = ...`) or into a published digest fails with an exception.
  - Attempting `TRUNCATE TABLE digest_claims` or `TRUNCATE TABLE digest_claim_citations` fails with an exception.
- **Python-side evidence CHECK constraint tests on `extracted_facts`:**
  - Inserting an `extracted_facts` row with `disclosure_status = 'disclosed'` and `value IS NULL` or empty string fails with `chk_extracted_facts_disclosure_value`.
  - Inserting an `extracted_facts` row with `disclosure_status = 'not_disclosed'` and a non-null `value` fails.
  - Inserting an `extracted_facts` row with `disclosure_status = 'not_disclosed'` and empty `quoted_span` fails.
  - Inserting an `extracted_facts` row with `extraction_method = 'llm_structured_output'` and missing/empty `quoted_span`, `confidence`, `extraction_model`, or `prompt_version` fails with `chk_extracted_facts_llm_evidence`.
  - Inserting an `extracted_facts` row where `observed_at` differs from the referenced `document_snapshots.fetched_at` fails with an exception from `trg_validate_extracted_fact_observed_at`.
- **Position column sequence and uniqueness tests:**
  - Inserting negative `position` values fails with `CHECK (position >= 0)`.
  - Inserting duplicate `(change_set_id, position)`, `(digest_id, position)`, or `(claim_id, position)` rows fails with a unique constraint violation.
  - Querying child collections with `ORDER BY position ASC` matches the pipeline's exact deterministic construction sequence.
  - Attempting to update `position` on an existing `changes` row is rejected by `check_changes_immutability()`.
- **Canonical subject collision & idempotency test:** Inserting `"OpenAI"` then `"openai"` then `"OpenAI."` for the same product resolves idempotently to a single row in `subjects` with canonical key `("openai", ...)` and stores the first-seen display casing.
- **ChangeSet↔Change subject consistency test:** Directly attempting to insert a `changes` row whose `(company_key, product_key)` does not match the referenced `change_set_id` fails with a foreign key violation.
- **Published digest immutability test:**
  - Attempting to update `title` or revert `status` on a published `digests` row fails with an exception.
  - Attempting to update or delete a `digest_claims` or `digest_claim_citations` row belonging to a published digest fails with an exception.
- **Publish idempotency test:** Attempting to publish two distinct digests for the exact same `digest_date` results in the second publish failing closed via `uq_digests_one_published_per_date`.
- **Statement-level truncate rejection:** Executing `TRUNCATE TABLE extracted_facts` fails with an explicit exception and preserves all rows.
- **Referential integrity & deletion restriction:**
  - Attempting to insert an `extracted_facts`, `changes`, or citation row with a non-existent `snapshot_id` fails with a foreign key violation.
  - Attempting to delete a `document_snapshots` row that is referenced by an `extracted_facts`, `changes`, or citation row fails with `ON DELETE RESTRICT`.
- **Keyset pagination query test:** Keyset traversal using `(detected_at, id) < (cursor_time, cursor_id)` and `(digest_date, id) < (cursor_date, cursor_id)` fetches `limit + 1` rows, slices correctly, and preserves pagination consistency.

---

## 8. Deferred Scope

The following areas are explicitly deferred to separate decisions and implementation PRs:

| Deferred Item | Owner & Target | Reason |
|---|---|---|
| FastAPI route wiring (`/v1/changes`, `/v1/digests`) | Person C / ADR 0010 | HTTP endpoints, route contracts, and serialization models belong to Delivery. |
| Vector index & embeddings (`pgvector`) | Person B / Future Phase | Semantic search is a derived index and not required for baseline relational persistence. |
| Subscriptions and email delivery tables | Person C | Delivery-internal persistence. |
| Shared `ReviewStatus` StrEnum promotion | Person B / Future ADR | `review_status` remains an open string per ADR 0009. Promoting it to a shared `StrEnum` or adding state-machine validation is deferred until lifecycle requirements are finalized. |
| Digest superseding and correction workflow | Person B / Future ADR | Uniqueness of a published digest per calendar date is resolved by `uq_digests_one_published_per_date`. The operational lifecycle for deliberately retracting, superseding, or version-revising an already-published digest (e.g. edition v2) is deferred to a dedicated workflow decision. |

*(Note: Shared database kernel placement in `src/ai_daily_digest/shared/db/` was specified in ADR 0002 §12.3 and merged into `main` via PR #48, removing it from the deferred scope).*

---

## 9. Acceptance Checklist

- [ ] **Person A (Ingestion Steward):**
  - Confirms schema compatibility with `source_items` and `document_snapshots` (referential integrity from `extracted_facts.snapshot_id`, `changes.current_snapshot_id`, and citations).
  - Confirms canonical subject keying `(company_key, product_key)` matches `normalise_name()` semantics.
  - Confirms composite `(change_set_id, company_key, product_key)` FK enforces subject consistency across changes and change sets.
  - Confirms `current_facts` composite FK (including `snapshot_id`) proves pointer consistency against `extracted_facts`, and that the corrected `(observed_at, snapshot_id, extraction_version, id)` ordering never lets a higher `extraction_version` on a different snapshot outrank a lower one on the correct snapshot.
  - Confirms shared database kernel alignment with ADR 0002 §12.3 (`shared/db/`).
- [ ] **Person B (Intelligence Steward — Author):**
  - Confirms all domain invariants from `FactStore`, `Change`, and `Digest` are preserved.
  - Verifies that `extracted_facts` and `changes` accurately capture disclosure semantics ([ADR 0006](0006-disclosure-status-semantics.md)) and change shapes (`validate_change_shape()`).
  - Verifies current-fact advisory locking serializes read/compare/advance sequences and prevents stale Change emissions.
  - Verifies integer `extraction_version >= 1` separates identical retries from versioned corrections, and that corrections emit no false business changes.
  - Verifies ChangeSet citation derivation from `changes` eliminates redundant join tables while guaranteeing immutable evidence and first-occurrence ordering.
  - Verifies evidence immutability protects historical change records and published digests against update, delete, and truncation.
- [ ] **Person C (Delivery Steward):**
  - Confirms keyset indexes `idx_changes_pagination` and `idx_digests_pagination` match the requirements of [ADR 0008](0008-cursor-pagination-contract.md) §4 and §12.
  - Confirms that public summary models (`ChangeSummary`, `DigestSummary`) can be projected efficiently from this schema.
  - Confirms publication prerequisites trigger enforces fail-closed guarantees across `INSERT`, `UPDATE`, and child mutations via mutual exclusion row locking.
