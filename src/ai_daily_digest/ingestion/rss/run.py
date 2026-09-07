"""Production composition root for one OpenAI News RSS collection run.

`collect-openai-rss` (see `[project.scripts]`), or
`python -m ai_daily_digest.ingestion.rss.run`:

1. load `sources.yaml` via `load_source_registry()`;
2. select the `openai_news` source and the shared `collection_policy`;
3. read `DATABASE_URL` through `shared.config.DatabaseConfig.from_env()`
   (no direct `os.environ` access here);
4. build the process's one engine + session factory with
   `shared.db.build_engine` / `build_session_factory` -- this module
   creates **no** engine, metadata, session maker, or migration of its
   own (ADR 0002 section 12.3);
5. create a real `HttpxFetcher`;
6. run `collect_openai_rss`;
7. print the `CollectionReport` as one line of JSON to stdout and exit
   `0` (ok) / `1` (partial) / `2` (failed / could not start).

`run_collection()` takes every collaborator as an argument, so the
offline unit tests drive it with a fake fetcher and an in-memory session
factory and never touch a socket or a database.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ai_daily_digest.ingestion.db.repository import PostgresSourceItemRepository
from ai_daily_digest.ingestion.persistence import IngestionWriteRepository
from ai_daily_digest.ingestion.rss.collector import (
    CollectionReport,
    CollectionStatus,
    collect_openai_rss,
)
from ai_daily_digest.ingestion.rss.transport import HttpFetcher, HttpxFetcher
from ai_daily_digest.ingestion.sources import SourceRegistry, load_source_registry
from ai_daily_digest.shared.config import DatabaseConfig
from ai_daily_digest.shared.db import build_engine, build_session_factory

LOGGER = logging.getLogger(__name__)

SOURCE_ID = "openai_news"

_SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
_RepositoryFactory = Callable[[AsyncSession], IngestionWriteRepository]

_EXIT_CODE_BY_STATUS = {
    CollectionStatus.OK: 0,
    CollectionStatus.PARTIAL: 1,
    CollectionStatus.FAILED: 2,
}
_START_FAILURE_EXIT_CODE = 2


def render_report(report: CollectionReport) -> str:
    """`report` as a single line of JSON: sorted keys, ISO-8601
    timestamps, `EntryFailure`s inlined. Safe to print -- every URL the
    collector puts in a report is already `sanitize_url`-redacted."""
    payload = {
        "source_id": report.source_id,
        "status": report.status.value,
        "started_at": report.started_at.isoformat(),
        "completed_at": report.completed_at.isoformat(),
        "fetched_entry_count": report.fetched_entry_count,
        "created_item_count": report.created_item_count,
        "created_snapshot_count": report.created_snapshot_count,
        "unchanged_count": report.unchanged_count,
        "failed_item_count": report.failed_item_count,
        "fetch_attempts": report.fetch_attempts,
        "failures": [dataclasses.asdict(failure) for failure in report.failures],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def exit_code_for(report: CollectionReport) -> int:
    """`0` ok, `1` partial, `2` failed -- a shell/cron caller can branch
    on the process result without parsing the JSON."""
    return _EXIT_CODE_BY_STATUS[report.status]


async def run_collection(
    *,
    fetcher: HttpFetcher,
    session_factory: _SessionFactory,
    repository_factory: _RepositoryFactory = PostgresSourceItemRepository,
    registry: SourceRegistry | None = None,
) -> CollectionReport:
    """The offline-testable core. Loads `sources.yaml` (unless `registry`
    is supplied), selects `openai_news`, and runs one collection pass
    with the injected fetcher, session factory, and repository factory
    (the last defaults to the real `PostgresSourceItemRepository`; an
    offline test passes an in-memory fake)."""
    resolved = registry if registry is not None else load_source_registry()
    source = resolved.get(SOURCE_ID)
    return await collect_openai_rss(
        source=source,
        policy=resolved.collection_policy,
        fetcher=fetcher,
        session_factory=session_factory,
        repository_factory=repository_factory,
    )


async def _preflight(engine: AsyncEngine) -> None:
    """One bounded `SELECT 1` so a misconfigured or unreachable database
    fails fast and clearly, instead of surfacing as every entry failing
    to persist."""
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


async def collect_with_real_infrastructure(fetcher: HttpFetcher | None = None) -> CollectionReport:
    """Build the process's engine + session factory from `DATABASE_URL`
    (via `DatabaseConfig.from_env`), preflight it, run one collection,
    and always dispose the engine. `fetcher` defaults to a real
    `HttpxFetcher`; the integration test passes a fake so it can exercise
    this real-infrastructure path without the live network."""
    config = DatabaseConfig.from_env()
    engine = build_engine(config)
    try:
        await _preflight(engine)
        session_factory = build_session_factory(engine)
        return await run_collection(
            fetcher=fetcher if fetcher is not None else HttpxFetcher(),
            session_factory=session_factory,
        )
    finally:
        await engine.dispose()


def main() -> int:
    """`collect-openai-rss` entrypoint. Never raises: a startup failure
    (no `DATABASE_URL`, database unreachable) is reported as JSON on
    stdout and exit `2`, the same channel as a collection failure."""
    logging.basicConfig(level=logging.INFO)
    try:
        report = asyncio.run(collect_with_real_infrastructure())
    except (ValueError, OSError, SQLAlchemyError) as exc:
        # Only the exception TYPE is reported -- a ValueError from
        # `DatabaseConfig.from_env` or a `SQLAlchemyError` from the
        # preflight can carry the DSN (host, port, database name) in its
        # message, which must never reach stdout or a log line.
        LOGGER.error("openai-rss collection could not start: %s", type(exc).__name__)
        print(
            json.dumps(
                {"source_id": SOURCE_ID, "status": "failed", "error": type(exc).__name__},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return _START_FAILURE_EXIT_CODE
    print(render_report(report))
    return exit_code_for(report)


if __name__ == "__main__":
    raise SystemExit(main())
