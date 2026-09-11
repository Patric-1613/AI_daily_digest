"""Offline collection tests for explicitly selected first-party articles."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_daily_digest.ingestion.article.collector import (
    ArticleCollectionReport,
    ArticleCollectionStatus,
    ArticleFailureCategory,
    collect_official_articles,
)
from ai_daily_digest.ingestion.rss.transport import HttpResponse, TransientTransportError
from ai_daily_digest.ingestion.sources import load_source_registry
from tests.unit.ingestion.fake_repository import InMemorySourceItemRepository
from tests.unit.ingestion.rss_helpers import (
    FakeFetcher,
    RecordingSleep,
    noop_session_factory,
    stepping_clock,
)

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "article"
_URL_100K = "https://www.anthropic.com/news/100k-context-windows"
_URL_200K = "https://www.anthropic.com/news/claude-2-1"


def _fixture(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


@pytest.mark.asyncio
async def test_collect_articles_persists_ordered_baseline_and_follow_up() -> None:
    registry = load_source_registry()
    repository = InMemorySourceItemRepository()
    report = await collect_official_articles(
        source=registry.get("anthropic_news"),
        urls=[_URL_100K, _URL_200K],
        policy=registry.collection_policy,
        fetcher=FakeFetcher(
            HttpResponse(status_code=200, body=_fixture("anthropic_context_100k.html")),
            HttpResponse(status_code=200, body=_fixture("anthropic_context_200k.html")),
        ),
        session_factory=noop_session_factory(),  # type: ignore[arg-type]
        repository_factory=lambda _session: repository,
        clock=stepping_clock(),
    )

    assert report.status is ArticleCollectionStatus.OK
    assert report.requested_count == report.processed_count == 2
    assert report.created_item_count == report.created_snapshot_count == 2
    assert report.failed_item_count == report.unchanged_count == 0


@pytest.mark.asyncio
async def test_collect_article_rerun_is_idempotent() -> None:
    registry = load_source_registry()
    repository = InMemorySourceItemRepository()

    async def run_once() -> ArticleCollectionReport:
        return await collect_official_articles(
            source=registry.get("anthropic_news"),
            urls=[_URL_100K],
            policy=registry.collection_policy,
            fetcher=FakeFetcher(
                HttpResponse(status_code=200, body=_fixture("anthropic_context_100k.html"))
            ),
            session_factory=noop_session_factory(),  # type: ignore[arg-type]
            repository_factory=lambda _session: repository,
            clock=stepping_clock(),
        )

    await run_once()
    second = await run_once()

    assert second.status is ArticleCollectionStatus.OK
    assert second.created_item_count == 0
    assert second.created_snapshot_count == 0
    assert second.unchanged_count == 1


@pytest.mark.asyncio
async def test_collect_article_retries_timeout_then_succeeds() -> None:
    registry = load_source_registry()
    sleep = RecordingSleep()
    fetcher = FakeFetcher(
        TransientTransportError("timeout"),
        HttpResponse(status_code=200, body=_fixture("anthropic_context_100k.html")),
    )
    report = await collect_official_articles(
        source=registry.get("anthropic_news"),
        urls=[_URL_100K],
        policy=registry.collection_policy,
        fetcher=fetcher,
        session_factory=noop_session_factory(),  # type: ignore[arg-type]
        repository_factory=lambda _session: InMemorySourceItemRepository(),
        sleep=sleep,
    )

    assert report.status is ArticleCollectionStatus.OK
    assert report.fetch_attempts == 2
    assert len(sleep.delays) == 1


@pytest.mark.asyncio
async def test_collect_article_reports_malformed_page_without_persisting() -> None:
    registry = load_source_registry()
    report = await collect_official_articles(
        source=registry.get("anthropic_news"),
        urls=[_URL_100K],
        policy=registry.collection_policy,
        fetcher=FakeFetcher(HttpResponse(status_code=200, body=_fixture("no_date.html"))),
        session_factory=noop_session_factory(),  # type: ignore[arg-type]
        repository_factory=lambda _session: InMemorySourceItemRepository(),
    )

    assert report.status is ArticleCollectionStatus.FAILED
    assert report.processed_count == 0
    assert report.failures[0].category is ArticleFailureCategory.PARSE_FAILED


@pytest.mark.asyncio
async def test_collect_article_rejects_off_allowlist_canonical_url() -> None:
    registry = load_source_registry()
    body = _fixture("anthropic_context_100k.html").replace(
        b"https://www.anthropic.com/news/100k-context-windows",
        b"https://example.com/copied-article",
    )
    report = await collect_official_articles(
        source=registry.get("anthropic_news"),
        urls=[_URL_100K],
        policy=registry.collection_policy,
        fetcher=FakeFetcher(HttpResponse(status_code=200, body=body)),
        session_factory=noop_session_factory(),  # type: ignore[arg-type]
        repository_factory=lambda _session: InMemorySourceItemRepository(),
    )

    assert report.status is ArticleCollectionStatus.FAILED
    assert report.processed_count == 0
    assert report.failures[0].category is ArticleFailureCategory.NORMALIZE_FAILED


@pytest.mark.asyncio
async def test_fetch_failure_report_redacts_query_string() -> None:
    registry = load_source_registry()
    report = await collect_official_articles(
        source=registry.get("anthropic_news"),
        urls=[f"{_URL_100K}?token=secret"],
        policy=registry.collection_policy,
        fetcher=FakeFetcher(TransientTransportError("timeout")),
        session_factory=noop_session_factory(),  # type: ignore[arg-type]
        sleep=RecordingSleep(),
    )

    assert report.status is ArticleCollectionStatus.FAILED
    assert report.failures[0].url == _URL_100K
    assert "secret" not in report.failures[0].url
