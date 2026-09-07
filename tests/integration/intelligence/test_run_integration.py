"""Integration tests for the persistent daily intelligence runner against PostgreSQL."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_daily_digest.ingestion.db.models import DocumentSnapshotRow, SourceItemRow
from ai_daily_digest.intelligence.db.models import DigestModel, ExtractedFactModel
from ai_daily_digest.intelligence.extract_facts import FactCandidate, FactExtractionResponse
from ai_daily_digest.intelligence.run import (
    main,
    run_pipeline,
    select_snapshots_in_window,
)
from ai_daily_digest.shared.ids import new_id

pytestmark = pytest.mark.integration

_OpenSession = Callable[[], AbstractAsyncContextManager[AsyncSession]]


async def _create_item_and_snapshot(
    session: AsyncSession,
    *,
    publisher: str = "OpenAI",
    title: str = "OpenAI GPT-4o Update",
    content_text: str = "OpenAI introduces GPT-4o with 128000 context window.",
    fetched_at: datetime,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a SourceItemRow and DocumentSnapshotRow pair."""
    item_id = new_id()
    snap_id = new_id()
    item = SourceItemRow(
        id=item_id,
        dedupe_key=f"sha256:{item_id}",
        source_id="openai_news",
        publisher=publisher,
        title=title,
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
async def test_window_selection_boundaries(
    database_session: AsyncSession,
) -> None:
    """Window selection correctly queries [window_start, window_end) including boundary cases."""
    window_start = datetime(2026, 9, 7, 0, 0, 0, tzinfo=UTC)
    window_end = datetime(2026, 9, 8, 0, 0, 0, tzinfo=UTC)

    # 1. strictly before window_start: excluded
    await _create_item_and_snapshot(
        database_session,
        fetched_at=window_start - timedelta(seconds=1),
    )
    # 2. exactly at window_start: included (boundary check)
    _, snap_start = await _create_item_and_snapshot(
        database_session,
        fetched_at=window_start,
    )
    # 3. inside window: included
    _, snap_mid = await _create_item_and_snapshot(
        database_session,
        fetched_at=window_start + timedelta(hours=12),
    )
    # 4. exactly at window_end: excluded (boundary check)
    await _create_item_and_snapshot(
        database_session,
        fetched_at=window_end,
    )
    # 5. strictly after window_end: excluded
    await _create_item_and_snapshot(
        database_session,
        fetched_at=window_end + timedelta(seconds=1),
    )

    selected = await select_snapshots_in_window(
        database_session,
        window_start=window_start,
        window_end=window_end,
    )

    selected_ids = [snap_row.id for _, snap_row in selected]
    assert snap_start in selected_ids
    assert snap_mid in selected_ids
    assert len(selected_ids) == 2
    # Verify chronological ordering
    assert selected_ids == [snap_start, snap_mid]


@pytest.mark.asyncio
async def test_idempotent_reprocessing(
    open_database_session: _OpenSession,
) -> None:
    """Reprocessing an already-seen snapshot in the window is a safe no-op."""
    window_start = datetime(2026, 9, 7, 0, 0, 0, tzinfo=UTC)
    window_end = datetime(2026, 9, 8, 0, 0, 0, tzinfo=UTC)
    digest_date = date(2026, 9, 7)

    async with open_database_session() as session:
        _, snap_id = await _create_item_and_snapshot(
            session,
            title="OpenAI GPT-4o Release",
            content_text="OpenAI introduces GPT-4o with 128000 context window.",
            fetched_at=window_start + timedelta(hours=2),
        )
        await session.commit()

    def mock_extract(system: str, prompt: str) -> FactExtractionResponse:
        del system, prompt
        return FactExtractionResponse(
            facts=[
                FactCandidate(
                    field="context_window_tokens",
                    value="128000",
                    quoted_span="128000 context window",
                    confidence=0.95,
                )
            ]
        )

    # First run over the window
    report1 = await run_pipeline(
        session_factory=open_database_session,
        digest_date=digest_date,
        window_start=window_start,
        window_end=window_end,
        extract_call_fn=mock_extract,
    )

    assert report1.selected_snapshot_count == 1
    assert report1.processed_snapshot_count == 1
    assert report1.failed_snapshot_count == 0

    # Count rows after first run
    async with open_database_session() as session:
        facts_count1 = (
            await session.execute(
                select(ExtractedFactModel).where(ExtractedFactModel.snapshot_id == snap_id)
            )
        ).all()
        assert len(facts_count1) == 1

    # Second run over the exact same window (replay)
    report2 = await run_pipeline(
        session_factory=open_database_session,
        digest_date=digest_date,
        window_start=window_start,
        window_end=window_end,
        extract_call_fn=mock_extract,
    )

    # Verify idempotency: safe no-op, no duplicates recorded
    assert report2.selected_snapshot_count == 1
    assert report2.processed_snapshot_count == 1
    assert report2.failed_snapshot_count == 0
    assert report2.extracted_change_count == 0

    async with open_database_session() as session:
        facts_count2 = (
            await session.execute(
                select(ExtractedFactModel).where(ExtractedFactModel.snapshot_id == snap_id)
            )
        ).all()
        # Row count unchanged
        assert len(facts_count2) == 1


@pytest.mark.asyncio
async def test_per_item_transaction_isolation(
    open_database_session: _OpenSession,
) -> None:
    """One item's failure does not roll back or abort other items in the batch."""
    window_start = datetime(2026, 9, 7, 0, 0, 0, tzinfo=UTC)
    window_end = datetime(2026, 9, 8, 0, 0, 0, tzinfo=UTC)
    digest_date = date(2026, 9, 7)

    async with open_database_session() as session:
        # Item 1: Valid item that will succeed
        _, snap1_id = await _create_item_and_snapshot(
            session,
            title="OpenAI GPT-4o Valid Item",
            content_text="OpenAI introduces GPT-4o with 128000 context window.",
            fetched_at=window_start + timedelta(hours=1),
        )
        # Item 2: Item that will raise an error during extraction
        _, snap2_id = await _create_item_and_snapshot(
            session,
            title="OpenAI GPT-4o Failing Item",
            content_text="OpenAI introduces GPT-4o with 256000 context window.",
            fetched_at=window_start + timedelta(hours=2),
        )
        await session.commit()

    def mock_extract_failing_second(system: str, prompt: str) -> FactExtractionResponse:
        del system
        if "256000" in prompt:
            raise RuntimeError("Simulated transient extraction failure")
        return FactExtractionResponse(
            facts=[
                FactCandidate(
                    field="context_window_tokens",
                    value="128000",
                    quoted_span="128000 context window",
                    confidence=0.95,
                )
            ]
        )

    report = await run_pipeline(
        session_factory=open_database_session,
        digest_date=digest_date,
        window_start=window_start,
        window_end=window_end,
        extract_call_fn=mock_extract_failing_second,
    )

    assert report.selected_snapshot_count == 2
    assert report.processed_snapshot_count == 1
    assert report.failed_snapshot_count == 1
    assert len(report.failures) == 1
    assert report.failures[0]["snapshot_id"] == str(snap2_id)
    assert report.failures[0]["error"] == "RuntimeError"

    # Verify that Item 1 committed successfully despite Item 2 failing
    async with open_database_session() as session:
        res1 = await session.execute(
            select(ExtractedFactModel).where(ExtractedFactModel.snapshot_id == snap1_id)
        )
        assert len(res1.all()) == 1

        res2 = await session.execute(
            select(ExtractedFactModel).where(ExtractedFactModel.snapshot_id == snap2_id)
        )
        assert len(res2.all()) == 0


@pytest.mark.asyncio
async def test_cli_main_e2e_smoke(
    temporary_database_url: str,
    open_database_session: _OpenSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI entry point (main) wires PostgresFactStore and runs end-to-end against real database."""
    today = datetime.now(UTC).date()
    now = datetime.now(UTC)

    async with open_database_session() as session:
        await _create_item_and_snapshot(
            session,
            title="OpenAI GPT-4o Launch",
            content_text="OpenAI introduces GPT-4o model.",
            fetched_at=now,
        )
        await session.commit()

    monkeypatch.setenv("DATABASE_URL", temporary_database_url)

    # Run main CLI entrypoint
    exit_code = main(["--digest-date", today.isoformat(), "--since", "24h"])

    # Returns 0 (published) or 1 (draft/review with 0 changes)
    assert exit_code in (0, 1)

    # Verify a digest was persisted in the real PostgreSQL database
    async with open_database_session() as session:
        res = await session.execute(select(DigestModel).where(DigestModel.digest_date == today))
        digests = list(res.scalars().all())
        assert len(digests) >= 1


@pytest.mark.asyncio
async def test_published_outcome_persists_as_draft_then_publishes(
    open_database_session: _OpenSession,
) -> None:
    """A run resulting in all claims supported persists as draft first, then publishes cleanly.

    Ensures compatibility with PR #84 publication gate: brand-new digests cannot
    be directly persisted with status='published' via persist_digest(); the runner
    persists as draft first and transitions via publish_digest().
    """
    window_start = datetime(2026, 9, 7, 0, 0, 0, tzinfo=UTC)
    window_end = datetime(2026, 9, 8, 0, 0, 0, tzinfo=UTC)
    digest_date = date(2026, 9, 7)

    async with open_database_session() as session:
        _, _snap_id = await _create_item_and_snapshot(
            session,
            title="OpenAI GPT-4o Launch",
            content_text="OpenAI introduces GPT-4o with 128000 context window.",
            fetched_at=window_start + timedelta(hours=2),
        )
        await session.commit()

    def mock_extract(system: str, prompt: str) -> FactExtractionResponse:
        del system, prompt
        return FactExtractionResponse(
            facts=[
                FactCandidate(
                    field="context_window_tokens",
                    value="128000",
                    quoted_span="128000 context window",
                    confidence=0.95,
                )
            ]
        )

    report = await run_pipeline(
        session_factory=open_database_session,
        digest_date=digest_date,
        window_start=window_start,
        window_end=window_end,
        extract_call_fn=mock_extract,
    )

    assert report.selected_snapshot_count == 1
    assert report.processed_snapshot_count == 1
    assert report.failed_snapshot_count == 0
    assert report.extracted_change_count == 1
    assert report.claim_count == 1
    assert report.published is True
    assert report.status == "published"
    assert report.digest_status == "published"
    assert report.digest_id is not None

    async with open_database_session() as session:
        res = await session.execute(select(DigestModel).where(DigestModel.id == report.digest_id))
        persisted_digest = res.scalar_one_or_none()
        assert persisted_digest is not None
        assert persisted_digest.status == "published"
        assert persisted_digest.digest_date == digest_date
