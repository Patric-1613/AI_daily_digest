"""Collection orchestration (`ingestion/rss/collector.py`): the
`CollectionReport` counts, one-transaction-per-entry isolation, and
deterministic timestamps. No network, no database -- a scripted
`FakeFetcher` feeds saved fixture bytes and an in-memory repository
stands in for PostgreSQL."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ai_daily_digest.ingestion.persistence import IngestionWriteRepository
from ai_daily_digest.ingestion.rss.collector import (
    CollectionReport,
    CollectionStatus,
    collect_openai_rss,
)
from ai_daily_digest.ingestion.rss.transport import (
    HttpResponse,
    PermanentTransportError,
    TransientTransportError,
)
from ai_daily_digest.ingestion.sources import load_source_registry
from ai_daily_digest.shared.schemas import DocumentSnapshot
from tests.unit.ingestion.fake_repository import InMemorySourceItemRepository
from tests.unit.ingestion.rss_helpers import (
    FakeFetcher,
    RecordingSleep,
    build_policy,
    load_fixture,
    noop_session_factory,
    stepping_clock,
)

_SOURCE = load_source_registry().get("openai_news")


def _repo_factory(
    repository: IngestionWriteRepository,
) -> Callable[[AsyncSession], IngestionWriteRepository]:
    def _make(_session: AsyncSession) -> IngestionWriteRepository:
        return repository

    return _make


def _feed_response(fixture: str, **overrides: object) -> HttpResponse:
    return HttpResponse(status_code=200, body=load_fixture(fixture), **overrides)  # type: ignore[arg-type]


async def _collect(
    fetcher: FakeFetcher,
    repository: InMemorySourceItemRepository,
    *,
    clock: Callable[[], datetime] | None = None,
    sleep: RecordingSleep | None = None,
) -> CollectionReport:
    return await collect_openai_rss(
        source=_SOURCE,
        policy=build_policy(),
        fetcher=fetcher,
        session_factory=noop_session_factory(),  # type: ignore[arg-type]
        repository_factory=_repo_factory(repository),
        clock=clock or stepping_clock(),
        sleep=sleep or RecordingSleep(),
    )


@pytest.mark.asyncio
async def test_first_run_creates_one_item_and_snapshot_per_entry() -> None:
    repository = InMemorySourceItemRepository()
    report = await _collect(FakeFetcher(_feed_response("openai_news_sample.xml")), repository)

    assert report.status is CollectionStatus.OK
    assert report.fetched_entry_count == 4
    assert report.created_item_count == 4
    assert report.created_snapshot_count == 4
    assert report.unchanged_count == 0
    assert report.failed_item_count == 0
    assert report.failures == ()


@pytest.mark.asyncio
async def test_duplicate_feed_entries_deduplicate_within_one_run() -> None:
    repository = InMemorySourceItemRepository()
    report = await _collect(
        FakeFetcher(_feed_response("openai_news_with_duplicate.xml")), repository
    )

    # Three entries, two of which canonicalize to the same URL and carry
    # identical content -> two items, two snapshots, one unchanged.
    assert report.fetched_entry_count == 3
    assert report.created_item_count == 2
    assert report.created_snapshot_count == 2
    assert report.unchanged_count == 1
    assert report.status is CollectionStatus.OK


@pytest.mark.asyncio
async def test_identical_rerun_creates_no_new_rows() -> None:
    repository = InMemorySourceItemRepository()
    await _collect(FakeFetcher(_feed_response("openai_news_sample.xml")), repository)

    rerun = await _collect(FakeFetcher(_feed_response("openai_news_sample.xml")), repository)

    assert rerun.created_item_count == 0
    assert rerun.created_snapshot_count == 0
    assert rerun.unchanged_count == 4
    assert rerun.status is CollectionStatus.OK


@pytest.mark.asyncio
async def test_changed_content_creates_a_new_snapshot_only() -> None:
    repository = InMemorySourceItemRepository()
    await _collect(FakeFetcher(_feed_response("openai_news_sample.xml")), repository)

    changed = await _collect(FakeFetcher(_feed_response("openai_news_changed.xml")), repository)

    # Only the first entry's body changed.
    assert changed.created_item_count == 0
    assert changed.created_snapshot_count == 1
    assert changed.unchanged_count == 3
    assert changed.status is CollectionStatus.OK


@pytest.mark.asyncio
async def test_malformed_entry_is_reported_while_siblings_continue() -> None:
    repository = InMemorySourceItemRepository()
    report = await _collect(
        FakeFetcher(_feed_response("openai_news_malformed_entry.xml")), repository
    )

    assert report.status is CollectionStatus.PARTIAL
    assert report.fetched_entry_count == 4
    assert report.created_item_count == 2
    assert report.failed_item_count == 2
    assert {failure.raw_index for failure in report.failures} == {1, 2}
    # The two valid siblings still persisted.
    assert report.created_snapshot_count == 2


@pytest.mark.asyncio
async def test_malformed_xml_is_a_source_level_failure() -> None:
    repository = InMemorySourceItemRepository()
    report = await _collect(
        FakeFetcher(_feed_response("openai_news_malformed_xml.xml")), repository
    )

    assert report.status is CollectionStatus.FAILED
    assert report.fetched_entry_count == 0
    assert report.created_item_count == 0
    assert report.created_snapshot_count == 0
    assert report.failed_item_count == 1


@pytest.mark.asyncio
async def test_completely_empty_body_is_a_source_level_failure() -> None:
    repository = InMemorySourceItemRepository()
    report = await _collect(FakeFetcher(HttpResponse(status_code=200, body=b"")), repository)

    assert report.status is CollectionStatus.FAILED
    assert report.fetched_entry_count == 0
    assert report.failed_item_count == 1
    assert "parse failed" in report.failures[0].reason


@pytest.mark.asyncio
async def test_zero_item_feed_is_an_anomaly_not_ok() -> None:
    repository = InMemorySourceItemRepository()
    report = await _collect(FakeFetcher(_feed_response("openai_news_empty.xml")), repository)

    assert report.status is CollectionStatus.FAILED
    assert report.fetched_entry_count == 0
    assert report.created_item_count == 0
    assert report.failed_item_count == 1
    assert "zero <item>" in report.failures[0].reason


@pytest.mark.asyncio
async def test_a_malformed_url_entry_fails_while_valid_siblings_continue() -> None:
    repository = InMemorySourceItemRepository()
    report = await _collect(
        FakeFetcher(_feed_response("openai_news_bad_url_entry.xml")), repository
    )

    assert report.status is CollectionStatus.PARTIAL
    assert report.fetched_entry_count == 4
    # entries 1 (bad port) and 2 (user-info) fail; 0 and 3 persist.
    assert report.created_item_count == 2
    assert report.created_snapshot_count == 2
    assert {failure.raw_index for failure in report.failures} == {1, 2}

    # The credential in entry 2's link/guid must not survive into the report.
    joined = " ".join(
        f"{failure.reason} {failure.link} {failure.guid}" for failure in report.failures
    )
    assert "s3cr3t-token" not in joined
    assert "api_key=leakme" not in joined
    assert "alice" not in joined


@pytest.mark.asyncio
async def test_entry_links_are_validated_against_the_source_policy() -> None:
    """Finding 2: an entry whose link is http or off-domain fails this one
    entry; valid siblings still persist, and nothing unsafe leaks."""
    repository = InMemorySourceItemRepository()
    report = await _collect(
        FakeFetcher(_feed_response("openai_news_offsite_entry.xml")), repository
    )

    assert report.status is CollectionStatus.PARTIAL
    assert report.fetched_entry_count == 4
    # entry 1 (http) and entry 2 (off-domain host) fail; 0 and 3 persist.
    assert report.created_item_count == 2
    assert report.created_snapshot_count == 2
    assert {failure.raw_index for failure in report.failures} == {1, 2}

    # The rejected host/path may appear as diagnostics, but the query-string
    # secret must not.
    joined = " ".join(
        f"{failure.reason} {failure.link} {failure.guid}" for failure in report.failures
    )
    assert "token=abc123" not in joined
    assert "abc123" not in joined


@pytest.mark.asyncio
async def test_fetch_failure_is_reported_and_not_retried() -> None:
    repository = InMemorySourceItemRepository()
    fetcher = FakeFetcher(PermanentTransportError("HTTP 404 from https://openai.com/news/rss.xml"))

    report = await _collect(fetcher, repository)

    assert report.status is CollectionStatus.FAILED
    assert report.fetch_attempts == 1
    assert fetcher.call_count == 1


@pytest.mark.asyncio
async def test_transient_fetch_failure_follows_the_retry_policy() -> None:
    repository = InMemorySourceItemRepository()
    fetcher = FakeFetcher(
        TransientTransportError("boom"),
        TransientTransportError("boom"),
        _feed_response("openai_news_sample.xml"),
    )
    sleep = RecordingSleep()

    report = await _collect(fetcher, repository, sleep=sleep)

    assert report.status is CollectionStatus.OK
    assert report.fetch_attempts == 3
    assert len(sleep.delays) == 2
    assert report.created_item_count == 4


@pytest.mark.asyncio
async def test_report_timestamps_are_deterministic_under_an_injected_clock() -> None:
    repository = InMemorySourceItemRepository()
    clock = stepping_clock(start=datetime(2026, 9, 5, 8, 0, 0, tzinfo=UTC), step_seconds=5)

    report = await _collect(
        FakeFetcher(_feed_response("openai_news_sample.xml")), repository, clock=clock
    )

    assert report.started_at == datetime(2026, 9, 5, 8, 0, 0, tzinfo=UTC)
    assert report.completed_at == datetime(2026, 9, 5, 8, 0, 5, tzinfo=UTC)


@pytest.mark.asyncio
async def test_persistence_failure_reason_never_leaks_exception_detail() -> None:
    secret = "password=hunter2 content='confidential snapshot body'"

    class _ExplodingRepository(InMemorySourceItemRepository):
        async def add_snapshot_if_new(self, **_kwargs: object) -> DocumentSnapshot:
            raise RuntimeError(secret)

    repository = _ExplodingRepository()
    report = await _collect(FakeFetcher(_feed_response("openai_news_sample.xml")), repository)

    assert report.status is CollectionStatus.PARTIAL
    assert report.failed_item_count == 4
    for failure in report.failures:
        assert secret not in failure.reason
        assert "hunter2" not in failure.reason
        assert failure.reason == "persistence failed (RuntimeError)"
