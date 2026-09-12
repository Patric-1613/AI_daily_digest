"""Integration tests for shared feed repositories — ADR 0008 & ADR 0011."""
# pylint: disable=too-many-locals,too-many-statements

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ai_daily_digest.ingestion.db.models import DocumentSnapshotRow, SourceItemRow
from ai_daily_digest.intelligence.db.models import (
    ChangeModel,
    ChangeSetModel,
    DigestClaimCitationModel,
    DigestClaimModel,
    DigestModel,
)
from ai_daily_digest.intelligence.db.repository import (
    PostgresChangeFeedRepository,
    PostgresDigestFeedRepository,
    PostgresFactStore,
)
from ai_daily_digest.shared.ids import new_id
from ai_daily_digest.shared.repositories import ChangeFeedFilter, DigestFeedFilter
from ai_daily_digest.shared.schemas import ClaimValidationStatus, DigestStatus, Subject

pytestmark = pytest.mark.integration

BASE_TIME = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)


async def _create_snapshot(
    session: AsyncSession,
    *,
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
        content_text="Sample snapshot text",
    )
    session.add(snap)
    await session.flush()
    return item_id, snap_id


async def _create_published_digest(
    session: AsyncSession,
    *,
    digest_id: uuid.UUID,
    digest_date: date,
    title: str,
    snapshot_id: uuid.UUID,
) -> DigestModel:
    """Helper to seed a published digest with valid claim and citation to satisfy DB trigger."""
    digest = DigestModel(
        id=digest_id,
        digest_date=digest_date,
        status=DigestStatus.DRAFT.value,
        title=title,
        created_at=BASE_TIME,
    )
    session.add(digest)
    await session.flush()

    c_id = new_id()
    claim = DigestClaimModel(
        id=c_id,
        digest_id=digest_id,
        position=0,
        text=f"Claim for {title}",
        validation_status=ClaimValidationStatus.SUPPORTED.value,
        created_at=BASE_TIME,
    )
    session.add(claim)
    await session.flush()

    citation = DigestClaimCitationModel(
        claim_id=c_id,
        snapshot_id=snapshot_id,
        position=0,
        created_at=BASE_TIME,
    )
    session.add(citation)
    await session.flush()

    digest.status = DigestStatus.PUBLISHED.value
    await session.flush()
    return digest


@pytest.mark.asyncio
async def test_list_changes_ordering_and_keyset_pagination(
    database_session: AsyncSession,
) -> None:
    """Test ChangeFeedRepository ordering (detected_at DESC, id DESC) and limit+1 keyset pagination."""
    prev_time = BASE_TIME - timedelta(days=1)
    _, prev_snap_id = await _create_snapshot(database_session, fetched_at=prev_time)

    repo = PostgresChangeFeedRepository(database_session)
    subj_model = await repo.ensure_subject(Subject(company="OpenAI", product="GPT-4o"))
    ck, pk = subj_model.company_key, subj_model.product_key

    cs_id = new_id()
    cs = ChangeSetModel(
        id=cs_id,
        company_key=ck,
        product_key=pk,
        review_status="pending",
        created_at=BASE_TIME,
    )
    database_session.add(cs)
    await database_session.flush()

    # Seed 4 changes with distinct detected_at times and matching snapshot fetched_at
    times = [BASE_TIME + timedelta(hours=i) for i in range(4)]
    change_ids = [new_id() for _ in range(4)]

    for pos, (cid, dt) in enumerate(zip(change_ids, times, strict=True)):
        _, curr_snap_id = await _create_snapshot(database_session, fetched_at=dt)
        ch = ChangeModel(
            id=cid,
            detected_at=dt,
            change_set_id=cs_id,
            position=pos,
            company_key=ck,
            product_key=pk,
            field="context_window_tokens",
            change_type="increased",
            confidence=0.95,
            review_status="pending",
            previous_value="128000",
            previous_observed_at=prev_time,
            previous_snapshot_id=prev_snap_id,
            current_value=f"{(pos + 1) * 100000}",
            current_observed_at=dt,
            current_snapshot_id=curr_snap_id,
            created_at=dt,
        )
        database_session.add(ch)
    await database_session.flush()

    # Page 1: limit = 2 -> returns 3 rows (limit + 1) in descending detected_at order
    page1 = await repo.list_changes(limit=2)
    assert len(page1) == 3
    assert page1[0].id == change_ids[3]  # newest (times[3])
    assert page1[1].id == change_ids[2]  # times[2]
    assert page1[2].id == change_ids[1]  # times[1]

    # Verify domain mapping
    assert page1[0].subject.company == "OpenAI"
    assert page1[0].subject.product == "GPT-4o"
    assert page1[0].field == "context_window_tokens"
    assert page1[0].change_type == "increased"
    assert page1[0].previous is not None and page1[0].previous.value == "128000"
    assert page1[0].current.value == "400000"

    # Page 2: resume from item at index 1 (times[2])
    cursor_tuple = (page1[1].detected_at, page1[1].id)
    page2 = await repo.list_changes(after=cursor_tuple, limit=2)
    assert len(page2) == 2  # exactly times[1] and times[0]
    assert page2[0].id == change_ids[1]
    assert page2[1].id == change_ids[0]


