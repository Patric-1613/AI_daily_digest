# 0015 — Digest claim linkage to originating changes

Status: Proposed by Person B (intelligence review steward); review requested from Person C (delivery steward) and Person A (ingestion steward)
Date: 2026-09-12

## Context

Following ADR 0014 (§5) and Issue #112, the `GET /v1/digests/{digest_id}` endpoint exposes published digest prose claims and official source citations (`snapshot_id`, `canonical_url`, `source_title`).

Issue #122 ("feat(intelligence): link digest claims to originating changes and surface structured before/after diffs") addresses the need to link published factual claims directly to the originating `Change` entity. This enables:
1. Direct relational traceability from digest claims back to underlying detected changes.
2. Structured before/after diff inspection for frontend UIs and downstream consumers without fragile text parsing.
3. Clean separation of immutable historical evidence and claim projections.

## Decision

### 1. Database Schema & Foreign Key Relationship

- Added nullable `change_id` (UUIDv7) to the `digest_claims` table referencing `changes.id`:
  ```sql
  ALTER TABLE digest_claims ADD COLUMN change_id UUID REFERENCES changes(id) ON DELETE SET NULL;
  CREATE INDEX idx_digest_claims_change_id ON digest_claims(change_id);
  ```
- **Nullability**: `change_id` is nullable (`nullable=True`) so existing/legacy digest claims remain valid and uncorrupted without requiring speculative backfills.
- **Deletion Semantics**: `ON DELETE SET NULL` ensures that if a change record is ever purged or archived, published digest claims remain readable and valid with historical citations intact.
- **Migration**: Managed via Alembic migration `0004_digest_claim_change_linkage.py` with full `upgrade()` and `downgrade()` support.

### 2. Domain & Delivery API Models

- **Shared Domain Model** (`src/ai_daily_digest/shared/schemas.py`):
  `DigestClaim` includes `change_id: Uuid7Id | None = None`.
- **Delivery Public Schema** (`src/ai_daily_digest/delivery/api/schemas.py`):
  `DigestClaimDetail` includes `change_id: Uuid7Id | None = None`.
- **Backward Compatibility**: Unlinked claims or legacy claims serialize `change_id: null`. Existing consumer clients (including PR #123) that read existing fields (`id`, `text`, `citations`, `validation_status`) continue working without disruption.

### 3. Claim Drafting & Persistence

- `draft_change_claim(change: Change)` in `src/ai_daily_digest/intelligence/draft_claims.py` sets `change_id=change.id` deterministically during drafting.
- `PostgresFactStore._hydrate_digests()` and `_insert_claims_and_citations()` hydrate and persist `change_id` preserving ordering and idempotency.

### 4. Safety & Privacy

- Relational linkage preserves existing fail-closed publication invariants from ADR 0014 (only published digests are readable; claims must have `validation_status="supported"` and valid citations).
- Internal fields, prompts, embeddings, and credentials are never leaked.

## Consequences

- Direct evidence traceability from published digest claims to underlying changes is established at the relational and API levels.
- Migration and domain models maintain 100% backward compatibility for existing digests.
- Consumers can navigate from claims to detailed structured changes via `change_id`.
