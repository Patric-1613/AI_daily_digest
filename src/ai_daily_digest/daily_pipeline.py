"""Combined daily ingestion -> intelligence orchestration for the single
production Render cron.

`run-daily-digest` (see `[project.scripts]`), or
`python -m ai_daily_digest.daily_pipeline`:

1. collect the verified production RSS sources (`openai_news`,
   `langchain_pypi`, `langgraph_pypi`, or exactly the `--source-id` values
   given) through the existing, unmodified `ingestion.rss.batch.run_rss_batch`
   -- same retries, host allowlists, deduplication, and per-source failure
   isolation as `collect-rss-batch`;
2. record the *real* collection start/finish boundary (not
   `--previous-complete-utc-day` / a fixed lookback -- see "Why not just
   chain the two CLIs" below);
3. when collection created at least one new snapshot, select exactly that
   window through the existing, unmodified `intelligence.run.run_pipeline`,
   capped at five snapshots;
4. combine both stages into one secret-safe JSON report and exit
   `0` (published, clean zero-change, or a healthy no-updates day) /
   `1` (partial collection, or partial/review intelligence) /
   `2` (collection configuration/total failure, or fatal intelligence
   failure).

## Why not just chain the two CLIs

`collect-rss-batch && generate-digest --previous-complete-utc-day` looks
equivalent but is not: snapshots collection just created carry
`fetched_at = now` (today, in whatever partial hour the cron happens to run
in), while `--previous-complete-utc-day` selects *yesterday's* whole UTC
day. A same-run snapshot would never be selected by that window, so the
freshly collected evidence would be silently skipped every single day. This
module fixes that by using the collection stage's own recorded
`started_at`/`finished_at` as the intelligence selection window instead of
any date arithmetic.

## Reused, unmodified building blocks

- `ingestion.rss.batch.run_rss_batch` -- one call, exactly as
  `collect-rss-batch` uses it. Its own dedup/idempotency/host-allowlist/
  retry/per-source-isolation behaviour is untouched.
- `intelligence.run.run_pipeline` and `intelligence.run.exit_code_for` --
  the exact same functions `generate-digest` calls, including PR #111's
  clean-zero-change classification. This module never re-implements or
  weakens any evidence, citation, or publication gate.

Both are invoked here as plain Python functions (typed keyword arguments),
never as a subprocess whose human-readable stdout would need parsing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ai_daily_digest.ingestion.rss.batch import (
    BatchReport,
    BatchStatus,
    render_batch_report,
    resolve_batch_sources,
    run_rss_batch,
)
from ai_daily_digest.ingestion.rss.run import SourceSelectionError
from ai_daily_digest.ingestion.rss.transport import HttpFetcher, HttpxFetcher
from ai_daily_digest.ingestion.sources import SourceRegistry, load_source_registry
from ai_daily_digest.intelligence.run import (
    DigestRunReport,
    exit_code_for,
    render_report,
    run_pipeline,
)
from ai_daily_digest.shared.config import DatabaseConfig
from ai_daily_digest.shared.db import build_engine, build_session_factory
from ai_daily_digest.shared.ids import new_id

LOGGER = logging.getLogger(__name__)

_SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
_RunCollection = Callable[..., Awaitable[BatchReport]]
_RunIntelligence = Callable[..., Awaitable[DigestRunReport]]

# The verified production RSS source ids -- mirrors
# `ingestion.rss.batch._VERIFIED_RSS_SOURCE_IDS`. Kept as this module's own
# public constant rather than importing that private name (modules must not
# import another module's private implementation).
PRODUCTION_RSS_SOURCE_IDS: tuple[str, ...] = ("openai_news", "langchain_pypi", "langgraph_pypi")

_DEFAULT_CONCURRENCY = 3
_MIN_CONCURRENCY = 1
_MAX_CONCURRENCY = 8

# Deliberate cost control, matching the current cron's `--limit 5`: each run
# processes at most five snapshots. Raising this is a separate,
# cost-approved change, not something this module decides on its own.
_INTELLIGENCE_SNAPSHOT_LIMIT = 5

# A snapshot's `fetched_at` is set by the collector strictly between our own
# pre- and post-collection clock reads. This margin only guards against a
# same-microsecond tie with the half-open window's exclusive upper bound; it
# never widens the window enough to pull in a snapshot from a previous run
# (those already have an earlier `fetched_at` than this run's own
# `window_start`).
_WINDOW_END_SAFETY_MARGIN = timedelta(seconds=1)

_START_FAILURE_EXIT_CODE = 2


def _utc_now() -> datetime:
    return datetime.now(UTC)


class CombinedPipelineStatus(StrEnum):
    """Overall outcome of one combined collection + intelligence run."""

    PUBLISHED = "published"
    CLEAN_ZERO_CHANGE = "clean_zero_change"
    NO_UPDATES = "no_updates"
    REVIEW = "review"
    PARTIAL = "partial"
    FAILED = "failed"


_EXIT_CODE_BY_COMBINED_STATUS: dict[CombinedPipelineStatus, int] = {
    CombinedPipelineStatus.PUBLISHED: 0,
    CombinedPipelineStatus.CLEAN_ZERO_CHANGE: 0,
    CombinedPipelineStatus.NO_UPDATES: 0,
    CombinedPipelineStatus.REVIEW: 1,
    CombinedPipelineStatus.PARTIAL: 1,
    CombinedPipelineStatus.FAILED: 2,
}


@dataclass(frozen=True, slots=True)
class DailyPipelineReport:  # pylint: disable=too-many-instance-attributes
    """The machine-readable result of one combined pipeline invocation."""

    job_run_id: uuid.UUID
    started_at: datetime
    finished_at: datetime
    status: CombinedPipelineStatus
    intelligence_window_start: datetime | None
    intelligence_window_end: datetime | None
    collection: BatchReport | None
    intelligence: DigestRunReport | None


def daily_pipeline_exit_code(report: DailyPipelineReport) -> int:
    """`0` published/clean-zero-change/no-updates, `1` partial/review,
    `2` failed -- a cron caller branches on the process result without
    parsing JSON."""
    return _EXIT_CODE_BY_COMBINED_STATUS[report.status]


def render_daily_pipeline_report(report: DailyPipelineReport) -> str:
    """`report` as one line of sorted-key JSON. The nested `collection` and
    `intelligence` objects are produced by round-tripping through the
    existing safe renderers (`render_batch_report` / `render_report`)
    rather than re-serializing the dataclasses by hand, so this can never
    leak a field either of those renderers doesn't already treat as safe --
    no URL, article text, prompt, or database credential."""
    payload: dict[str, object] = {
        "job_run_id": str(report.job_run_id),
        "started_at": report.started_at.isoformat(),
        "finished_at": report.finished_at.isoformat(),
        "status": report.status.value,
        "intelligence_window_start": (
            report.intelligence_window_start.isoformat()
            if report.intelligence_window_start is not None
            else None
        ),
        "intelligence_window_end": (
            report.intelligence_window_end.isoformat()
            if report.intelligence_window_end is not None
            else None
        ),
        "collection": (
            json.loads(render_batch_report(report.collection))
            if report.collection is not None
            else None
        ),
        "intelligence": (
            json.loads(render_report(report.intelligence))
            if report.intelligence is not None
            else None
        ),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _combine_status(
    collection_status: BatchStatus,
    intelligence_report: DigestRunReport,
) -> CombinedPipelineStatus:
    """The combined outcome-semantics table, built entirely from the two
    stages' own existing status vocabularies. Reuses `exit_code_for`
    (PR #111) rather than re-deriving zero-change/failure logic."""
    intel_exit = exit_code_for(intelligence_report)
    if intel_exit == 2:
        return CombinedPipelineStatus.FAILED
    if collection_status is BatchStatus.PARTIAL:
        # A partial collection is always surfaced, even when intelligence
        # itself would have been clean or published -- the cron operator
        # must not see a "healthy" exit code while a source is failing.
        return CombinedPipelineStatus.PARTIAL
    if intel_exit == 1:
        return (
            CombinedPipelineStatus.REVIEW
            if intelligence_report.status == "review"
            else CombinedPipelineStatus.PARTIAL
        )
    return (
        CombinedPipelineStatus.PUBLISHED
        if intelligence_report.status == "published"
        else CombinedPipelineStatus.CLEAN_ZERO_CHANGE
    )


async def run_daily_pipeline(  # pylint: disable=too-many-arguments,too-many-locals
    *,
    registry: SourceRegistry,
    session_factory: _SessionFactory,
    fetcher: HttpFetcher,
    source_ids: Sequence[str] = PRODUCTION_RSS_SOURCE_IDS,
    concurrency: int = _DEFAULT_CONCURRENCY,
    intelligence_limit: int = _INTELLIGENCE_SNAPSHOT_LIMIT,
    title: str | None = None,
    clock: Callable[[], datetime] = _utc_now,
    job_run_id: uuid.UUID | None = None,
    run_collection: _RunCollection = run_rss_batch,
    run_intelligence: _RunIntelligence = run_pipeline,
) -> DailyPipelineReport:
    """The offline-testable composition core. Takes both stages as
    injectable collaborators (defaulting to the real, unmodified
    `run_rss_batch` / `run_pipeline`) so tests drive it with fakes and never
    touch the network, a database, or an LLM.

    Intelligence is only invoked when collection produced at least one new
    snapshot -- a healthy zero-updates day never touches the digest tables
    at all, so no empty digest can ever be published.
    """
    started_at = clock()
    job_id = job_run_id if job_run_id is not None else new_id()

    collection_report = await run_collection(
        registry=registry,
        source_ids=source_ids,
        fetcher=fetcher,
        session_factory=session_factory,
        concurrency=concurrency,
        clock=clock,
    )

    if collection_report.status is BatchStatus.FAILED:
        return DailyPipelineReport(
            job_run_id=job_id,
            started_at=started_at,
            finished_at=clock(),
            status=CombinedPipelineStatus.FAILED,
            intelligence_window_start=None,
            intelligence_window_end=None,
            collection=collection_report,
            intelligence=None,
        )

    new_snapshot_count = sum(result.created_snapshot_count for result in collection_report.results)
    if new_snapshot_count == 0:
        combined_status = (
            CombinedPipelineStatus.NO_UPDATES
            if collection_report.status is BatchStatus.OK
            else CombinedPipelineStatus.PARTIAL
        )
        return DailyPipelineReport(
            job_run_id=job_id,
            started_at=started_at,
            finished_at=clock(),
            status=combined_status,
            intelligence_window_start=None,
            intelligence_window_end=None,
            collection=collection_report,
            intelligence=None,
        )

    # The real collection boundary -- not `--previous-complete-utc-day`, not
    # a fixed lookback. Every snapshot this run created has `fetched_at`
    # strictly between these two instants.
    window_start = collection_report.started_at
    window_end = collection_report.finished_at + _WINDOW_END_SAFETY_MARGIN
    digest_date = started_at.date()

    intelligence_report = await run_intelligence(
        session_factory=session_factory,
        digest_date=digest_date,
        window_start=window_start,
        window_end=window_end,
        limit=intelligence_limit,
        title=title,
        clock=clock,
    )

    return DailyPipelineReport(
        job_run_id=job_id,
        started_at=started_at,
        finished_at=clock(),
        status=_combine_status(collection_report.status, intelligence_report),
        intelligence_window_start=window_start,
        intelligence_window_end=window_end,
        collection=collection_report,
        intelligence=intelligence_report,
    )


async def _preflight(engine: AsyncEngine) -> None:
    """Fail fast if the database is misconfigured or unreachable. Mirrors
    `ingestion.rss.batch`/`intelligence.run`'s own trivial `_preflight` --
    not shared logic, just the same one-line pattern each composition root
    defines for itself."""
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


async def run_daily_pipeline_with_real_infrastructure(  # pylint: disable=too-many-arguments
    *,
    source_ids: Sequence[str] = PRODUCTION_RSS_SOURCE_IDS,
    concurrency: int = _DEFAULT_CONCURRENCY,
    intelligence_limit: int = _INTELLIGENCE_SNAPSHOT_LIMIT,
    title: str | None = None,
    fetcher: HttpFetcher | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> DailyPipelineReport:
    """Build the process's one engine + session factory from `DATABASE_URL`,
    preflight it, run one combined pass, and always dispose the engine.
    Both collection and intelligence share this single engine/session
    factory -- no second engine is created for one invocation."""
    config = DatabaseConfig.from_env()
    registry = load_source_registry()
    engine = build_engine(config)
    try:
        await _preflight(engine)
        session_factory = build_session_factory(engine)
        return await run_daily_pipeline(
            registry=registry,
            session_factory=session_factory,
            fetcher=fetcher if fetcher is not None else HttpxFetcher(),
            source_ids=source_ids,
            concurrency=concurrency,
            intelligence_limit=intelligence_limit,
            title=title,
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


def _positive_int(value: str) -> int:
    try:
        ival = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid integer value: {value!r}") from exc
    if ival <= 0:
        raise argparse.ArgumentTypeError(f"Limit must be strictly positive (> 0), got: {ival}")
    return ival


@dataclass(frozen=True, slots=True)
class _Args:
    source_ids: tuple[str, ...]
    concurrency: int
    intelligence_limit: int
    title: str | None


def _parse_args(argv: Sequence[str] | None) -> _Args:
    parser = argparse.ArgumentParser(
        prog="run-daily-digest",
        description=(
            "Collect the verified production RSS sources and run the intelligence "
            "pipeline over exactly the snapshots that collection just created. "
            "With no --source-id, runs the verified set "
            "(openai_news, langchain_pypi, langgraph_pypi). "
            "Exit 0 = published, clean zero-change, or a healthy no-updates day; "
            "1 = partial collection or partial/review intelligence; "
            "2 = collection configuration/total failure or fatal intelligence failure."
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
    parser.add_argument(
        "--limit",
        type=_positive_int,
        default=_INTELLIGENCE_SNAPSHOT_LIMIT,
        help=(
            "maximum snapshots the intelligence stage processes "
            f"(must be > 0, default {_INTELLIGENCE_SNAPSHOT_LIMIT})"
        ),
    )
    parser.add_argument(
        "--title",
        default=None,
        help="Optional custom title for the generated digest",
    )
    namespace = parser.parse_args(argv)
    selected = tuple(namespace.source_ids) if namespace.source_ids else PRODUCTION_RSS_SOURCE_IDS
    return _Args(
        source_ids=selected,
        concurrency=namespace.concurrency,
        intelligence_limit=namespace.limit,
        title=namespace.title,
    )


def _emit_start_failure(exc: Exception) -> int:
    # Only the exception TYPE is machine-reported -- `DatabaseConfig.from_env`
    # and the preflight can carry the DSN in their messages.
    LOGGER.error("run-daily-digest could not start: %s", type(exc).__name__)
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
    """`run-daily-digest` entrypoint. Never raises: an argument error exits
    via argparse; an empty/unknown/non-RSS selection, a missing
    `DATABASE_URL`, or an unreachable database is reported as JSON on
    stdout with exit `2`."""
    logging.basicConfig(level=logging.INFO)
    args = _parse_args(argv)

    try:
        # Fail fast on selection errors -- no DATABASE_URL required to
        # discover an id is wrong (mirrors collect-rss-batch's own main()).
        resolve_batch_sources(load_source_registry(), args.source_ids)
    except (SourceSelectionError, OSError, ValueError) as exc:
        return _emit_start_failure(exc)

    try:
        report = asyncio.run(
            run_daily_pipeline_with_real_infrastructure(
                source_ids=args.source_ids,
                concurrency=args.concurrency,
                intelligence_limit=args.intelligence_limit,
                title=args.title,
            )
        )
    except (SourceSelectionError, ValueError, OSError, SQLAlchemyError) as exc:
        return _emit_start_failure(exc)

    print(render_daily_pipeline_report(report))
    return daily_pipeline_exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