@pytest.mark.asyncio
async def test_list_changes_filtering(database_session: AsyncSession) -> None:
    """Test ChangeFeedRepository filters by company_key, product_key, and field."""
    _, prev_snap_id = await _create_snapshot(database_session, fetched_at=BASE_TIME)

    repo = PostgresFactStore(database_session)
    subj1 = await repo.ensure_subject(Subject(company="OpenAI", product="GPT-4o"))
    subj2 = await repo.ensure_subject(Subject(company="Anthropic", product="Claude"))

    cs1_id, cs2_id = new_id(), new_id()
    cs1 = ChangeSetModel(
        id=cs1_id,
        company_key=subj1.company_key,
        product_key=subj1.product_key,
        review_status="pending",
        created_at=BASE_TIME,
    )
    cs2 = ChangeSetModel(
        id=cs2_id,
        company_key=subj2.company_key,
        product_key=subj2.product_key,
        review_status="pending",
        created_at=BASE_TIME,
    )
    database_session.add_all([cs1, cs2])
    await database_session.flush()

    t1 = BASE_TIME + timedelta(hours=1)
    _, snap1_id = await _create_snapshot(database_session, fetched_at=t1)
    # Change 1: OpenAI context_window_tokens
    ch1 = ChangeModel(
        id=new_id(),
        detected_at=t1,
        change_set_id=cs1_id,
        position=0,
        company_key=subj1.company_key,
        product_key=subj1.product_key,
        field="context_window_tokens",
        change_type="increased",
        confidence=0.95,
        review_status="pending",
        previous_value="128000",
        previous_observed_at=BASE_TIME,
        previous_snapshot_id=prev_snap_id,
        current_value="256000",
        current_observed_at=t1,
        current_snapshot_id=snap1_id,
        created_at=t1,
    )
    t2 = BASE_TIME + timedelta(hours=2)
    _, snap2_id = await _create_snapshot(database_session, fetched_at=t2)
    # Change 2: OpenAI input_price_usd
    ch2 = ChangeModel(
        id=new_id(),
        detected_at=t2,
        change_set_id=cs1_id,
        position=1,
        company_key=subj1.company_key,
        product_key=subj1.product_key,
        field="input_price_usd",
        change_type="decreased",
        confidence=0.95,
        review_status="pending",
        previous_value="5.00",
        previous_observed_at=BASE_TIME,
        previous_snapshot_id=prev_snap_id,
        current_value="2.50",
        current_observed_at=t2,
        current_snapshot_id=snap2_id,
        created_at=t2,
    )
    t3 = BASE_TIME + timedelta(hours=3)
    _, snap3_id = await _create_snapshot(database_session, fetched_at=t3)
    # Change 3: Anthropic context_window_tokens
    ch3 = ChangeModel(
        id=new_id(),
        detected_at=t3,
        change_set_id=cs2_id,
        position=0,
        company_key=subj2.company_key,
        product_key=subj2.product_key,
        field="context_window_tokens",
        change_type="increased",
        confidence=0.95,
        review_status="pending",
        previous_value="100000",
        previous_observed_at=BASE_TIME,
        previous_snapshot_id=prev_snap_id,
        current_value="200000",
        current_observed_at=t3,
        current_snapshot_id=snap3_id,
        created_at=t3,
    )
    database_session.add_all([ch1, ch2, ch3])
    await database_session.flush()

    # Filter by company_key="openai"
    openai_changes = await repo.list_changes(
        feed_filter=ChangeFeedFilter(company_key=subj1.company_key)
    )
    assert len(openai_changes) == 2
    assert {c.field for c in openai_changes} == {"context_window_tokens", "input_price_usd"}

    # Filter by field="input_price_usd"
    price_changes = await repo.list_changes(feed_filter=ChangeFeedFilter(field="input_price_usd"))
    assert len(price_changes) == 1
    assert price_changes[0].id == ch2.id

    # ADR 0008 half-open [from, to) range boundary tests for detected_from / detected_to:
    # 1. Range [t1, t3): exact lower bound t1 is INCLUDED, exact upper bound t3 is EXCLUDED
    range_changes = await repo.list_changes(
        feed_filter=ChangeFeedFilter(detected_from=t1, detected_to=t3)
    )
    assert len(range_changes) == 2
    assert {c.id for c in range_changes} == {ch1.id, ch2.id}

    # 2. One-sided lower bound: detected_from=t2 -> [t2, infinity)
    from_changes = await repo.list_changes(feed_filter=ChangeFeedFilter(detected_from=t2))
    assert len(from_changes) == 2
    assert {c.id for c in from_changes} == {ch2.id, ch3.id}

    # 3. One-sided upper bound: detected_to=t2 -> (-infinity, t2)
    to_changes = await repo.list_changes(feed_filter=ChangeFeedFilter(detected_to=t2))
    assert len(to_changes) == 1
    assert to_changes[0].id == ch1.id


