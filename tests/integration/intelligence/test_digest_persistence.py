"""Integration tests for PostgresDigestRepository persistence and publication — ADR 0011."""
# pylint: disable=too-many-locals

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from ai_daily_digest.ingestion.db.models import DocumentSnapshotRow, SourceItemRow
from ai_daily_digest.intelligence.db.repository import (
    PostgresDigestRepository,
    PostgresFactStore,
)
from ai_daily_digest.shared.ids import new_id
from ai_daily_digest.shared.repositories import DigestRepository
from ai_daily_digest.shared.schemas import (
    ClaimValidationStatus,
    Digest,
    DigestClaim,
    DigestStatus,
    DocumentSnapshot,
)
from ai_daily_digest.shared.snapshot_resolver import InMemorySnapshotResolver

pytestmark = pytest.mark.integration

_OpenSession = Callable[[], AbstractAsyncContextManager[AsyncSession]]
BASE_TIME = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)


async def _create_snapshot(
    session: AsyncSession,
    *,
    content_text: str = "Sample snapshot content",
    fetched_at: datetime = BASE_TIME,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Helper to create a SourceItem and DocumentSnapshot for foreign key satisfaction."""
    item_id = new_id()
    snap_id = new_id()
    item = SourceItemRow(
        id=item_id,
        dedupe_key=f"sha256:{item_id}",
        source_id="openai_news",
        publisher="OpenAI",
        title="Sample Release",
        canonical_url=f"https://openai.example.com/news/{item_id}",
        first_fetched_at=fetched_at,
    )
    session.add(item)
    await session.flush()

    snap = DocumentSnapshotRow(
        id=snap_id,
        source_item_id=item_id,
        content_hash=f"hash-{snap_id}",
        fetched_at=fetched_at,
        content_text=content_text,
    )
    session.add(snap)
    await session.flush()
    return item_id, snap_id


@pytest.mark.asyncio
async def test_digest_repository_protocol_conformance(
    database_session: AsyncSession,
) -> None:
    """PostgresFactStore and PostgresDigestRepository conform to DigestRepository protocol."""
    repo = PostgresDigestRepository(database_session)
    assert isinstance(repo, DigestRepository)
    assert isinstance(PostgresFactStore(database_session), DigestRepository)


@pytest.mark.asyncio
async def test_persist_digest_with_claims_and_citations_exact_positional_order(
    database_session: AsyncSession,
) -> None:
    """persist_digest() writes digest, claims, and citations; get_digest_by_id() preserves order."""
    _, snap_a = await _create_snapshot(database_session, content_text="Snapshot A")
    _, snap_b = await _create_snapshot(database_session, content_text="Snapshot B")

    repo = PostgresDigestRepository(database_session)

    digest_id = new_id()
    claim_1_id = new_id()
    claim_2_id = new_id()

    # Claim 1 has 2 citations [snap_a, snap_b] in specific order
    claim_1 = DigestClaim(
        id=claim_1_id,
        text="First claim text",
        citation_snapshot_ids=[snap_a, snap_b],
        validation_status=ClaimValidationStatus.PENDING,
    )
    # Claim 2 has 1 citation [snap_b]
    claim_2 = DigestClaim(
        id=claim_2_id,
        text="Second claim text",
        citation_snapshot_ids=[snap_b],
        validation_status=ClaimValidationStatus.PENDING,
    )

    draft_digest = Digest(
        id=digest_id,
        digest_date=date(2026, 9, 4),
        status=DigestStatus.DRAFT,
        title="Positional Order Digest",
        claims=[claim_1, claim_2],
    )

    persisted = await repo.persist_digest(draft_digest)
    assert persisted.id == digest_id
    assert persisted.status == DigestStatus.DRAFT

    retrieved = await repo.get_digest_by_id(digest_id)
    assert retrieved is not None
    assert retrieved.id == digest_id
    assert retrieved.digest_date == date(2026, 9, 4)
    assert retrieved.status == DigestStatus.DRAFT
    assert retrieved.title == "Positional Order Digest"

    # Assert exact positional order of claims
    assert len(retrieved.claims) == 2
    assert retrieved.claims[0].id == claim_1_id
    assert retrieved.claims[0].text == "First claim text"
    assert retrieved.claims[0].citation_snapshot_ids == [snap_a, snap_b]
    assert retrieved.claims[1].id == claim_2_id
    assert retrieved.claims[1].text == "Second claim text"
    assert retrieved.claims[1].citation_snapshot_ids == [snap_b]


@pytest.mark.asyncio
async def test_publish_supported_digest_succeeds_and_updates_claims(
    database_session: AsyncSession,
) -> None:
    """publish_digest() validates grounded claims, updates claim validation_status to supported
    before marking digest published, and commits without trigger violation."""
    item_id, snap_id = await _create_snapshot(
        database_session,
        content_text="OpenAI announces GPT-5 release with 99 percent accuracy.",
    )

    repo = PostgresDigestRepository(database_session)

    digest_id = new_id()
    claim_id = new_id()

    claim = DigestClaim(
        id=claim_id,
        text="OpenAI announced GPT-5 with 99 percent accuracy",
        citation_snapshot_ids=[snap_id],
        validation_status=ClaimValidationStatus.PENDING,
    )
    draft_digest = Digest(
        id=digest_id,
        digest_date=date(2026, 9, 4),
        status=DigestStatus.DRAFT,
        title="September 4 Release Digest",
        claims=[claim],
    )

    await repo.persist_digest(draft_digest)

    # Prepare resolver with snapshot content
    snap_doc = DocumentSnapshot(
        id=snap_id,
        source_item_id=item_id,
        fetched_at=BASE_TIME,
        content_hash=f"hash-{snap_id}",
        content_text="OpenAI announces GPT-5 release with 99 percent accuracy.",
    )
    resolver = InMemorySnapshotResolver({snap_id: snap_doc})

    # Publish via repository
    published = await repo.publish_digest(
        digest_id,
        known_snapshot_ids={snap_id},
        snapshot_resolver=resolver,
    )

    assert published.status == DigestStatus.PUBLISHED
    assert len(published.claims) == 1
    assert published.claims[0].validation_status == ClaimValidationStatus.SUPPORTED

    # Verify retrieved from DB
    stored = await repo.get_digest_by_id(digest_id)
    assert stored is not None
    assert stored.status == DigestStatus.PUBLISHED
    assert stored.claims[0].validation_status == ClaimValidationStatus.SUPPORTED


@pytest.mark.asyncio
async def test_publish_unsupported_digest_routes_to_review(
    database_session: AsyncSession,
) -> None:
    """publish_digest() routes ungrounded digests to review without firing publication trigger."""
    item_id, snap_id = await _create_snapshot(
        database_session,
        content_text="Article discussing models without any benchmark figures.",
    )

    repo = PostgresDigestRepository(database_session)

    digest_id = new_id()
    claim_id = new_id()

    # Numeric claim with number 1000 missing from snapshot text
    claim = DigestClaim(
        id=claim_id,
        text="Model achieved 1000 benchmarks",
        citation_snapshot_ids=[snap_id],
        validation_status=ClaimValidationStatus.PENDING,
    )
    draft_digest = Digest(
        id=digest_id,
        digest_date=date(2026, 9, 4),
        status=DigestStatus.DRAFT,
        title="Review Routed Digest",
        claims=[claim],
    )

    await repo.persist_digest(draft_digest)

    snap_doc = DocumentSnapshot(
        id=snap_id,
        source_item_id=item_id,
        fetched_at=BASE_TIME,
        content_hash=f"hash-{snap_id}",
        content_text="Article discussing models without any benchmark figures.",
    )
    resolver = InMemorySnapshotResolver({snap_id: snap_doc})

    routed = await repo.publish_digest(
        digest_id,
        known_snapshot_ids={snap_id},
        snapshot_resolver=resolver,
    )

    assert routed.status == DigestStatus.REVIEW
    assert routed.claims[0].validation_status == ClaimValidationStatus.UNSUPPORTED

    stored = await repo.get_digest_by_id(digest_id)
    assert stored is not None
    assert stored.status == DigestStatus.REVIEW
    assert stored.claims[0].validation_status == ClaimValidationStatus.UNSUPPORTED


@pytest.mark.asyncio
async def test_direct_sql_publication_enforcement_trigger_backstop(
    database_session: AsyncSession,
) -> None:
    """Direct SQL attempt to force status='published' on an unsupported/uncited digest
    is rejected by PostgreSQL trigger trg_enforce_digest_publication."""
    _, snap_id = await _create_snapshot(database_session)

    repo = PostgresDigestRepository(database_session)

    digest_id = new_id()
    claim_id = new_id()

    # Persist a draft digest with a pending claim
    claim = DigestClaim(
        id=claim_id,
        text="Unverified claim",
        citation_snapshot_ids=[snap_id],
        validation_status=ClaimValidationStatus.PENDING,
    )
    draft_digest = Digest(
        id=digest_id,
        digest_date=date(2026, 9, 4),
        status=DigestStatus.DRAFT,
        title="Direct SQL Test Digest",
        claims=[claim],
    )
    await repo.persist_digest(draft_digest)

    # Attempt direct SQL UPDATE to 'published' while claim validation_status is 'pending'
    with pytest.raises(DBAPIError) as exc_info:
        await database_session.execute(
            text("UPDATE digests SET status = 'published' WHERE id = :id"),
            {"id": digest_id},
        )
        await database_session.flush()

    assert "contains 1 unsupported claims" in str(exc_info.value)


@pytest.mark.asyncio
async def test_persist_digest_idempotency_and_modification_guard(
    database_session: AsyncSession,
) -> None:
    """persist_digest() is idempotent on published digests, raises ValueError if differing."""
    item_id, snap_id = await _create_snapshot(database_session, content_text="Grounded content")

    repo = PostgresDigestRepository(database_session)

    digest_id = new_id()
    claim_id = new_id()

    claim = DigestClaim(
        id=claim_id,
        text="Grounded claim",
        citation_snapshot_ids=[snap_id],
        validation_status=ClaimValidationStatus.PENDING,
    )
    draft_digest = Digest(
        id=digest_id,
        digest_date=date(2026, 9, 4),
        status=DigestStatus.DRAFT,
        title="Idempotent Digest",
        claims=[claim],
    )
    await repo.persist_digest(draft_digest)

    # Publish it
    snap_doc = DocumentSnapshot(
        id=snap_id,
        source_item_id=item_id,
        fetched_at=BASE_TIME,
        content_hash=f"hash-{snap_id}",
        content_text="Grounded content",
    )
    published = await repo.publish_digest(
        digest_id,
        known_snapshot_ids={snap_id},
        snapshot_resolver=InMemorySnapshotResolver({snap_id: snap_doc}),
    )
    assert published.status == DigestStatus.PUBLISHED

    # 1. Repeat persist_digest with identical published digest -> safe no-op
    replayed = await repo.persist_digest(published)
    assert replayed.id == digest_id
    assert replayed.status == DigestStatus.PUBLISHED

    # 2. Attempt to modify title of already-published digest -> raises ValueError
    modified = published.model_copy(update={"title": "Different Title"})
    with pytest.raises(ValueError, match="Cannot modify already-published digest"):
        await repo.persist_digest(modified)


@pytest.mark.asyncio
async def test_get_latest_published_digest(
    database_session: AsyncSession,
) -> None:
    """get_latest_published_digest() returns latest published digest by (digest_date, id) DESC."""
    item_id, snap_id = await _create_snapshot(
        database_session, content_text="Content text with Fact 1 and Fact 2"
    )
    snap_doc = DocumentSnapshot(
        id=snap_id,
        source_item_id=item_id,
        fetched_at=BASE_TIME,
        content_hash=f"hash-{snap_id}",
        content_text="Content text with Fact 1 and Fact 2",
    )
    resolver = InMemorySnapshotResolver({snap_id: snap_doc})

    repo = PostgresDigestRepository(database_session)

    d1_id = new_id()
    d2_id = new_id()

    # Digest 1 on Sept 2
    d1 = Digest(
        id=d1_id,
        digest_date=date(2026, 9, 2),
        status=DigestStatus.DRAFT,
        title="Digest Sept 2",
        claims=[DigestClaim(id=new_id(), text="Fact 1", citation_snapshot_ids=[snap_id])],
    )
    await repo.persist_digest(d1)
    await repo.publish_digest(d1_id, known_snapshot_ids={snap_id}, snapshot_resolver=resolver)

    # Digest 2 on Sept 3 (more recent)
    d2 = Digest(
        id=d2_id,
        digest_date=date(2026, 9, 3),
        status=DigestStatus.DRAFT,
        title="Digest Sept 3",
        claims=[DigestClaim(id=new_id(), text="Fact 2", citation_snapshot_ids=[snap_id])],
    )
    await repo.persist_digest(d2)
    await repo.publish_digest(d2_id, known_snapshot_ids={snap_id}, snapshot_resolver=resolver)

    latest = await repo.get_latest_published_digest()
    assert latest is not None
    assert latest.id == d2_id
    assert latest.digest_date == date(2026, 9, 3)


@pytest.mark.asyncio
async def test_persist_digest_rejects_publishing_existing_draft_directly(
    database_session: AsyncSession,
) -> None:
    """persist_digest() raises ValueError when called with status=PUBLISHED on an existing draft."""
    _, snap_id = await _create_snapshot(database_session)
    repo = PostgresDigestRepository(database_session)

    digest_id = new_id()
    draft_digest = Digest(
        id=digest_id,
        digest_date=date(2026, 9, 4),
        status=DigestStatus.DRAFT,
        title="Draft Digest",
        claims=[
            DigestClaim(
                id=new_id(),
                text="Pending claim",
                citation_snapshot_ids=[snap_id],
                validation_status=ClaimValidationStatus.PENDING,
            )
        ],
    )
    await repo.persist_digest(draft_digest)

    attempted_published = draft_digest.model_copy(update={"status": DigestStatus.PUBLISHED})
    with pytest.raises(ValueError, match=r"Cannot publish existing digest .* via persist_digest"):
        await repo.persist_digest(attempted_published)


@pytest.mark.asyncio
async def test_persist_digest_rejects_brand_new_digest_with_published_status(
    database_session: AsyncSession,
) -> None:
    """persist_digest() raises ValueError when called with status=PUBLISHED for a new digest."""
    repo = PostgresDigestRepository(database_session)
    new_published = Digest(
        id=new_id(),
        digest_date=date(2026, 9, 4),
        status=DigestStatus.PUBLISHED,
        title="Direct Published Digest",
        claims=[],
    )
    with pytest.raises(
        ValueError,
        match=(
            r"Cannot persist a new digest with status='published' via "
            r"persist_digest\(\); use publish_digest\(\)"
        ),
    ):
        await repo.persist_digest(new_published)


@pytest.mark.asyncio
async def test_persist_digest_rejects_modifying_immutable_digest_date_on_existing_draft(
    database_session: AsyncSession,
) -> None:
    """persist_digest() raises ValueError when attempting to change digest_date on draft."""
    _, snap_id = await _create_snapshot(database_session)
    repo = PostgresDigestRepository(database_session)

    digest_id = new_id()
    draft_digest = Digest(
        id=digest_id,
        digest_date=date(2026, 9, 4),
        status=DigestStatus.DRAFT,
        title="Draft Digest",
        claims=[
            DigestClaim(
                id=new_id(),
                text="Pending claim",
                citation_snapshot_ids=[snap_id],
                validation_status=ClaimValidationStatus.PENDING,
            )
        ],
    )
    await repo.persist_digest(draft_digest)

    modified_date_digest = draft_digest.model_copy(update={"digest_date": date(2026, 9, 5)})
    expected_msg = (
        f"Cannot modify immutable digest_date for digest {digest_id}: 2026-09-04 != 2026-09-05"
    )
    with pytest.raises(ValueError, match=expected_msg):
        await repo.persist_digest(modified_date_digest)


@pytest.mark.asyncio
async def test_publish_digest_row_lock_blocks_concurrent_claim_mutation(
    open_database_session: _OpenSession,
) -> None:
    """Acquiring row lock in publish_digest() blocks concurrent child mutation until commit.

    Demonstrates ADR 0011 §5.1:
    The publication transaction acquires an exclusive row lock on the parent digests
    row (FOR UPDATE) before running validation checks. Concurrent child mutations
    acquiring FOR KEY SHARE block until publish_digest()'s transaction completes.
    Once completed, child insertion attempts against the published digest are
    rejected by the storage-level immutability trigger.
    """
    digest_id = new_id()
    claim_id = new_id()
    digest_date = date(2026, 9, 14)

    # 1. Setup draft digest in a committed transaction
    async with open_database_session() as setup_session:
        item_id, snap_id = await _create_snapshot(
            setup_session, content_text="Revenue increased by 25 percent"
        )
        snap_doc = DocumentSnapshot(
            id=snap_id,
            source_item_id=item_id,
            fetched_at=BASE_TIME,
            content_hash=f"hash-{snap_id}",
            content_text="Revenue increased by 25 percent",
        )
        repo = PostgresDigestRepository(setup_session)
        draft = Digest(
            id=digest_id,
            digest_date=digest_date,
            status=DigestStatus.DRAFT,
            title="Concurrency Draft Digest",
            claims=[
                DigestClaim(
                    id=claim_id,
                    text="Revenue increased by 25 percent",
                    citation_snapshot_ids=[snap_id],
                    validation_status=ClaimValidationStatus.PENDING,
                )
            ],
        )
        await repo.persist_digest(draft)
        await setup_session.commit()

    resolver = InMemorySnapshotResolver({snap_id: snap_doc})

    # 2. Concurrency synchronization
    started_publish = asyncio.Event()
    mutation_unblocked = asyncio.Event()
    mutation_was_blocked = False

    async def run_publisher() -> None:
        nonlocal mutation_was_blocked
        async with open_database_session() as session_pub:
            repo_pub = PostgresDigestRepository(session_pub)
            # publish_digest acquires SELECT ... FOR UPDATE on digest_id
            published = await repo_pub.publish_digest(
                digest_id,
                known_snapshot_ids={snap_id},
                snapshot_resolver=resolver,
            )
            assert published.status == DigestStatus.PUBLISHED
            started_publish.set()

            # Hold transaction open briefly so concurrent worker attempts lock and must wait
            await asyncio.sleep(0.15)
            # Verify mutation has not unblocked yet because session_pub has not committed
            assert not mutation_unblocked.is_set()
            mutation_was_blocked = True
            await session_pub.commit()

    async def run_mutator() -> None:
        await started_publish.wait()
        async with open_database_session() as session_mut:
            # ADR 0011 §5.1 child mutation path: SELECT id FROM digests WHERE id = :id FOR KEY SHARE
            # This conflicts with FOR UPDATE and blocks until session_pub commits.
            await session_mut.execute(
                text("SELECT id FROM digests WHERE id = :id FOR KEY SHARE"),
                {"id": digest_id},
            )
            mutation_unblocked.set()

            # Once unblocked, verify the digest is now published and attempts to mutate child claims
            # are rejected by storage enforcement (trg_protect_published_digest_claims_insert)
            with pytest.raises(
                DBAPIError, match="Cannot insert claims into an already published digest"
            ):
                await session_mut.execute(
                    text("""
                        INSERT INTO digest_claims (id, digest_id, position, text, validation_status, created_at)
                        VALUES (:id, :d_id, 1, 'Concurrent mutation after publish', 'supported', :now)
                    """),
                    {"id": new_id(), "d_id": digest_id, "now": BASE_TIME},
                )

    await asyncio.gather(run_publisher(), run_mutator())
    assert mutation_was_blocked is True
