"""Unit tests for DigestRepository validation gate delegation and routing — ADR 0011."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from ai_daily_digest.intelligence.db.models import DigestClaimModel, DigestModel
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


def _create_test_snapshot(snap_id: uuid.UUID, text: str) -> DocumentSnapshot:
    return DocumentSnapshot(
        id=snap_id,
        source_item_id=new_id(),
        fetched_at=datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC),
        content_hash=f"hash-{snap_id}",
        content_text=text,
    )


def test_postgres_fact_store_satisfies_digest_repository_protocol() -> None:
    """PostgresFactStore and PostgresDigestRepository alias satisfy DigestRepository protocol."""
    mock_session = AsyncMock()
    store = PostgresFactStore(mock_session)
    assert isinstance(store, DigestRepository)
    assert PostgresDigestRepository is PostgresFactStore


@pytest.mark.asyncio
async def test_publish_digest_raises_value_error_when_digest_not_found() -> None:
    """publish_digest() raises ValueError when digest_id does not exist in the database."""
    mock_session = AsyncMock()
    repo = PostgresFactStore(mock_session)

    # Mock get_digest_by_id to return None
    repo.get_digest_by_id = AsyncMock(return_value=None)  # type: ignore[method-assign]

    missing_id = new_id()
    resolver = InMemorySnapshotResolver()

    with pytest.raises(ValueError, match=f"Digest with id {missing_id} not found"):
        await repo.publish_digest(
            missing_id,
            known_snapshot_ids=set(),
            snapshot_resolver=resolver,
        )


@pytest.mark.asyncio
async def test_publish_digest_supported_claims_routes_to_published() -> None:
    """publish_digest() delegates to validate.py::publish_digest(), updates claims to supported,
    and updates digest status to published with explicit flush ordering."""
    mock_session = AsyncMock()
    repo = PostgresFactStore(mock_session)

    digest_id = new_id()
    claim_id = new_id()
    snap_id = new_id()

    # Grounded claim with 42 matching snapshot content
    claim = DigestClaim(
        id=claim_id,
        text="Revenue increased by 42 percent",
        citation_snapshot_ids=[snap_id],
        validation_status=ClaimValidationStatus.PENDING,
    )
    draft_digest = Digest(
        id=digest_id,
        digest_date=date(2026, 9, 4),
        status=DigestStatus.DRAFT,
        title="Valid Daily Digest",
        claims=[claim],
    )

    repo.get_digest_by_id = AsyncMock(return_value=draft_digest)  # type: ignore[method-assign]

    # Mock DB claim and digest models
    c_model = DigestClaimModel(
        id=claim_id,
        digest_id=digest_id,
        position=0,
        text=claim.text,
        validation_status="pending",
        created_at=datetime.now(UTC),
    )
    d_model = DigestModel(
        id=digest_id,
        digest_date=draft_digest.digest_date,
        status="draft",
        title=draft_digest.title,
        created_at=datetime.now(UTC),
    )

    claims_res = MagicMock()
    claims_res.scalars.return_value.all.return_value = [c_model]
    mock_session.execute.return_value = claims_res
    mock_session.get.return_value = d_model

    resolver = InMemorySnapshotResolver(
        {snap_id: _create_test_snapshot(snap_id, "Revenue up 42 percent")}
    )

    result = await repo.publish_digest(
        digest_id,
        known_snapshot_ids={snap_id},
        snapshot_resolver=resolver,
    )

    # Assert validation and routing
    assert result.status == DigestStatus.PUBLISHED
    assert len(result.claims) == 1
    assert result.claims[0].validation_status == ClaimValidationStatus.SUPPORTED

    # Verify claim updated before digest status and flushed
    assert c_model.validation_status == "supported"
    assert d_model.status == "published"
    assert mock_session.flush.call_count >= 2


@pytest.mark.asyncio
async def test_publish_digest_unsupported_claims_routes_to_review() -> None:
    """publish_digest() routes to review when claims cite unknown snapshots or are ungrounded."""
    mock_session = AsyncMock()
    repo = PostgresFactStore(mock_session)

    digest_id = new_id()
    claim_id = new_id()
    unknown_snap_id = new_id()

    claim = DigestClaim(
        id=claim_id,
        text="Ungrounded claim with unresolvable citation",
        citation_snapshot_ids=[unknown_snap_id],
        validation_status=ClaimValidationStatus.PENDING,
    )
    draft_digest = Digest(
        id=digest_id,
        digest_date=date(2026, 9, 4),
        status=DigestStatus.DRAFT,
        title="Unverified Digest",
        claims=[claim],
    )

    repo.get_digest_by_id = AsyncMock(return_value=draft_digest)  # type: ignore[method-assign]

    c_model = DigestClaimModel(
        id=claim_id,
        digest_id=digest_id,
        position=0,
        text=claim.text,
        validation_status="pending",
        created_at=datetime.now(UTC),
    )
    d_model = DigestModel(
        id=digest_id,
        digest_date=draft_digest.digest_date,
        status="draft",
        title=draft_digest.title,
        created_at=datetime.now(UTC),
    )

    claims_res = MagicMock()
    claims_res.scalars.return_value.all.return_value = [c_model]
    mock_session.execute.return_value = claims_res
    mock_session.get.return_value = d_model

    resolver = InMemorySnapshotResolver()  # Resolver does not know unknown_snap_id

    result = await repo.publish_digest(
        digest_id,
        known_snapshot_ids=set(),  # unknown_snap_id not known
        snapshot_resolver=resolver,
    )

    # Assert validation gate routed to review
    assert result.status == DigestStatus.REVIEW
    assert result.claims[0].validation_status == ClaimValidationStatus.UNSUPPORTED
    assert c_model.validation_status == "unsupported"
    assert d_model.status == "review"
    assert mock_session.flush.call_count >= 2


@pytest.mark.asyncio
async def test_persist_digest_idempotent_on_published_digest() -> None:
    """persist_digest() returns existing digest if identical, raises ValueError if differing."""
    mock_session = AsyncMock()
    repo = PostgresFactStore(mock_session)

    digest_id = new_id()
    claim_id = new_id()
    snap_id = new_id()

    published_digest = Digest(
        id=digest_id,
        digest_date=date(2026, 9, 4),
        status=DigestStatus.PUBLISHED,
        title="Published Digest",
        claims=[
            DigestClaim(
                id=claim_id,
                text="Confirmed fact",
                citation_snapshot_ids=[snap_id],
                validation_status=ClaimValidationStatus.SUPPORTED,
            )
        ],
    )

    # 1. Identical replay: returns existing
    repo.get_digest_by_id = AsyncMock(return_value=published_digest)  # type: ignore[method-assign]
    replayed = await repo.persist_digest(published_digest)
    assert replayed == published_digest
    assert mock_session.add.call_count == 0

    # 2. Differing attributes: raises ValueError
    modified_digest = published_digest.model_copy(update={"title": "Changed Title"})
    with pytest.raises(ValueError, match="Cannot modify already-published digest"):
        await repo.persist_digest(modified_digest)


@pytest.mark.asyncio
async def test_persist_digest_rejects_publishing_existing_draft_directly() -> None:
    """persist_digest() raises ValueError when called with status=PUBLISHED on an existing draft."""
    mock_session = AsyncMock()
    repo = PostgresFactStore(mock_session)

    digest_id = new_id()
    existing_draft = Digest(
        id=digest_id,
        digest_date=date(2026, 9, 4),
        status=DigestStatus.DRAFT,
        title="Draft Digest",
        claims=[],
    )
    repo.get_digest_by_id = AsyncMock(return_value=existing_draft)  # type: ignore[method-assign]

    attempted_published = existing_draft.model_copy(update={"status": DigestStatus.PUBLISHED})
    with pytest.raises(ValueError, match=r"Cannot publish existing digest .* via persist_digest"):
        await repo.persist_digest(attempted_published)
