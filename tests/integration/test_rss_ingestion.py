"""The generic RSS collector against the real `PostgresSourceItemRepository`
and a real PostgreSQL database (`ingestion/rss/collector.py` +
docs/adr/0002-postgres-pgvector.md sections 8, 9, 13).

Proves the persistence-orchestration rules the in-memory collector unit
tests can only approximate: a first run inserts an item and a snapshot,
an identical rerun inserts neither, and a genuine content change at the
same canonical URL adds a second immutable snapshot and advances the
latest-snapshot pointer -- for `openai_news` and for the `langchain_pypi`
/ `langgraph_pypi` sources driven through the same adapter.

Skips automatically when no test database is configured (see
`tests/integration/conftest.py`); it is never marked passed in that
case.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_daily_digest.ingestion.db.models import DocumentSnapshotRow, SourceItemRow
from ai_daily_digest.ingestion.rss.collector import (
    CollectionReport,
    CollectionStatus,
    collect_rss_source,
)
from ai_daily_digest.ingestion.rss.run import (
    collect_with_real_infrastructure,
    exit_code_for,
    render_report,
    run_collection,
)
from ai_daily_digest.ingestion.rss.transport import HttpResponse
from ai_daily_digest.ingestion.sources import load_source_registry
from tests.integration.conftest import (
    create_temporary_database,
    drop_temporary_database,
    run_alembic_async,
)
from tests.unit.ingestion.rss_helpers import (
    FakeFetcher,
    RecordingSleep,
    build_policy,
    stepping_clock,
)

pytestmark = pytest.mark.integration

_SOURCE = load_source_registry().get("openai_news")
_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "rss"

# The four canonical URLs the `openai_news_sample.xml` / `openai_news_changed.xml`
# fixtures produce. Every assertion scopes to these rather than counting the
# whole table -- other integration suites commit their own `source_id="openai_news"`
# rows into the shared temporary database (ADR 0002 section 15: "no test assumes
# the tables are globally empty").
_SAMPLE_CANONICAL_URLS = (
    "https://openai.com/index/gpt-4o-256k-context",
    "https://openai.com/index/introducing-structured-outputs",
    "https://openai.com/research/scaling-laws-for-reasoning",
    "https://openai.com/index/whisper-large-v3-turbo",
)


def _feed_response(fixture: str) -> HttpResponse:
    return HttpResponse(status_code=200, body=(_FIXTURES / fixture).read_bytes())


def _bound_session_factory(
    session: AsyncSession,
) -> Callable[[], AbstractAsyncContextManager[AsyncSession]]:
    """Hand every entry the one per-test session. `ingest_document`
    commits inside it; the `database_session` fixture joins the outer
    transaction via a SAVEPOINT, so those commits are still rolled back
    at teardown."""

    @asynccontextmanager
    async def _open() -> AsyncIterator[AsyncSession]:
        yield session

    return _open


async def _collect(session: AsyncSession, fixture: str) -> CollectionReport:
    return await collect_rss_source(
        source=_SOURCE,
        policy=build_policy(),
        fetcher=FakeFetcher(_feed_response(fixture)),
        session_factory=_bound_session_factory(session),
        clock=stepping_clock(start=datetime(2026, 9, 5, 12, 0, tzinfo=UTC)),
        sleep=RecordingSleep(),
    )


async def _counts(session: AsyncSession) -> tuple[int, int]:
    """`(source_items, document_snapshots)` for exactly the four sample
    fixture articles -- never a whole-table count (ADR 0002 section 15)."""
    items = await session.scalar(
        select(func.count())
        .select_from(SourceItemRow)
        .where(SourceItemRow.canonical_url.in_(_SAMPLE_CANONICAL_URLS))
    )
    snapshots = await session.scalar(
        select(func.count())
        .select_from(DocumentSnapshotRow)
        .join(SourceItemRow, DocumentSnapshotRow.source_item_id == SourceItemRow.id)
        .where(SourceItemRow.canonical_url.in_(_SAMPLE_CANONICAL_URLS))
    )
    return int(items or 0), int(snapshots or 0)


@pytest.mark.asyncio
async def test_first_run_persists_items_and_snapshots(database_session: AsyncSession) -> None:
    report = await _collect(database_session, "openai_news_sample.xml")

    assert report.status is CollectionStatus.OK
    assert report.created_item_count == 4
    assert report.created_snapshot_count == 4
    assert await _counts(database_session) == (4, 4)

    # Every persisted sample item points at a snapshot (ADR 0002 section 13 step 3).
    rows = (
        (
            await database_session.execute(
                select(SourceItemRow).where(SourceItemRow.canonical_url.in_(_SAMPLE_CANONICAL_URLS))
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 4
    assert all(row.latest_snapshot_id is not None for row in rows)
    assert {row.source_id for row in rows} == {"openai_news"}


@pytest.mark.asyncio
async def test_identical_rerun_is_idempotent(database_session: AsyncSession) -> None:
    await _collect(database_session, "openai_news_sample.xml")
    before = await _counts(database_session)

    rerun = await _collect(database_session, "openai_news_sample.xml")

    assert rerun.created_item_count == 0
    assert rerun.created_snapshot_count == 0
    assert rerun.unchanged_count == 4
    assert await _counts(database_session) == before


@pytest.mark.asyncio
async def test_changed_content_adds_a_snapshot_and_advances_the_pointer(
    database_session: AsyncSession,
) -> None:
    await _collect(database_session, "openai_news_sample.xml")

    canonical = "https://openai.com/index/gpt-4o-256k-context"
    item_before = (
        await database_session.execute(
            select(SourceItemRow).where(SourceItemRow.canonical_url == canonical)
        )
    ).scalar_one()
    first_snapshot_id = item_before.latest_snapshot_id

    changed = await _collect(database_session, "openai_news_changed.xml")

    assert changed.created_item_count == 0
    assert changed.created_snapshot_count == 1
    assert changed.unchanged_count == 3

    # One item gained a second immutable snapshot; the pointer moved to it.
    item_ids = [item_before.id]
    snapshot_count = await database_session.scalar(
        select(func.count())
        .select_from(DocumentSnapshotRow)
        .where(DocumentSnapshotRow.source_item_id.in_(item_ids))
    )
    assert snapshot_count == 2

    await database_session.refresh(item_before)
    assert item_before.latest_snapshot_id != first_snapshot_id
    assert item_before.title.startswith("GPT-4o")


@pytest.mark.asyncio
async def test_snapshot_raw_location_is_not_the_public_article_url(
    database_session: AsyncSession,
) -> None:
    """Correction 3: `raw_location` must stay NULL until immutable
    raw-object storage exists -- it must never be set to the public
    article URL."""
    await _collect(database_session, "openai_news_sample.xml")

    raw_locations = (
        (
            await database_session.execute(
                select(DocumentSnapshotRow.raw_location)
                .join(SourceItemRow, DocumentSnapshotRow.source_item_id == SourceItemRow.id)
                .where(SourceItemRow.canonical_url.in_(_SAMPLE_CANONICAL_URLS))
            )
        )
        .scalars()
        .all()
    )
    assert raw_locations == [None, None, None, None]


@pytest.mark.asyncio
async def test_composition_root_run_collection_against_real_db(
    database_session: AsyncSession,
) -> None:
    """Correction 5: the `run.run_collection` core -- `sources.yaml` load,
    `openai_news` selection, real `PostgresSourceItemRepository` -- drives
    a full run end to end. Only the fetcher is faked (no live network)."""
    report = await run_collection(
        source_id="openai_news",
        fetcher=FakeFetcher(_feed_response("openai_news_sample.xml")),
        session_factory=_bound_session_factory(database_session),
    )

    assert report.source_id == "openai_news"
    assert report.status is CollectionStatus.OK
    assert exit_code_for(report) == 0
    assert await _counts(database_session) == (4, 4)

    parsed = json.loads(render_report(report))
    assert parsed["status"] == "ok"
    assert parsed["created_item_count"] == 4


@pytest.mark.asyncio
async def test_collect_with_real_infrastructure_builds_its_own_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The outer glue: `DATABASE_URL` -> `DatabaseConfig` -> shared
    `build_engine`/`build_session_factory` -> preflight ->
    `collect_rss_source` -> engine disposed. Runs against its own
    run-unique migrated database (not the shared session fixture's), and
    only the fetcher is faked."""
    base_url = os.environ["DATABASE_URL"]
    isolated_url = await create_temporary_database(base_url)
    await run_alembic_async(database_url=isolated_url, target="head")
    monkeypatch.setenv("DATABASE_URL", isolated_url)
    try:
        report = await collect_with_real_infrastructure(
            "openai_news",
            fetcher=FakeFetcher(_feed_response("openai_news_sample.xml")),
        )
    finally:
        await drop_temporary_database(base_url, isolated_url)

    assert report.status is CollectionStatus.OK
    assert report.created_item_count == 4
    assert report.created_snapshot_count == 4


