"""Production composition root for guarded historical article collection."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ai_daily_digest.ingestion.article.collector import (
    HTML_MEDIA_TYPES,
    ArticleCollectionReport,
    ArticleCollectionStatus,
    collect_official_articles,
)
from ai_daily_digest.ingestion.db.repository import PostgresSourceItemRepository
from ai_daily_digest.ingestion.persistence import IngestionWriteRepository
from ai_daily_digest.ingestion.rss.transport import HttpFetcher, HttpxFetcher
from ai_daily_digest.ingestion.rss.url_policy import UnsafeUrlError, require_safe_url
from ai_daily_digest.ingestion.sources import (
    SourceDefinition,
    SourceRegistry,
    SourceType,
    load_source_registry,
)
from ai_daily_digest.shared.config import DatabaseConfig
from ai_daily_digest.shared.db import build_engine, build_session_factory

LOGGER = logging.getLogger(__name__)

_ARTICLE_SOURCE_TYPES = frozenset({SourceType.RSS, SourceType.HTML, SourceType.CHANGELOG})
_SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
_RepositoryFactory = Callable[[AsyncSession], IngestionWriteRepository]


class ArticleSelectionError(Exception):
    """A source id or requested URL is outside the reviewed registry boundary."""


def resolve_article_source(
    registry: SourceRegistry, source_id: str, urls: Sequence[str]
) -> SourceDefinition:
    try:
        source = registry.get(source_id)
    except KeyError as exc:
        raise ArticleSelectionError(f"no source with id {source_id!r} in sources.yaml") from exc
    if source.type not in _ARTICLE_SOURCE_TYPES:
        raise ArticleSelectionError(
            f"source {source_id!r} has unsupported type {source.type.value!r}"
        )
    if not urls:
        raise ArticleSelectionError("at least one --url is required")
    for url in urls:
        try:
            require_safe_url(url, allowed_hosts=source.host_allowlist())
        except UnsafeUrlError as exc:
            raise ArticleSelectionError(str(exc)) from exc
    return source


def render_report(report: ArticleCollectionReport) -> str:
    payload = {
        "source_id": report.source_id,
        "status": report.status.value,
        "started_at": report.started_at.isoformat(),
        "completed_at": report.completed_at.isoformat(),
        "requested_count": report.requested_count,
        "processed_count": report.processed_count,
        "created_item_count": report.created_item_count,
        "created_snapshot_count": report.created_snapshot_count,
        "unchanged_count": report.unchanged_count,
        "failed_item_count": report.failed_item_count,
        "fetch_attempts": report.fetch_attempts,
        "failures": [
            {**dataclasses.asdict(failure), "category": failure.category.value}
            for failure in report.failures
        ],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def exit_code_for(report: ArticleCollectionReport) -> int:
    return {
        ArticleCollectionStatus.OK: 0,
        ArticleCollectionStatus.PARTIAL: 1,
        ArticleCollectionStatus.FAILED: 2,
    }[report.status]


async def run_collection(  # pylint: disable=too-many-arguments
    *,
    source_id: str,
    urls: Sequence[str],
    fetcher: HttpFetcher,
    session_factory: _SessionFactory,
    repository_factory: _RepositoryFactory = PostgresSourceItemRepository,
    registry: SourceRegistry | None = None,
) -> ArticleCollectionReport:
    resolved = registry if registry is not None else load_source_registry()
    source = resolve_article_source(resolved, source_id, urls)
    return await collect_official_articles(
        source=source,
        urls=urls,
        policy=resolved.collection_policy,
        fetcher=fetcher,
        session_factory=session_factory,
        repository_factory=repository_factory,
    )


async def _preflight(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


async def collect_with_real_infrastructure(
    source_id: str, urls: Sequence[str], fetcher: HttpFetcher | None = None
) -> ArticleCollectionReport:
    config = DatabaseConfig.from_env()
    engine = build_engine(config)
    try:
        await _preflight(engine)
        return await run_collection(
            source_id=source_id,
            urls=urls,
            fetcher=fetcher
            if fetcher is not None
            else HttpxFetcher(approved_media_types=HTML_MEDIA_TYPES),
            session_factory=build_session_factory(engine),
        )
    finally:
        await engine.dispose()


def _parse_args(argv: Sequence[str] | None) -> tuple[str, list[str]]:
    parser = argparse.ArgumentParser(
        prog="collect-official-article",
        description="Collect ordered, explicitly selected first-party article URLs.",
    )
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--url", action="append", required=True, dest="urls")
    args = parser.parse_args(argv)
    return str(args.source_id), list(args.urls)


def _emit_failure(source_id: str, exc: Exception) -> int:
    LOGGER.error("article collection could not start for %r: %s", source_id, type(exc).__name__)
    if isinstance(exc, ArticleSelectionError):
        LOGGER.error("%s", exc)
    print(
        json.dumps(
            {"source_id": source_id, "status": "failed", "error": type(exc).__name__},
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    source_id, urls = _parse_args(argv)
    try:
        resolve_article_source(load_source_registry(), source_id, urls)
    except (ArticleSelectionError, OSError, ValueError) as exc:
        return _emit_failure(source_id, exc)
    try:
        report = asyncio.run(collect_with_real_infrastructure(source_id, urls))
    except (ArticleSelectionError, OSError, ValueError, SQLAlchemyError) as exc:
        return _emit_failure(source_id, exc)
    print(render_report(report))
    return exit_code_for(report)


if __name__ == "__main__":
    raise SystemExit(main())
