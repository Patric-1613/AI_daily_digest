"""Production composition root for one RSS-source collection run.

`collect-rss --source-id <id>` (see `[project.scripts]`), or
`python -m ai_daily_digest.ingestion.rss.run --source-id <id>`:

1. load `sources.yaml` via `load_source_registry()`;
2. resolve `--source-id` and require `type: rss` (an unknown id or a
   non-RSS type is a clear failure with exit 2, before any database
   connection);
3. read `DATABASE_URL` through `shared.config.DatabaseConfig.from_env()`
   (no direct `os.environ` access here);
4. build the process's one engine + session factory with
   `shared.db.build_engine` / `build_session_factory` -- this module
   creates **no** engine, metadata, session maker, or migration of its
   own (ADR 0002 section 12.3);
5. create a real `HttpxFetcher`;
6. run `collect_rss_source`;
7. print the `CollectionReport` as one line of JSON to stdout and exit
   `0` (ok) / `1` (partial) / `2` (failed / could not start).

`collect-openai-rss` is kept as a thin compatibility command equivalent
to `collect-rss --source-id openai_news`.

`run_collection()` takes every collaborator as an argument, so the
offline unit tests drive it with a fake fetcher and an in-memory session
factory and never touch a socket or a database. This is a single-run
command, not a scheduler -- it never reads `cadence_minutes`.
"""

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

from ai_daily_digest.ingestion.db.repository import PostgresSourceItemRepository
from ai_daily_digest.ingestion.persistence import IngestionWriteRepository
from ai_daily_digest.ingestion.rss.collector import (
    CollectionReport,
    CollectionStatus,
    collect_rss_source,
)
from ai_daily_digest.ingestion.rss.transport import HttpFetcher, HttpxFetcher
from ai_daily_digest.ingestion.sources import (
    SourceDefinition,
    SourceRegistry,
    SourceType,
    load_source_registry,
)
from ai_daily_digest.shared.config import DatabaseConfig
from ai_daily_digest.shared.db import build_engine, build_session_factory

LOGGER = logging.getLogger(__name__)

_OPENAI_NEWS_SOURCE_ID = "openai_news"

_SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
_RepositoryFactory = Callable[[AsyncSession], IngestionWriteRepository]

_EXIT_CODE_BY_STATUS = {
    CollectionStatus.OK: 0,
    CollectionStatus.PARTIAL: 1,
    CollectionStatus.FAILED: 2,
}
_START_FAILURE_EXIT_CODE = 2


class SourceSelectionError(Exception):
    """The `--source-id` names no source, or names one whose type is not
    `rss`. A configuration/operator error, distinct from a collection
    failure -- reported the same way (JSON + exit 2) but detected before
    any database connection."""


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


def resolve_rss_source(registry: SourceRegistry, source_id: str) -> SourceDefinition:
    """The one `SourceDefinition` for `source_id`, proven to be an RSS
    source. Raises `SourceSelectionError` for an unknown id or a non-RSS
    type -- the message names the id and the offending type but carries
    no secret."""
    try:
        source = registry.get(source_id)
    except KeyError as exc:
        raise SourceSelectionError(f"no source with id {source_id!r} in sources.yaml") from exc
    if source.type is not SourceType.RSS:
        raise SourceSelectionError(
            f"source {source_id!r} has type {source.type.value!r}, not 'rss' -- "
            f"no RSS collector applies to it"
        )
    return source


