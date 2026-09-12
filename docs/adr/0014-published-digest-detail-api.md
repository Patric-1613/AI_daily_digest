# 0014 — Published digest-detail API endpoint (`GET /v1/digests/{digest_id}`)

Status: Proposed by Person B (intelligence review steward); review requested from Person C (delivery steward) and Person A (ingestion steward)
Date: 2026-09-12

## Context

AI Daily Digest previously established:
- The public `GET /v1/digests` list endpoint returning paginated `DigestSummary` items (ADR 0008, ADR 0010).
- The intelligence persistence aggregate in PostgreSQL storing `DigestModel` and `DigestClaimModel` rows (ADR 0011).
- The production pipeline generating grounded, evidence-backed daily digests (`assemble_digest.py`, `run.py`).

While `GET /v1/digests` lists published digest summaries (`id`, `digest_date`, `status`, `title`), consumers (such as web frontends, email adapters, and downstream subscribers) need a detailed view of a specific published digest. This view must include its grounded claim texts, validation statuses, and citation snapshot identifiers for auditability and traceability.

This decision is coordinated through issue #112. Person B (`@SujinJK`) is the active author. Person C (`@chamath-wijayasundara`) is the delivery review steward. Person A (`@Patric-1613`) provides non-author review.

## Decision

### 1. HTTP Route & Path Parameter

`GET /v1/digests/{digest_id}`

- `digest_id` is an RFC 9562 UUID v7 path parameter.
- A malformed UUID in the path returns HTTP 422 with the standard error envelope.

### 2. Published-Only Fail-Closed Security Gate

The endpoint serves public consumers and must strictly enforce publication boundaries:
- Only digests with `status == DigestStatus.PUBLISHED` (wire value `"published"`) are returned.
- If a requested digest does not exist, or if it is currently in `"draft"` or `"review"` status, the server returns **HTTP 404**:
  ```json
  {
    "error": {
      "code": "digest_not_found",
      "message": "The requested digest was not found.",
      "request_id": "01a0331b-e020-7170-949d-8d85b81bfa91",
      "details": {}
    }
  }
  ```
- This prevents draft, unvalidated, or review-pending intelligence from being exposed before two-layer publication gates pass.

### 3. Response Schema (`DigestDetail`)

```json
{
  "id": "01a034ed-e100-7e73-ab06-1fecafdc495c",
  "digest_date": "2026-08-24",
  "status": "published",
  "title": "AI Daily Digest — 24 August 2026",
  "claims": [
    {
      "id": "01a034ed-e100-74d1-8508-247704ced117",
      "text": "Example Model now supports a 256k-token context window.",
      "citation_snapshot_ids": [
        "01a032cd-23e0-76d3-a27c-f608ccc02226"
      ],
      "validation_status": "supported"
    }
  ]
}
```

- `claims` is a list of `DigestClaimDetail` items ordered deterministically by `id ASC`.
- Each claim includes its `id`, grounded `text`, list of `citation_snapshot_ids`, and `validation_status` (`"supported"` | `"unsupported"`).
- Public responses never contain raw LLM prompts, intermediate reasoning tokens, vector embeddings, database credentials, or subscriber email addresses.

### 4. Shared Repository Protocol

The `DigestFeedRepository` protocol in `src/ai_daily_digest/shared/repositories.py` is extended with:

```python
async def get_published_digest(self, digest_id: uuid.UUID) -> Digest | None:
    """Retrieve a published digest by ID with claims loaded.
    
    Returns None if the digest does not exist or its status is not PUBLISHED.
    """
```

`PostgresFactStore` in `src/ai_daily_digest/intelligence/db/repository.py` implements this method by querying `DigestModel` with `selectinload(DigestModel.claims)` and filtering by `id == digest_id` and `status == DigestStatus.PUBLISHED.value`.

## Consequences

- The public API now provides complete, evidence-traceable detail for published digests.
- Consumers can render grounded claims and navigate to cited snapshot sources.
- Draft and review digests remain completely private and unreachable via the public HTTP interface.
