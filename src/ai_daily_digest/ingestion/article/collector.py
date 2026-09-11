"""Collect explicitly selected first-party article URLs through guarded ingestion."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from ai_daily_digest.ingestion.article.normalize import normalize_article
from ai_daily_digest.ingestion.article.parser import ArticleParseError, parse_article
from ai_daily_digest.ingestion.db.repository import PostgresSourceItemRepository
from ai_daily_digest.ingestion.persistence import IngestionWriteRepository
from ai_daily_digest.ingestion.rss.normalize import EntryNormalizationError
from ai_daily_digest.ingestion.rss.transport import (
    HttpFetcher,
    RetryOutcome,
    TransportError,
    fetch_with_retry,
)
from ai_daily_digest.ingestion.rss.url_policy import sanitize_url
from ai_daily_digest.ingestion.service import ingest_document
from ai_daily_digest.ingestion.sources import CollectionPolicy, SourceDefinition

LOGGER = logging.getLogger(__name__)

ARTICLE_COLLECTOR_VERSION = "official-article/0.1.0"
ARTICLE_ACCEPT = "text/html, application/xhtml+xml"
HTML_MEDIA_TYPES = frozenset({"text/html", "application/xhtml+xml"})

_SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
_RepositoryFactory = Callable[[AsyncSession], IngestionWriteRepository]


class ArticleCollectionStatus(StrEnum):
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"


class ArticleFailureCategory(StrEnum):
    FETCH_FAILED = "fetch_failed"
    PARSE_FAILED = "parse_failed"
    NORMALIZE_FAILED = "normalize_failed"
    PERSISTENCE_FAILED = "persistence_failed"


@dataclass(frozen=True, slots=True)
class ArticleFailure:
    index: int
    url: str
    category: ArticleFailureCategory


@dataclass(frozen=True, slots=True)
class ArticleCollectionReport:  # pylint: disable=too-many-instance-attributes
    source_id: str
    started_at: datetime
    completed_at: datetime
    status: ArticleCollectionStatus
    requested_count: int
    processed_count: int
    created_item_count: int
    created_snapshot_count: int
    unchanged_count: int
    failed_item_count: int
    fetch_attempts: int
    failures: tuple[ArticleFailure, ...] = field(default_factory=tuple)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class _Tally:
    processed: int = 0
    created_items: int = 0
    created_snapshots: int = 0
    unchanged: int = 0
    fetch_attempts: int = 0
    failures: list[ArticleFailure] = field(default_factory=list)


def _failure(index: int, url: str, category: ArticleFailureCategory) -> ArticleFailure:
    return ArticleFailure(index=index, url=sanitize_url(url), category=category)


async def collect_official_articles(  # pylint: disable=too-many-arguments,too-many-locals
    *,
    source: SourceDefinition,
    urls: Sequence[str],
    policy: CollectionPolicy,
    fetcher: HttpFetcher,
    session_factory: _SessionFactory,
    repository_factory: _RepositoryFactory = PostgresSourceItemRepository,
    clock: Callable[[], datetime] = _utc_now,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    collector_version: str = ARTICLE_COLLECTOR_VERSION,
) -> ArticleCollectionReport:
    """Fetch and persist selected URLs sequentially, preserving baseline order."""
    if not urls:
        raise ValueError("at least one article URL is required")
    started_at = clock()
    tally = _Tally()

    for index, url in enumerate(urls):
        fetched_at = clock()
        outcome = RetryOutcome()
        try:
            response = await fetch_with_retry(
                fetcher,
                url,
                policy=policy,
                allowed_hosts=source.host_allowlist(),
                accept=ARTICLE_ACCEPT,
                sleep=sleep,
                outcome=outcome,
            )
        except TransportError:
            tally.fetch_attempts += outcome.attempts
            tally.failures.append(_failure(index, url, ArticleFailureCategory.FETCH_FAILED))
            continue
        tally.fetch_attempts += outcome.attempts

        try:
            article = parse_article(response.body)
        except ArticleParseError:
            tally.failures.append(_failure(index, url, ArticleFailureCategory.PARSE_FAILED))
            continue
        try:
            document = normalize_article(
                article,
                requested_url=url,
                source=source,
                fetched_at=fetched_at,
                collector_version=collector_version,
                etag=response.etag,
                last_modified=response.last_modified,
            )
        except EntryNormalizationError:
            tally.failures.append(_failure(index, url, ArticleFailureCategory.NORMALIZE_FAILED))
            continue

        try:
            async with session_factory() as session:
                result = await ingest_document(session, repository_factory(session), document)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            LOGGER.warning(
                "article persistence failed",
                extra={"index": index, "exception_type": type(exc).__name__},
            )
            tally.failures.append(_failure(index, url, ArticleFailureCategory.PERSISTENCE_FAILED))
            continue

        tally.processed += 1
        tally.created_items += int(result.item_created)
        tally.created_snapshots += int(result.snapshot_created)
        tally.unchanged += int(not result.snapshot_created)

    if len(tally.failures) == len(urls):
        status = ArticleCollectionStatus.FAILED
    elif tally.failures:
        status = ArticleCollectionStatus.PARTIAL
    else:
        status = ArticleCollectionStatus.OK
    return ArticleCollectionReport(
        source_id=source.id,
        started_at=started_at,
        completed_at=clock(),
        status=status,
        requested_count=len(urls),
        processed_count=tally.processed,
        created_item_count=tally.created_items,
        created_snapshot_count=tally.created_snapshots,
        unchanged_count=tally.unchanged,
        failed_item_count=len(tally.failures),
        fetch_attempts=tally.fetch_attempts,
        failures=tuple(tally.failures),
    )
