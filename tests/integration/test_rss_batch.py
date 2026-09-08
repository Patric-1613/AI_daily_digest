"""Day 5: the `collect-rss-batch` runner against a real PostgreSQL
database and the real `PostgresSourceItemRepository`
(`ingestion/rss/batch.py`).

Proves what the offline batch tests can only approximate: the batch runs
every verified source through the existing persistence path, a repeat run
is idempotent, one source's failure leaves the others' rows intact, the
report and logs never expose a query string or scraped content, and the
real composition root fans the sources out concurrently against
independent sessions.

Row assertions are deltas scoped to each `source_id`, so no test assumes
the shared temporary database is empty (ADR 0002 section 15). Skips
automatically when no test database is configured; it is never marked
passed in that case.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_daily_digest.ingestion.db.models import DocumentSnapshotRow, SourceItemRow
from ai_daily_digest.ingestion.rss.batch import (
    BatchReport,
    BatchStatus,
    collect_batch_with_real_infrastructure,
    render_batch_report,
    run_rss_batch,
)
from ai_daily_digest.ingestion.rss.collector import CollectionStatus
from ai_daily_digest.ingestion.rss.transport import HttpResponse, PermanentTransportError
from ai_daily_digest.ingestion.sources import load_source_registry
from tests.integration.conftest import (
    create_temporary_database,
    drop_temporary_database,
    run_alembic_async,
)
from tests.unit.ingestion.rss_helpers import MappingFetcher, RecordingSleep, stepping_clock

pytestmark = pytest.mark.integration

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "rss"
_T0 = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
_VERIFIED = ("openai_news", "langchain_pypi", "langgraph_pypi")
_FEED_URL = {
    "openai_news": "https://openai.com/news/rss.xml",
    "langchain_pypi": "https://pypi.org/rss/project/langchain/releases.xml",
    "langgraph_pypi": "https://pypi.org/rss/project/langgraph/releases.xml",
}
_FIXTURE = {
    "openai_news": "openai_news_sample.xml",
    "langchain_pypi": "pypi_langchain_sample.xml",
    "langgraph_pypi": "pypi_langgraph_sample.xml",
}
_EXPECTED_ENTRIES = {"openai_news": 4, "langchain_pypi": 3, "langgraph_pypi": 3}
_REGISTRY = load_source_registry()


def _bound_session_factory(
    session: AsyncSession,
) -> Callable[[], AbstractAsyncContextManager[AsyncSession]]:
    @asynccontextmanager
    async def _open() -> AsyncIterator[AsyncSession]:
        yield session

    return _open


def _ok_fetcher(*source_ids: str) -> MappingFetcher:
    return MappingFetcher(
        {
            _FEED_URL[s]: HttpResponse(status_code=200, body=(_FIXTURES / _FIXTURE[s]).read_bytes())
            for s in source_ids
        }
    )


async def _counts(session: AsyncSession, source_id: str) -> tuple[int, int]:
    items = await session.scalar(
        select(func.count()).select_from(SourceItemRow).where(SourceItemRow.source_id == source_id)
    )
    snapshots = await session.scalar(
        select(func.count())
        .select_from(DocumentSnapshotRow)
        .join(SourceItemRow, DocumentSnapshotRow.source_item_id == SourceItemRow.id)
        .where(SourceItemRow.source_id == source_id)
    )
    return int(items or 0), int(snapshots or 0)


async def _run_batch(
    session: AsyncSession, fetcher: MappingFetcher, *, concurrency: int = 1
) -> BatchReport:
    return await run_rss_batch(
        registry=_REGISTRY,
        source_ids=_VERIFIED,
        fetcher=fetcher,
        session_factory=_bound_session_factory(session),
        concurrency=concurrency,
        sleep=RecordingSleep(),
        clock=stepping_clock(start=_T0),
    )


@pytest.mark.asyncio
async def test_batch_persists_every_verified_source(database_session: AsyncSession) -> None:
    before = {s: await _counts(database_session, s) for s in _VERIFIED}

    report = await _run_batch(database_session, _ok_fetcher(*_VERIFIED))

    assert report.status is BatchStatus.OK
    for source_id, expected in _EXPECTED_ENTRIES.items():
        items_after, snaps_after = await _counts(database_session, source_id)
        items_before, snaps_before = before[source_id]
        assert items_after - items_before == expected
        assert snaps_after - snaps_before == expected


@pytest.mark.asyncio
async def test_batch_rerun_is_idempotent(database_session: AsyncSession) -> None:
    await _run_batch(database_session, _ok_fetcher(*_VERIFIED))
    after_first = {s: await _counts(database_session, s) for s in _VERIFIED}

    rerun = await _run_batch(database_session, _ok_fetcher(*_VERIFIED))

    assert rerun.status is BatchStatus.OK
    by_id = {r.source_id: r for r in rerun.results}
    for source_id, expected in _EXPECTED_ENTRIES.items():
        assert by_id[source_id].created_item_count == 0
        assert by_id[source_id].created_snapshot_count == 0
        assert by_id[source_id].unchanged_count == expected
        assert await _counts(database_session, source_id) == after_first[source_id]


@pytest.mark.asyncio
async def test_one_source_failure_leaves_the_others_persisted(
    database_session: AsyncSession,
) -> None:
    before = {s: await _counts(database_session, s) for s in _VERIFIED}
    fetcher = MappingFetcher(
        {
            _FEED_URL["langchain_pypi"]: PermanentTransportError("HTTP 503 from feed"),
            _FEED_URL["openai_news"]: HttpResponse(
                status_code=200, body=(_FIXTURES / _FIXTURE["openai_news"]).read_bytes()
            ),
            _FEED_URL["langgraph_pypi"]: HttpResponse(
                status_code=200, body=(_FIXTURES / _FIXTURE["langgraph_pypi"]).read_bytes()
            ),
        }
    )

    report = await _run_batch(database_session, fetcher)

    assert report.status is BatchStatus.PARTIAL
    # the failed source wrote nothing
    assert await _counts(database_session, "langchain_pypi") == before["langchain_pypi"]
    # the healthy sources wrote their rows
    for source_id in ("openai_news", "langgraph_pypi"):
        items_after, _ = await _counts(database_session, source_id)
        assert items_after - before[source_id][0] == _EXPECTED_ENTRIES[source_id]


@pytest.mark.asyncio
async def test_report_and_logs_never_expose_a_query_string_or_feed_content(
    database_session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    # this fixture carries an off-domain entry link with `?token=abc123`
    fetcher = MappingFetcher(
        {
            _FEED_URL["langchain_pypi"]: HttpResponse(
                status_code=200,
                body=(_FIXTURES / "pypi_langchain_offsite_entry.xml").read_bytes(),
            ),
            _FEED_URL["openai_news"]: HttpResponse(
                status_code=200, body=(_FIXTURES / _FIXTURE["openai_news"]).read_bytes()
            ),
            _FEED_URL["langgraph_pypi"]: HttpResponse(
                status_code=200, body=(_FIXTURES / _FIXTURE["langgraph_pypi"]).read_bytes()
            ),
        }
    )

    with caplog.at_level(logging.INFO):
        report = await _run_batch(database_session, fetcher)

    rendered = render_batch_report(report)
    by_id = {r.source_id: r for r in report.results}
    assert by_id["langchain_pypi"].status is CollectionStatus.PARTIAL
    assert by_id["langchain_pypi"].error_category == "partial_entry_failures"
    for needle in ("token=abc123", "abc123", "?token", "<item>", "<rss"):
        assert needle not in rendered
        assert needle not in caplog.text


@pytest.mark.asyncio
async def test_real_infrastructure_runs_sources_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real composition root builds one engine + `build_session_factory`
    and fans the verified sources out at concurrency 3 against independent
    per-call sessions -- only the fetcher is faked."""
    base_url = os.environ.get("DATABASE_URL")
    if not base_url:
        pytest.skip("set DATABASE_URL to run PostgreSQL integration tests")
    isolated_url = await create_temporary_database(base_url)
    await run_alembic_async(database_url=isolated_url, target="head")
    monkeypatch.setenv("DATABASE_URL", isolated_url)
    try:
        report = await collect_batch_with_real_infrastructure(
            source_ids=_VERIFIED,
            concurrency=3,
            fetcher=_ok_fetcher(*_VERIFIED),
        )
    finally:
        await drop_temporary_database(base_url, isolated_url)

    assert report.status is BatchStatus.OK
    assert report.concurrency == 3
    by_id = {r.source_id: r for r in report.results}
    for source_id, expected in _EXPECTED_ENTRIES.items():
        assert by_id[source_id].created_item_count == expected
        assert by_id[source_id].created_snapshot_count == expected