# -- Day 4: the same adapter, driven for each new PyPI source ----------

_PYPI_SAMPLE = {
    "langchain_pypi": "pypi_langchain_sample.xml",
    "langgraph_pypi": "pypi_langgraph_sample.xml",
}


async def _pypi_urls(session: AsyncSession, source_id: str) -> list[str]:
    return list(
        (
            await session.execute(
                select(SourceItemRow.canonical_url).where(SourceItemRow.source_id == source_id)
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("source_id", list(_PYPI_SAMPLE))
async def test_pypi_source_first_run_persists_through_the_generic_adapter(
    source_id: str, database_session: AsyncSession
) -> None:
    report = await run_collection(
        source_id=source_id,
        fetcher=FakeFetcher(_feed_response(_PYPI_SAMPLE[source_id])),
        session_factory=_bound_session_factory(database_session),
    )

    assert report.source_id == source_id
    assert report.status is CollectionStatus.OK
    assert report.created_item_count == report.created_snapshot_count == 3

    rows = (
        (
            await database_session.execute(
                select(SourceItemRow).where(SourceItemRow.source_id == source_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 3
    assert {row.publisher for row in rows} == {"Python Package Index"}
    assert all(row.canonical_url.startswith("https://pypi.org/project/") for row in rows)
    assert all(row.latest_snapshot_id is not None for row in rows)


@pytest.mark.asyncio
async def test_pypi_source_rerun_is_idempotent_and_change_adds_one_snapshot(
    database_session: AsyncSession,
) -> None:
    async def _run(fixture: str) -> CollectionReport:
        return await run_collection(
            source_id="langchain_pypi",
            fetcher=FakeFetcher(_feed_response(fixture)),
            session_factory=_bound_session_factory(database_session),
        )

    first = await _run("pypi_langchain_sample.xml")
    assert first.created_item_count == first.created_snapshot_count == 3

    rerun = await _run("pypi_langchain_sample.xml")
    assert rerun.created_item_count == 0
    assert rerun.created_snapshot_count == 0
    assert rerun.unchanged_count == 3

    changed = await _run("pypi_langchain_changed.xml")
    assert changed.created_item_count == 0
    assert changed.created_snapshot_count == 1
    assert changed.unchanged_count == 2

    urls = await _pypi_urls(database_session, "langchain_pypi")
    assert len(urls) == 3  # no duplicate SourceItem
    total_snapshots = await database_session.scalar(
        select(func.count())
        .select_from(DocumentSnapshotRow)
        .join(SourceItemRow, DocumentSnapshotRow.source_item_id == SourceItemRow.id)
        .where(SourceItemRow.source_id == "langchain_pypi")
    )
    assert total_snapshots == 4  # 3 originals + 1 new immutable snapshot


@pytest.mark.asyncio
async def test_off_domain_pypi_entry_fails_without_a_false_success(
    database_session: AsyncSession,
) -> None:
    report = await run_collection(
        source_id="langchain_pypi",
        fetcher=FakeFetcher(_feed_response("pypi_langchain_offsite_entry.xml")),
        session_factory=_bound_session_factory(database_session),
    )

    # One entry off the pypi.org allowlist -> PARTIAL, never OK.
    assert report.status is CollectionStatus.PARTIAL
    assert report.created_item_count == 2
    assert report.failed_item_count == 1
    assert report.failures[0].raw_index == 1
    joined = f"{report.failures[0].reason} {report.failures[0].link}"
    assert "token=abc123" not in joined
    assert len(await _pypi_urls(database_session, "langchain_pypi")) == 2