@pytest.mark.asyncio
async def test_list_digests_only_published_and_pagination(
    database_session: AsyncSession,
) -> None:
    """Test DigestFeedRepository returns only published digests and handles keyset pagination."""
    _, snap_id = await _create_snapshot(database_session)

    # Seed 3 published digests with valid claims/citations
    d_pub1_id, d_pub2_id, d_pub3_id = new_id(), new_id(), new_id()
    await _create_published_digest(
        database_session,
        digest_id=d_pub1_id,
        digest_date=date(2026, 9, 3),
        title="Digest Sept 3",
        snapshot_id=snap_id,
    )
    await _create_published_digest(
        database_session,
        digest_id=d_pub2_id,
        digest_date=date(2026, 9, 2),
        title="Digest Sept 2",
        snapshot_id=snap_id,
    )
    await _create_published_digest(
        database_session,
        digest_id=d_pub3_id,
        digest_date=date(2026, 9, 1),
        title="Digest Sept 1",
        snapshot_id=snap_id,
    )

    # Seed 1 draft digest and 1 review digest
    d_draft_id, d_rev_id = new_id(), new_id()
    draft = DigestModel(
        id=d_draft_id,
        digest_date=date(2026, 9, 4),
        status=DigestStatus.DRAFT.value,
        title="Draft Digest",
        created_at=BASE_TIME,
    )
    review = DigestModel(
        id=d_rev_id,
        digest_date=date(2026, 9, 5),
        status=DigestStatus.REVIEW.value,
        title="Review Digest",
        created_at=BASE_TIME,
    )
    database_session.add_all([draft, review])
    await database_session.flush()

    repo = PostgresDigestFeedRepository(database_session)

    # Query without filter: draft and review must be omitted
    page1 = await repo.list_digests(limit=2)
    assert len(page1) == 3  # limit + 1
    assert page1[0].id == d_pub1_id  # 2026-09-03
    assert page1[1].id == d_pub2_id  # 2026-09-02
    assert page1[2].id == d_pub3_id  # 2026-09-01

    # Verify claim & citation mapping
    assert page1[0].status == DigestStatus.PUBLISHED
    assert len(page1[0].claims) == 1
    assert page1[0].claims[0].text == "Claim for Digest Sept 3"
    assert page1[0].claims[0].validation_status == ClaimValidationStatus.SUPPORTED
    assert page1[0].claims[0].citation_snapshot_ids == [snap_id]

    # Page 2: resume after Sept 2
    cursor_tuple = (page1[1].digest_date, page1[1].id)
    page2 = await repo.list_digests(after=cursor_tuple, limit=2)
    assert len(page2) == 1
    assert page2[0].id == d_pub3_id
    assert page2[0].digest_date == date(2026, 9, 1)


