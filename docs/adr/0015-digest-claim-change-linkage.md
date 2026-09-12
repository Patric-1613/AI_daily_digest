# 0015 — Digest claim linkage to originating changes and structured diff projection

Status: Accepted (authored by Person B; agreed by Person A and Person C)
Date: 2026-09-12

## Context

Following ADR 0014 (§5) and Issue #112, the `GET /v1/digests/{digest_id}` endpoint exposes published digest prose claims and official source citations (`snapshot_id`, `canonical_url`, `source_title`).

Issue #122 ("feat(intelligence): link digest claims to originating changes and surface structured before/after diffs") addresses the requirement to link published factual claims directly to their originating `Change` entities and surface structured diff information. This accomplishes:
1. Direct relational traceability from published claims back to underlying detected facts and changes.
2. Structured before/after diff inspection (`company`, `product`, `field`, `change_type`, `previous_value`, `current_value`) for frontend UIs and downstream consumers without fragile prose text parsing.
3. Clean separation of immutable historical evidence and claim projections.

## Decision

### 1. Database Schema & Foreign Key Relationship

- Added nullable `change_id` (UUIDv7) to the `digest_claims` table referencing `changes.id`:
  ```sql
  ALTER TABLE digest_claims ADD COLUMN change_id UUID REFERENCES changes(id) ON DELETE SET NULL;
  CREATE INDEX idx_digest_claims_change_id ON digest_claims(change_id);
  ```
- **Nullability**: `change_id` is nullable (`nullable=True`) so existing/legacy digest claims remain valid and uncorrupted without speculative backfills.
- **Deletion Semantics**: While PostgreSQL immutability trigger `trg_protect_changes_delete` prevents row deletions on `changes` in production, `ON DELETE SET NULL` at the database foreign key level guarantees that even in non-trigger environments or future archival routines, published digest claims remain valid and uncorrupted.
- **Migration**: Managed via Alembic migration `0004_digest_claim_change_linkage.py` with full `upgrade()` and `downgrade()` support.

### 2. Structured Change Domain & Delivery API Models

- **Shared Domain Model** (`src/ai_daily_digest/shared/schemas.py`):
  ```python
  class DigestClaimChange(BaseModel):
      id: Uuid7Id
      company: str
      product: str
      field: str
      change_type: str
      previous_value: str | None = None
      current_value: str | None = None
  ```
  `DigestClaim` includes `change_id: Uuid7Id | None = None` and `change: DigestClaimChange | None = None`.

- **Delivery Public Schema** (`src/ai_daily_digest/delivery/api/schemas.py`):
  ```python
  class DigestClaimChangeDetail(BaseModel):
      id: Uuid7Id
      company: str
      product: str
      field: str
      change_type: str
      previous_value: str | None = None
      current_value: str | None = None
  ```
  `DigestClaimDetail` includes `change_id: Uuid7Id | None = None` and `change: DigestClaimChangeDetail | None = None`.

- **Backward Compatibility**: Unlinked claims or legacy claims serialize `change_id: null` and `change: null`. Existing consumer clients (including PR #123) that read existing fields (`id`, `text`, `citations`, `validation_status`) continue working without disruption.

### 3. Claim Drafting & Persistence

- `draft_change_claim(change: Change)` in `src/ai_daily_digest/intelligence/draft_claims.py` sets `change_id=change.id` deterministically during drafting.
- `PostgresFactStore._insert_claims_and_citations()` persists `change_id` preserving ordering and idempotency.

### 4. Repository Hydration (Zero N+1 Queries)

- `PostgresFactStore._hydrate_digests()` collects all non-null `change_id` values across all claims in the requested batch and executes a single batch `SELECT` joining `ChangeModel` with `SubjectModel`.
- Populates `DigestClaim.change` with `DigestClaimChange(...)` using exact stored values.
- If a claim has `change_id` set but the corresponding change record is missing or invalid, the route layer `GET /v1/digests/{digest_id}` fails closed with HTTP 500 (`internal_error`) to prevent serving incomplete evidence.

### 5. Safety & Privacy

- Relational linkage preserves existing fail-closed publication invariants from ADR 0014 (only published digests are readable; claims must have `validation_status="supported"` and valid citations).
- Internal fields, prompts, embeddings, and credentials are never leaked.

## Consequences

- Direct evidence traceability from published digest claims to underlying changes is established at the relational and API levels.
- Consumers can inspect structured diffs directly without text scraping.
- Migration and domain models maintain 100% backward compatibility for existing digests.
