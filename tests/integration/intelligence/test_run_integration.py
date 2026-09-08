"""Integration tests for the persistent daily intelligence runner against PostgreSQL."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_daily_digest.ingestion.db.models import DocumentSnapshotRow, SourceItemRow
from ai_daily_digest.intelligence.db.models import (
    ChangeModel,
    ChangeSetModel,
    DigestModel,
    ExtractedFactModel,
)
from ai_daily_digest.intelligence.db.repository import PostgresFactStore
from ai_daily_digest.intelligence.extract_facts import FactCandidate, FactExtractionResponse
from ai_daily_digest.intelligence.run import (
    main,
    run_pipeline,
    select_snapshots_in_window,
)
from ai_daily_digest.shared.ids import new_id
from ai_daily_digest.shared.schemas import (
    DisclosureStatus,
    ExtractedFact,
    ExtractionMethod,
    Subject,
)

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
    window_start = datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC)
    window_end = datetime(2026, 9, 2, 0, 0, 0, tzinfo=UTC)

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
    window_start = datetime(2026, 9, 2, 0, 0, 0, tzinfo=UTC)
    window_end = datetime(2026, 9, 3, 0, 0, 0, tzinfo=UTC)
    digest_date = date(2026, 9, 2)

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
    window_start = datetime(2026, 9, 3, 0, 0, 0, tzinfo=UTC)
    window_end = datetime(2026, 9, 4, 0, 0, 0, tzinfo=UTC)
    digest_date = date(2026, 9, 3)

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
    target_date = date(2026, 9, 4)
    target_time = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)

    async with open_database_session() as session:
        await _create_item_and_snapshot(
            session,
            title="OpenAI GPT-4o Launch",
            content_text="OpenAI introduces GPT-4o model.",
            fetched_at=target_time,
        )
        await session.commit()

    monkeypatch.setenv("DATABASE_URL", temporary_database_url)

    # Run main CLI entrypoint via to_thread since main() invokes asyncio.run()
    exit_code = await asyncio.to_thread(
        main, ["--digest-date", target_date.isoformat(), "--since", "24h"]
    )

    # Returns 0 (published) or 1 (draft/review with 0 changes)
    assert exit_code in (0, 1)

    # Verify a digest was persisted in the real PostgreSQL database
    async with open_database_session() as session:
        res = await session.execute(
            select(DigestModel).where(DigestModel.digest_date == target_date)
        )
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
    window_start = datetime(2026, 9, 5, 0, 0, 0, tzinfo=UTC)
    window_end = datetime(2026, 9, 6, 0, 0, 0, tzinfo=UTC)
    digest_date = date(2026, 9, 5)

    async with open_database_session() as session:
        # Snapshot 1: Baseline observation establishing output_price_usd = 15
        await _create_item_and_snapshot(
            session,
            title="OpenAI GPT-4o Baseline",
            content_text="OpenAI sets GPT-4o output price at 15 dollars per million tokens.",
            fetched_at=window_start + timedelta(hours=1),
        )
        # Snapshot 2: Update observation changing output_price_usd to 30
        await _create_item_and_snapshot(
            session,
            title="OpenAI GPT-4o Launch",
            content_text="OpenAI sets GPT-4o output price at 30 dollars per million tokens.",
            fetched_at=window_start + timedelta(hours=2),
        )
        await session.commit()

    def mock_extract(system: str, prompt: str) -> FactExtractionResponse:
        del system
        val = "30" if "30" in prompt else "15"
        return FactExtractionResponse(
            facts=[
                FactCandidate(
                    field="output_price_usd",
                    value=val,
                    quoted_span=f"{val} dollars",
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

    assert report.selected_snapshot_count == 2
    assert report.processed_snapshot_count == 2
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


@pytest.mark.asyncio
async def test_multiple_snapshots_same_subject_share_one_changeset(  # pylint: disable=too-many-locals
    open_database_session: _OpenSession,
) -> None:
    """Two snapshots for the same subject in one run share one ChangeSet across transactions.

    Regression test for ADR 0007 batch-scoped ChangeSet ID allocation:
    when separate per-item transactions process multiple snapshots for the
    same Subject in a single run, the repository must not duplicate-insert
    the ChangeSet row, and all resulting Changes must reference the exact same
    ChangeSet ID with sequential positions.
    """
    baseline_time = datetime(2026, 9, 6, 12, 0, 0, tzinfo=UTC)
    window_start = datetime(2026, 9, 7, 0, 0, 0, tzinfo=UTC)
    window_end = datetime(2026, 9, 8, 0, 0, 0, tzinfo=UTC)
    digest_date = date(2026, 9, 7)
    subject = Subject(company="OpenAI", product="GPT-4o")

    # Seed baseline facts prior to the window so subsequent snapshots generate changes
    async with open_database_session() as session:
        _, base_snap_id = await _create_item_and_snapshot(
            session,
            title="OpenAI GPT-4o Initial",
            content_text="OpenAI sets GPT-4o output price at 15 and input price at 5.",
            fetched_at=baseline_time,
        )
        baseline_facts = [
            ExtractedFact(
                id=new_id(),
                snapshot_id=base_snap_id,
                field="output_price_usd",
                value="15",
                quoted_span="15 dollars",
                confidence=0.95,
                extraction_method=ExtractionMethod.DETERMINISTIC,
                disclosure_status=DisclosureStatus.DISCLOSED,
            ),
            ExtractedFact(
                id=new_id(),
                snapshot_id=base_snap_id,
                field="input_price_usd",
                value="5",
                quoted_span="5 dollars",
                confidence=0.95,
                extraction_method=ExtractionMethod.DETERMINISTIC,
                disclosure_status=DisclosureStatus.DISCLOSED,
            ),
        ]
        store = PostgresFactStore(session)
        await store.detect_and_persist_changes(
            subject=subject,
            facts=baseline_facts,
            snapshot_observed_at=baseline_time,
            detected_at=baseline_time,
            extraction_version=1,
        )
        await session.commit()

    # Create two snapshots in the window for the same subject
    async with open_database_session() as session:
        # Snapshot 1: updates output_price_usd to 30
        _, snap1_id = await _create_item_and_snapshot(
            session,
            title="OpenAI GPT-4o Price Increase",
            content_text="OpenAI updates GPT-4o output price to 30 dollars.",
            fetched_at=window_start + timedelta(hours=1),
        )
        # Snapshot 2: updates input_price_usd to 10
        _, snap2_id = await _create_item_and_snapshot(
            session,
            title="OpenAI GPT-4o Input Adjustment",
            content_text="OpenAI updates GPT-4o input price to 10 dollars.",
            fetched_at=window_start + timedelta(hours=2),
        )
        await session.commit()

    def mock_extract(system: str, prompt: str) -> FactExtractionResponse:
        del system
        if "output price to 30" in prompt:
            return FactExtractionResponse(
                facts=[
                    FactCandidate(
                        field="output_price_usd",
                        value="30",
                        quoted_span="30 dollars",
                        confidence=0.95,
                    )
                ]
            )
        return FactExtractionResponse(
            facts=[
                FactCandidate(
                    field="input_price_usd",
                    value="10",
                    quoted_span="10 dollars",
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

    assert report.selected_snapshot_count == 2
    assert report.processed_snapshot_count == 2
    assert report.failed_snapshot_count == 0
    assert report.extracted_change_count == 2

    async with open_database_session() as session:
        res = await session.execute(
            select(ChangeModel)
            .where(ChangeModel.current_snapshot_id.in_([snap1_id, snap2_id]))
            .order_by(ChangeModel.position.asc())
        )
        changes = list(res.scalars().all())
        assert len(changes) == 2

        cs_ids = {c.change_set_id for c in changes}
        assert len(cs_ids) == 1
        shared_cs_id = cs_ids.pop()

        assert [c.position for c in changes] == [0, 1]

        cs_res = await session.execute(
            select(ChangeSetModel).where(ChangeSetModel.id == shared_cs_id)
        )
        change_sets = list(cs_res.scalars().all())
        assert len(change_sets) == 1


@pytest.mark.asyncio
async def test_failed_item_forces_persisted_digest_to_review(
    open_database_session: _OpenSession,
) -> None:
    """A run containing failed items forces the persisted digest to review/unpublished status."""
    digest_date = date(2026, 9, 10)
    window_start = datetime(2026, 9, 10, 0, 0, 0, tzinfo=UTC)
    window_end = datetime(2026, 9, 11, 0, 0, 0, tzinfo=UTC)

    async with open_database_session() as session:
        # Item 1: Valid item
        _, _snap1_id = await _create_item_and_snapshot(
            session,
            title="OpenAI GPT-4o Working",
            content_text="OpenAI introduces GPT-4o with 128000 context window.",
            fetched_at=window_start + timedelta(hours=1),
        )
        # Item 2: Failing item
        _, _snap2_id = await _create_item_and_snapshot(
            session,
            title="OpenAI GPT-4o Broken",
            content_text="OpenAI introduces GPT-4o with invalid content 999999.",
            fetched_at=window_start + timedelta(hours=2),
        )
        await session.commit()

    def mock_extract(system: str, prompt: str) -> FactExtractionResponse:
        del system
        if "999999" in prompt:
            raise RuntimeError("Extraction simulated crash")
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

    assert report.failed_snapshot_count == 1
    assert report.digest_status == "review"
    assert report.published is False
    assert report.status == "partial"

    # Verify directly in PostgreSQL that the persisted digest row is review, NOT published
    async with open_database_session() as session:
        res = await session.execute(
            select(DigestModel).where(DigestModel.id == report.digest_id)
        )
        digest_row = res.scalar_one()
        assert digest_row.status == "review"

        published_res = await session.execute(
            select(DigestModel).where(
                DigestModel.digest_date == digest_date,
                DigestModel.status == "published",
            )
        )
        assert published_res.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_failed_digest_persistence_retry_recovers_changes(
    open_database_session: _OpenSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry after a failed digest persistence recovers already-committed changes without loss."""
    baseline_date = date(2026, 9, 11)
    window_start = datetime(2026, 9, 11, 0, 0, 0, tzinfo=UTC)
    window_end = datetime(2026, 9, 12, 0, 0, 0, tzinfo=UTC)

    # 1. Establish baseline observation before the window
    async with open_database_session() as session:
        _, _base_snap_id = await _create_item_and_snapshot(
            session,
            title="OpenAI GPT-4o Baseline",
            content_text="OpenAI introduces GPT-4o with 128000 tokens.",
            fetched_at=window_start - timedelta(hours=4),
        )
        await session.commit()

    def baseline_extract(system: str, prompt: str) -> FactExtractionResponse:
        del system, prompt
        return FactExtractionResponse(
            facts=[
                FactCandidate(
                    field="context_window_tokens",
                    value="128000",
                    quoted_span="128000 tokens",
                    confidence=0.95,
                )
            ]
        )

    # Run pipeline for baseline snapshot so current_facts has 128000
    await run_pipeline(
        session_factory=open_database_session,
        digest_date=date(2026, 9, 10),
        window_start=window_start - timedelta(hours=6),
        window_end=window_start - timedelta(hours=2),
        extract_call_fn=baseline_extract,
    )

    # 2. Insert new snapshot in the test window that modifies context_window_tokens to 256000
    async with open_database_session() as session:
        _, test_snap_id = await _create_item_and_snapshot(
            session,
            title="OpenAI GPT-4o Upgrade",
            content_text="OpenAI updates GPT-4o with 256000 tokens.",
            fetched_at=window_start + timedelta(hours=2),
        )
        await session.commit()

    def upgrade_extract(system: str, prompt: str) -> FactExtractionResponse:
        del system, prompt
        return FactExtractionResponse(
            facts=[
                FactCandidate(
                    field="context_window_tokens",
                    value="256000",
                    quoted_span="256000 tokens",
                    confidence=0.95,
                )
            ]
        )

    # 3. Simulate failure during final digest persistence in Run 1
    original_persist = PostgresFactStore.persist_digest

    async def _failing_persist(self: PostgresFactStore, digest: Any) -> Any:
        raise RuntimeError("Simulated digest persistence database crash")

    monkeypatch.setattr(PostgresFactStore, "persist_digest", _failing_persist)

    with pytest.raises(RuntimeError, match="Simulated digest persistence database crash"):
        await run_pipeline(
            session_factory=open_database_session,
            digest_date=baseline_date,
            window_start=window_start,
            window_end=window_end,
            extract_call_fn=upgrade_extract,
        )

    # Verify that Item's change WAS committed to changes table, but NO digest was persisted
    async with open_database_session() as session:
        res_changes = await session.execute(
            select(ChangeModel).where(ChangeModel.current_snapshot_id == test_snap_id)
        )
        committed_changes = list(res_changes.scalars().all())
        assert len(committed_changes) == 1
        assert committed_changes[0].current_value == "256000"

        res_digest = await session.execute(
            select(DigestModel).where(DigestModel.digest_date == baseline_date)
        )
        assert res_digest.scalar_one_or_none() is None

    # 4. Run 2: The Retry (without failure)
    monkeypatch.setattr(PostgresFactStore, "persist_digest", original_persist)

    retry_report = await run_pipeline(
        session_factory=open_database_session,
        digest_date=baseline_date,
        window_start=window_start,
        window_end=window_end,
        extract_call_fn=upgrade_extract,
    )

    # Resumable recovery: even though detect_and_persist_changes emitted no new change (already in current_facts),
    # get_changes_for_snapshot recovered the committed change!
    assert retry_report.extracted_change_count == 1
    assert retry_report.claim_count == 1
    assert retry_report.digest_status == "published"
    assert retry_report.published is True

    async with open_database_session() as session:
        res_published = await session.execute(
            select(DigestModel).where(
                DigestModel.digest_date == baseline_date,
                DigestModel.status == "published",
            )
        )
        published_digest = res_published.scalar_one()
        assert published_digest.id == retry_report.digest_id