@pytest.mark.asyncio
async def test_list_digests_date_filtering(database_session: AsyncSession) -> None:
    """Test DigestFeedRepository filters by date_from and date_to (half-open [from, to))."""
    _, snap_id = await _create_snapshot(database_session)
    d1_id, d2_id, d3_id = new_id(), new_id(), new_id()

    await _create_published_digest(
        database_session,
        digest_id=d1_id,
        digest_date=date(2026, 9, 10),
        title="Digest Sept 10",
        snapshot_id=snap_id,
    )
    await _create_published_digest(
        database_session,
        digest_id=d2_id,
        digest_date=date(2026, 9, 11),
        title="Digest Sept 11",
        snapshot_id=snap_id,
    )
    await _create_published_digest(
        database_session,
        digest_id=d3_id,
        digest_date=date(2026, 9, 12),
        title="Digest Sept 12",
        snapshot_id=snap_id,
    )

    repo = PostgresDigestFeedRepository(database_session)

    # 1. Range [2026-09-10, 2026-09-12): lower bound 9-10 is INCLUDED, upper bound 9-12 is EXCLUDED
    filtered_range = await repo.list_digests(
        feed_filter=DigestFeedFilter(date_from=date(2026, 9, 10), date_to=date(2026, 9, 12))
    )
    assert len(filtered_range) == 2
    assert [d.id for d in filtered_range] == [d2_id, d1_id]

    # 2. One-sided lower bound: date_from=2026-09-11 -> [2026-09-11, infinity)
    from_digests = await repo.list_digests(
        feed_filter=DigestFeedFilter(date_from=date(2026, 9, 11))
    )
    assert len(from_digests) == 2
    assert [d.id for d in from_digests] == [d3_id, d2_id]

    # 3. One-sided upper bound: date_to=2026-09-11 -> (-infinity, 2026-09-11)
    to_digests = await repo.list_digests(feed_filter=DigestFeedFilter(date_to=date(2026, 9, 11)))
    assert len(to_digests) == 1
    assert to_digests[0].id == d1_id


@pytest.mark.asyncio
async def test_get_published_digest_database_behavior(
    database_session: AsyncSession,
) -> None:
    """Test get_published_digest returns published digest with claims and citations, and None for draft/review/missing."""
    _, snap_id = await _create_snapshot(database_session)

    # 1. Published digest with claims and citations
    pub_id = new_id()
    await _create_published_digest(
        database_session,
        digest_id=pub_id,
        digest_date=date(2026, 9, 12),
        title="Published Digest",
        snapshot_id=snap_id,
    )

    # 2. Draft digest
    draft_id = new_id()
    draft = DigestModel(
        id=draft_id,
        digest_date=date(2026, 9, 12),
        status=DigestStatus.DRAFT.value,
        title="Draft Digest",
        created_at=BASE_TIME,
    )

    # 3. Review digest
    review_id = new_id()
    review = DigestModel(
        id=review_id,
        digest_date=date(2026, 9, 12),
        status=DigestStatus.REVIEW.value,
        title="Review Digest",
        created_at=BASE_TIME,
    )
    database_session.add_all([draft, review])
    await database_session.flush()

    repo = PostgresDigestFeedRepository(database_session)

    # Published digest is retrieved with claims and citations
    retrieved = await repo.get_published_digest(pub_id)
    assert retrieved is not None
    assert retrieved.id == pub_id
    assert retrieved.status == DigestStatus.PUBLISHED
    assert retrieved.title == "Published Digest"
    assert len(retrieved.claims) == 1
    assert retrieved.claims[0].text == "Claim for Published Digest"
    assert retrieved.claims[0].citation_snapshot_ids == [snap_id]
    assert retrieved.claims[0].validation_status == ClaimValidationStatus.SUPPORTED

    # Draft digest returns None
    assert await repo.get_published_digest(draft_id) is None

    # Review digest returns None
    assert await repo.get_published_digest(review_id) is None

    # Non-existent digest returns None
    assert await repo.get_published_digest(new_id()) is None
