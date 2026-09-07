"""The ingestion service's per-item write sequence
(docs/adr/0002-postgres-pgvector.md section 13). Owns exactly **one**
`AsyncSession` transaction per ingested item: the repository is bound to
that session and never commits or rolls back on its own (section 13),
so this module -- not the repository -- is where transaction ownership
actually lives.

Not a collector, and performs no network I/O: `ingest_document()` takes
an already-fetched, already-normalized `FetchedDocument` and persists
it. Collecting, fetching, and normalizing content is a collector's job,
explicitly out of scope for this PR (ADR 0002 section 16, "Explicitly
excluded: collectors and any external network call").
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from ai_daily_digest.ingestion.persistence import IngestionWriteRepository, SourceItemMetadata
from ai_daily_digest.shared.ids import new_id
from ai_daily_digest.shared.schemas import DocumentSnapshot, SourceItem


@dataclass(frozen=True)
class FetchedDocument:  # pylint: disable=too-many-instance-attributes
    """An already-fetched, already-normalized document -- the input to
    `ingest_document()`. Producing one (collecting, normalizing,
    content-hashing) is a collector's job, out of scope for this PR.

    One field per SourceItem identity/DocumentSnapshot storage column
    this document carries (ADR 0002 sections 8, 9) -- a plain data
    holder, not a class with behaviour to simplify."""

    dedupe_key: str
    source_id: str
    canonical_url: str
    first_fetched_at: datetime
    fetched_at: datetime
    content_hash: str
    content_text: str
    metadata: SourceItemMetadata
    raw_location: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    collector_version: str | None = None


@dataclass(frozen=True)
class IngestResult:
    """What one `ingest_document()` call produced.

    `item_created` / `snapshot_created` say whether this call inserted a
    new row versus finding an existing one -- derived from whether the
    id generated up front came back (a found row always carries a
    different, older id). A collection-run report needs both counts, and
    there is no other way to learn them from the write protocol's
    return values.

    `advanced` is exactly `advance_latest_snapshot_and_metadata`'s own
    return value when it ran, or `False` when step 3 never ran at all
    (no new snapshot was inserted -- section 13's Phase-1 limitation)."""

    source_item: SourceItem
    snapshot: DocumentSnapshot
    item_created: bool
    snapshot_created: bool
    advanced: bool


async def ingest_document(
    session: AsyncSession,
    repository: IngestionWriteRepository,
    document: FetchedDocument,
) -> IngestResult:
    """Run the per-item write sequence (ADR 0002 section 13) inside the
    caller-supplied session's transaction, and commit exactly once, only
    after every write has succeeded.

    Any exception rolls this transaction back before propagating --
    **for a new item, no rows persist at all; for an existing item, only
    this attempt's changes are lost**, never rows a previous run already
    committed (section 13, "Rollback behaviour"). The caller (a future
    collection-run orchestrator, out of scope here) is expected to catch
    the re-raised exception, record this item as failed, and continue
    with the next one -- "one source failure does not abort others"
    (`docs/ARCHITECTURE.md`) is a property of that caller, not of this
    function, which only guarantees a clean, fully-rolled-back failure.
    """
    try:
        candidate_item_id = new_id()
        source_item = await repository.find_or_create_source_item(
            item_id=candidate_item_id,
            dedupe_key=document.dedupe_key,
            source_id=document.source_id,
            canonical_url=document.canonical_url,
            first_fetched_at=document.first_fetched_at,
            metadata=document.metadata,
        )
        item_created = source_item.id == candidate_item_id

        # A candidate id generated up front: add_snapshot_if_new()
        # returns this exact id back only when it actually inserted a
        # new row -- an existing row's id (found via the unique
        # (source_item_id, content_hash) conflict path) can never equal
        # a freshly generated UUID v7. That equality is the "was a new
        # snapshot inserted" signal section 13 step 3 needs, without
        # the repository protocol having to return an extra flag.
        candidate_snapshot_id = new_id()
        snapshot = await repository.add_snapshot_if_new(
            snapshot_id=candidate_snapshot_id,
            source_item_id=source_item.id,
            fetched_at=document.fetched_at,
            content_hash=document.content_hash,
            content_text=document.content_text,
            raw_location=document.raw_location,
            etag=document.etag,
            last_modified=document.last_modified,
            collector_version=document.collector_version,
        )

        snapshot_created = snapshot.id == candidate_snapshot_id
        advanced = False
        # Section 13 step 3: "If, and only if, a new snapshot was
        # inserted" -- a duplicate-content fetch never reaches this call
        # at all (the documented Phase-1 limitation: metadata is not
        # independently refreshed for a duplicate-content fetch).
        if snapshot_created:
            advanced = await repository.advance_latest_snapshot_and_metadata(
                source_item_id=source_item.id,
                snapshot_id=snapshot.id,
                fetched_at=snapshot.fetched_at,
                metadata=document.metadata,
            )

        await session.commit()
    except Exception:
        await session.rollback()
        raise

    return IngestResult(
        source_item=source_item,
        snapshot=snapshot,
        item_created=item_created,
        snapshot_created=snapshot_created,
        advanced=advanced,
    )
