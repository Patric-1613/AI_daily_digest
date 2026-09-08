"""Scheduler-friendly batch collection over the verified production RSS sources.

`collect-rss-batch` (see `[project.scripts]`), or
`python -m ai_daily_digest.ingestion.rss.batch`:

1. load `sources.yaml`;
2. choose the sources to run -- by default the verified production RSS
   source ids (`openai_news`, `langchain_pypi`, `langgraph_pypi`; see
   `src/ai_daily_digest/ingestion/README.md`), or exactly the
   `--source-id` values given for a controlled or manual run;
3. resolve every chosen id to a validated `type: rss` `SourceDefinition`
   **before any network or database work** -- an unknown or non-RSS id is
   a clear exit-2 failure;
4. read `DATABASE_URL` through `shared.config.DatabaseConfig`, build the
   one shared engine + session factory, and preflight it;
5. run each source through the existing `collect_rss_source` adapter under
   a bounded `asyncio.Semaphore` -- reusing its per-source timeout, retry,
   backoff, URL canonicalization, content hashing, snapshot deduplication
   and PostgreSQL persistence unchanged. One source failing never cancels
   the others;
6. print one structured, secret-safe JSON batch report and exit
   `0` (every selected source ok) / `1` (partial success) /
   `2` (configuration or selection failure, or every selected source
   failed).

This is the "one separately invoked job command" from
`docs/ARCHITECTURE.md` ("Scheduling"), safe for a cloud-cron worker. It is
**not** a cadence-aware `--due` scheduler: durable per-source last-attempt
state has no accepted schema yet (ADR 0002 section 7 defers
`collection_runs` to "the feature that needs it, in its own migration"),
so this command always runs the full selected set and relies on the
adapter's existing idempotent dedup for repeated runs.

`run_rss_batch()` takes every collaborator as an argument, so the offline
tests drive it with fake fetchers and an in-memory session factory and
never open a socket or touch a database.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ai_daily_digest.ingestion.db.repository import PostgresSourceItemRepository
from ai_daily_digest.ingestion.persistence import IngestionWriteRepository
from ai_daily_digest.ingestion.rss.collector import (
    RSS_COLLECTOR_VERSION,
    CollectionReport,
    CollectionStatus,
    collect_rss_source,
)
from ai_daily_digest.ingestion.rss.run import SourceSelectionError, resolve_rss_source
from ai_daily_digest.ingestion.rss.transport import HttpFetcher, HttpxFetcher
from ai_daily_digest.ingestion.sources import SourceDefinition, SourceRegistry, load_source_registry
from ai_daily_digest.shared.config import DatabaseConfig
from ai_daily_digest.shared.db import build_engine, build_session_factory
from ai_daily_digest.shared.ids import new_id

LOGGER = logging.getLogger(__name__)

_SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
_RepositoryFactory = Callable[[AsyncSession], IngestionWriteRepository]

# The production-verified RSS source ids. These are the sources that have
# been structurally preflighted against their live feed and given
# source-specific unit + PostgreSQL integration tests -- see the
# "Verified source IDs" section of
# `src/ai_daily_digest/ingestion/README.md`, which this tuple mirrors.
# Other `type: rss` entries in `sources.yaml` are accepted only through an
# explicit `--source-id`, never by default.
_VERIFIED_RSS_SOURCE_IDS: tuple[str, ...] = (
    "openai_news",
    "langchain_pypi",
    "langgraph_pypi",
)

_DEFAULT_CONCURRENCY = 3
_MIN_CONCURRENCY = 1
# A conservative ceiling: this command is a courteous batch crawler, not a
# load generator. Well above the verified set, low enough that an operator
# typo cannot fan out dozens of simultaneous source fetches.
_MAX_CONCURRENCY = 8

_START_FAILURE_EXIT_CODE = 2


class BatchStatus(StrEnum):
    """Overall outcome of one batch invocation.

    - ``ok``: every selected source finished ``ok``.
    - ``partial``: mixed -- at least one source did useful work and at
      least one did not (a ``partial`` source, or an ``ok``/``failed``
      split).
    - ``failed``: every selected source ``failed`` (startup failure is
      reported separately by ``main``).
    """

    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"


_EXIT_CODE_BY_STATUS = {
    BatchStatus.OK: 0,
    BatchStatus.PARTIAL: 1,
    BatchStatus.FAILED: 2,
}

# FAILED-report reason prefixes -> the fixed `error_category` token
# reported. Closed and content-free: no branch ever emits an exception
# string, a URL, or feed text.
_FAILED_REASON_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("fetch failed", "fetch_failed"),
    ("parse failed", "parse_failed"),
    ("feed anomaly", "empty_feed"),
)
_PARTIAL_CATEGORY = "partial_entry_failures"
_FALLBACK_FAILED_CATEGORY = "collection_failed"


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class SourceRunResult:  # pylint: disable=too-many-instance-attributes
    """One selected source's contribution to the batch report -- counts
    plus a fixed-vocabulary `error_category`. Deliberately not the
    underlying `CollectionReport`: its `failures` tuple can carry
    `sanitize_url`-redacted links, more than an operator needs in a
    multi-source summary."""

    source_id: str
    status: CollectionStatus
    fetched_entry_count: int
    created_item_count: int
    created_snapshot_count: int
    unchanged_count: int
    failed_item_count: int
    fetch_attempts: int
    error_category: str | None


@dataclass(frozen=True, slots=True)
class BatchReport:
    """The machine-readable result of one batch invocation. Deterministic
    under an injected clock and a fixed `job_run_id`."""

    job_run_id: uuid.UUID
    started_at: datetime
    finished_at: datetime
    requested_source_ids: tuple[str, ...]
    concurrency: int
    status: BatchStatus
    results: tuple[SourceRunResult, ...] = field(default_factory=tuple)


def error_category_for(report: CollectionReport) -> str | None:
    """A fixed-vocabulary category for a `CollectionReport`, or `None` when
    it is `ok`. Never derived from an exception message, a URL, or feed
    content -- only from the report's status and the *prefix* of its
    feed-level failure reason."""
    if report.status is CollectionStatus.OK:
        return None
    if report.status is CollectionStatus.PARTIAL:
        return _PARTIAL_CATEGORY
    reason = report.failures[0].reason if report.failures else ""
    for prefix, category in _FAILED_REASON_CATEGORIES:
        if reason.startswith(prefix):
            return category
    return _FALLBACK_FAILED_CATEGORY


def batch_status(results: Sequence[SourceRunResult]) -> BatchStatus:
    """`ok` when every source is `ok`; `failed` when every source is
    `failed`; `partial` for anything in between (including no results,
    which only a bug can produce -- selection is validated first)."""
    if not results:
        return BatchStatus.FAILED
    statuses = {result.status for result in results}
    if statuses == {CollectionStatus.OK}:
        return BatchStatus.OK
    if statuses == {CollectionStatus.FAILED}:
        return BatchStatus.FAILED
    return BatchStatus.PARTIAL


def batch_exit_code(report: BatchReport) -> int:
    """`0` all selected sources ok, `1` partial, `2` all selected failed --
    a cron caller branches on the process result without parsing JSON. A
    startup/config/selection failure is also `2` (emitted by `main`)."""
    return _EXIT_CODE_BY_STATUS[report.status]


def render_batch_report(report: BatchReport) -> str:
    """`report` as one line of sorted-key JSON. Safe to print: every value
    is an id, an ISO-8601 timestamp, a count, a status string, or a
    fixed-vocabulary `error_category`. There is no field that can hold a
    URL, a query string, feed content, or a raw exception."""
    sources = [
        {
            "source_id": result.source_id,
            "status": result.status.value,
            "fetched_entry_count": result.fetched_entry_count,
            "created_item_count": result.created_item_count,
            "created_snapshot_count": result.created_snapshot_count,
            "unchanged_count": result.unchanged_count,
            "failed_item_count": result.failed_item_count,
            "fetch_attempts": result.fetch_attempts,
            "error_category": result.error_category,
        }
        for result in report.results
    ]
    totals = {
        "requested": len(report.requested_source_ids),
        "succeeded": sum(1 for r in report.results if r.status is CollectionStatus.OK),
        "partial": sum(1 for r in report.results if r.status is CollectionStatus.PARTIAL),
        "failed": sum(1 for r in report.results if r.status is CollectionStatus.FAILED),
        # A successfully processed entry either produced a new snapshot
        # (a brand-new SourceItem, or an existing one whose content
        # changed) or was unchanged. Counting `created_item_count` instead
        # of `created_snapshot_count` would undercount a changed-content
        # rerun, where an existing item gains a snapshot but no new item.
        "items_processed": sum(
            r.created_snapshot_count + r.unchanged_count for r in report.results
        ),
        "new_snapshots": sum(r.created_snapshot_count for r in report.results),
        "unchanged": sum(r.unchanged_count for r in report.results),
        "failed_items": sum(r.failed_item_count for r in report.results),
    }
    payload: dict[str, object] = {
        "job_run_id": str(report.job_run_id),
        "started_at": report.started_at.isoformat(),
        "finished_at": report.finished_at.isoformat(),
        "requested_source_ids": list(report.requested_source_ids),
        "concurrency": report.concurrency,
        "status": report.status.value,
        "totals": totals,
        "sources": sources,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def resolve_batch_sources(
    registry: SourceRegistry, source_ids: Sequence[str]
) -> list[SourceDefinition]:
    """Every selected id resolved to a validated `type: rss`
    `SourceDefinition`, in first-seen order with duplicates dropped.
    Raises `SourceSelectionError` for an empty selection, an unknown id,
    or a non-RSS id -- all of it detected before any network or database
    work."""
    if not source_ids:
        raise SourceSelectionError("no sources selected for the batch run")
    resolved: list[SourceDefinition] = []
    seen: set[str] = set()
    for source_id in source_ids:
        if source_id in seen:
            continue
        seen.add(source_id)
        resolved.append(resolve_rss_source(registry, source_id))
    return resolved


def _result_from_report(report: CollectionReport, error_category: str | None) -> SourceRunResult:
    return SourceRunResult(
        source_id=report.source_id,
        status=report.status,
        fetched_entry_count=report.fetched_entry_count,
        created_item_count=report.created_item_count,
        created_snapshot_count=report.created_snapshot_count,
        unchanged_count=report.unchanged_count,
        failed_item_count=report.failed_item_count,
        fetch_attempts=report.fetch_attempts,
        error_category=error_category,
    )


def _unexpected_failure_result(source_id: str) -> SourceRunResult:
    return SourceRunResult(
        source_id=source_id,
        status=CollectionStatus.FAILED,
        fetched_entry_count=0,
        created_item_count=0,
        created_snapshot_count=0,
        unchanged_count=0,
        failed_item_count=0,
        fetch_attempts=0,
        error_category=_FALLBACK_FAILED_CATEGORY,
    )


async def run_rss_batch(  # pylint: disable=too-many-arguments,too-many-locals
    *,
    registry: SourceRegistry,
    source_ids: Sequence[str],
    fetcher: HttpFetcher,
    session_factory: _SessionFactory,
    repository_factory: _RepositoryFactory = PostgresSourceItemRepository,
    concurrency: int = _DEFAULT_CONCURRENCY,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], datetime] = _utc_now,
    collector_version: str = RSS_COLLECTOR_VERSION,
    job_run_id: uuid.UUID | None = None,
) -> BatchReport:
    """The offline-testable core. Resolves every selected id first (so a
    bad selection fails before any collaborator is touched), then runs
    each source through `collect_rss_source` under an
    `asyncio.Semaphore(concurrency)`. One source's failure -- captured in
    its report, or an unexpected raise -- never cancels the others.
    `clock` times only the batch envelope; per-source report timestamps
    are the adapter's own and are not surfaced here.

    `concurrency` must be in the inclusive range
    ``_MIN_CONCURRENCY``..``_MAX_CONCURRENCY`` (1..8) for a direct Python
    caller too, not just the CLI -- the same courteous-crawler ceiling."""
    if not _MIN_CONCURRENCY <= concurrency <= _MAX_CONCURRENCY:
        raise ValueError(
            f"concurrency must be between {_MIN_CONCURRENCY} and {_MAX_CONCURRENCY}, "
            f"got {concurrency}"
        )

    resolved = resolve_batch_sources(registry, source_ids)
    requested = tuple(dict.fromkeys(source_ids))
    job_id = job_run_id if job_run_id is not None else new_id()
    started_at = clock()
    semaphore = asyncio.Semaphore(concurrency)

    async def _run_one(source: SourceDefinition) -> SourceRunResult:
        async with semaphore:
            try:
                report = await collect_rss_source(
                    source=source,
                    policy=registry.collection_policy,
                    fetcher=fetcher,
                    session_factory=session_factory,
                    repository_factory=repository_factory,
                    sleep=sleep,
                    collector_version=collector_version,
                )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                # `collect_rss_source` captures every *collection* failure
                # in its report; reaching here is an unexpected fault. Log
                # the exception TYPE only -- its string can carry scraped
                # content, a DSN, or bound SQL parameter values.
                LOGGER.error(
                    "batch source collection raised unexpectedly",
                    extra={
                        "source_id": source.id,
                        "exception_type": type(exc).__name__,
                    },
                )
                return _unexpected_failure_result(source.id)
        return _result_from_report(report, error_category_for(report))

    results = await asyncio.gather(*(_run_one(source) for source in resolved))
    finished_at = clock()

    return BatchReport(
        job_run_id=job_id,
        started_at=started_at,
        finished_at=finished_at,
        requested_source_ids=requested,
        concurrency=concurrency,
        status=batch_status(results),
        results=tuple(results),
    )


async def _preflight(engine: AsyncEngine) -> None:
    """One bounded `SELECT 1` so a misconfigured or unreachable database
    fails fast and clearly at startup, not as every source failing to
    persist. Mirrors `ingestion/rss/run.py::_preflight` -- a trivial
    connectivity check, not shared collection logic."""
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


async def collect_batch_with_real_infrastructure(
    *,
    source_ids: Sequence[str],
    concurrency: int,
    fetcher: HttpFetcher | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> BatchReport:
    """Build the process's one engine + session factory from `DATABASE_URL`
    (via `DatabaseConfig.from_env`), preflight it, run one batch pass, and
    always dispose the engine. `fetcher` defaults to a real `HttpxFetcher`;
    the integration test passes a fake so it can exercise this
    real-infrastructure path without the live network. No second engine,
    session factory, or `MetaData` is created (ADR 0002 section 12.4)."""
    config = DatabaseConfig.from_env()
    registry = load_source_registry()
    engine = build_engine(config)
    try:
        await _preflight(engine)
        session_factory = build_session_factory(engine)
        return await run_rss_batch(
            registry=registry,
            source_ids=source_ids,
            fetcher=fetcher if fetcher is not None else HttpxFetcher(),
            session_factory=session_factory,
            concurrency=concurrency,
            clock=clock,
        )
    finally:
        await engine.dispose()


def _concurrency_arg(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"concurrency must be an integer, got {raw!r}") from None
    if not _MIN_CONCURRENCY <= value <= _MAX_CONCURRENCY:
        raise argparse.ArgumentTypeError(
            f"concurrency must be between {_MIN_CONCURRENCY} and {_MAX_CONCURRENCY}, got {value}"
        )
    return value


@dataclass(frozen=True, slots=True)
class _Args:
    source_ids: tuple[str, ...]
    concurrency: int


def _parse_args(argv: Sequence[str] | None) -> _Args:
    parser = argparse.ArgumentParser(
        prog="collect-rss-batch",
        description=(
            "Collect verified production RSS sources in one batch. With no --source-id, "
            "runs the verified set (openai_news, langchain_pypi, langgraph_pypi). "
            "Exit 0 = every source ok, 1 = partial, 2 = config/selection failure or total failure."
        ),
    )
    parser.add_argument(
        "--source-id",
        action="append",
        dest="source_ids",
        metavar="ID",
        help=(
            "an rss source id from sources.yaml; repeatable; when given, replaces the "
            "verified default set (for controlled or manual runs)"
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=_concurrency_arg,
        default=_DEFAULT_CONCURRENCY,
        help=(
            f"maximum sources fetched at once "
            f"({_MIN_CONCURRENCY}-{_MAX_CONCURRENCY}, default {_DEFAULT_CONCURRENCY})"
        ),
    )
    namespace = parser.parse_args(argv)
    selected = tuple(namespace.source_ids) if namespace.source_ids else _VERIFIED_RSS_SOURCE_IDS
    return _Args(source_ids=selected, concurrency=namespace.concurrency)


def _emit_start_failure(exc: Exception) -> int:
    # Only the exception TYPE is machine-reported. `DatabaseConfig.from_env`
    # and the preflight can carry the DSN in their messages; a
    # `SourceSelectionError` message names only ids/types (safe) but is
    # still reduced to its type for consistency.
    LOGGER.error("collect-rss-batch could not start: %s", type(exc).__name__)
    if isinstance(exc, SourceSelectionError):
        LOGGER.error("%s", exc)
    print(
        json.dumps(
            {"status": "failed", "error": type(exc).__name__},
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return _START_FAILURE_EXIT_CODE


def main(argv: Sequence[str] | None = None) -> int:
    """`collect-rss-batch` entrypoint. Never raises: an argument error
    exits via argparse; an empty/unknown/non-RSS selection, a missing
    `DATABASE_URL`, or an unreachable database is reported as JSON on
    stdout with exit `2`."""
    logging.basicConfig(level=logging.INFO)
    args = _parse_args(argv)

    try:
        # Fail fast on selection errors -- no DATABASE_URL required to
        # discover an id is wrong.
        resolve_batch_sources(load_source_registry(), args.source_ids)
    except (SourceSelectionError, OSError, ValueError) as exc:
        return _emit_start_failure(exc)

    try:
        report = asyncio.run(
            collect_batch_with_real_infrastructure(
                source_ids=args.source_ids, concurrency=args.concurrency
            )
        )
    except (SourceSelectionError, ValueError, OSError, SQLAlchemyError) as exc:
        return _emit_start_failure(exc)

    print(render_batch_report(report))
    return batch_exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