async def run_collection(
    *,
    source_id: str,
    fetcher: HttpFetcher,
    session_factory: _SessionFactory,
    repository_factory: _RepositoryFactory = PostgresSourceItemRepository,
    registry: SourceRegistry | None = None,
) -> CollectionReport:
    """The offline-testable core. Loads `sources.yaml` (unless `registry`
    is supplied), resolves `source_id` to a validated RSS
    `SourceDefinition`, and runs one collection pass with the injected
    fetcher, session factory, and repository factory (the last defaults
    to the real `PostgresSourceItemRepository`; an offline test passes an
    in-memory fake). Raises `SourceSelectionError` for an unknown or
    non-RSS `source_id`."""
    resolved = registry if registry is not None else load_source_registry()
    source = resolve_rss_source(resolved, source_id)
    return await collect_rss_source(
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


async def collect_with_real_infrastructure(
    source_id: str, fetcher: HttpFetcher | None = None
) -> CollectionReport:
    """Build the process's engine + session factory from `DATABASE_URL`
    (via `DatabaseConfig.from_env`), preflight it, run one collection of
    `source_id`, and always dispose the engine. `fetcher` defaults to a
    real `HttpxFetcher`; the integration test passes a fake so it can
    exercise this real-infrastructure path without the live network."""
    config = DatabaseConfig.from_env()
    engine = build_engine(config)
    try:
        await _preflight(engine)
        session_factory = build_session_factory(engine)
        return await run_collection(
            source_id=source_id,
            fetcher=fetcher if fetcher is not None else HttpxFetcher(),
            session_factory=session_factory,
        )
    finally:
        await engine.dispose()


def _parse_args(argv: Sequence[str] | None) -> str:
    parser = argparse.ArgumentParser(
        prog="collect-rss",
        description="Run one collection pass over a single RSS source from sources.yaml.",
    )
    parser.add_argument(
        "--source-id",
        required=True,
        help="a source id from sources.yaml whose type is 'rss' (e.g. openai_news, langchain_pypi)",
    )
    source_id: str = parser.parse_args(argv).source_id
    return source_id


def _emit_failure(source_id: str, exc: Exception) -> int:
    # Only the exception TYPE is machine-reported. A `ValueError` from
    # `DatabaseConfig.from_env` or a `SQLAlchemyError` from the preflight
    # can carry the DSN (host, port, database name); a `SourceSelectionError`
    # message is safe but is still logged at the type level for consistency.
    LOGGER.error("rss collection could not start for %r: %s", source_id, type(exc).__name__)
    if isinstance(exc, SourceSelectionError):
        LOGGER.error("%s", exc)
    print(
        json.dumps(
            {"source_id": source_id, "status": "failed", "error": type(exc).__name__},
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return _START_FAILURE_EXIT_CODE


def main(argv: Sequence[str] | None = None) -> int:
    """`collect-rss` entrypoint. Never raises: an argument error exits via
    argparse; an unknown/non-RSS source id, a missing `DATABASE_URL`, or
    an unreachable database is reported as JSON on stdout and exit `2`."""
    logging.basicConfig(level=logging.INFO)
    source_id = _parse_args(argv)

    try:
        # Fail fast on selection errors -- no DATABASE_URL required to
        # find out the id is wrong.
        resolve_rss_source(load_source_registry(), source_id)
    except (SourceSelectionError, OSError, ValueError) as exc:
        return _emit_failure(source_id, exc)

    try:
        report = asyncio.run(collect_with_real_infrastructure(source_id))
    except (SourceSelectionError, ValueError, OSError, SQLAlchemyError) as exc:
        return _emit_failure(source_id, exc)

    print(render_report(report))
    return exit_code_for(report)


def main_openai_news(argv: Sequence[str] | None = None) -> int:
    """`collect-openai-rss` compatibility entrypoint. Kept because PR #71
    shipped `collect-openai-rss` as the documented command.

    It **always** collects `openai_news` and **never** any other source.
    It parses its own argument list with a parser that accepts **no**
    options (bar `-h`): `collect-openai-rss --source-id langchain_pypi`,
    or any other extra argument, exits with argparse code `2` **before**
    any registry, database, or network access -- it can never be
    redirected to a different source. With no extra arguments it invokes
    the generic path for `openai_news`.

    `argv` defaults to `None` so a console-script call reads the real
    command line; tests pass an explicit sequence.
    """
    argparse.ArgumentParser(
        prog="collect-openai-rss",
        description=(
            "Compatibility command: one collection pass over the openai_news RSS source. "
            "Equivalent to `collect-rss --source-id openai_news`. Takes no arguments."
        ),
    ).parse_args(argv)
    return main(["--source-id", _OPENAI_NEWS_SOURCE_ID])


if __name__ == "__main__":
    raise SystemExit(main())
