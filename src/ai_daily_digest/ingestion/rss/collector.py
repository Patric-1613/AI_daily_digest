"""Collection orchestration for the OpenAI News RSS source.

Ties the transport, parser, and normalizer to the existing ingestion
service: fetch the feed once, parse it once, then persist each entry in
its **own transaction** (`ingest_document` owns that, ADR 0002 section
13) so one bad entry rolls back only itself and the remaining entries
still run (`docs/ARCHITECTURE.md`: "One source failure does not abort
others").

Returns a small, deterministic, machine-readable `CollectionReport` an
operator can act on -- counts plus structured, body-free failures.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from ai_daily_digest.ingestion.db.repository import PostgresSourceItemRepository
from ai_daily_digest.ingestion.persistence import IngestionWriteRepository
from ai_daily_digest.ingestion.rss.normalize import EntryNormalizationError, normalize_entry
from ai_daily_digest.ingestion.rss.parser import RssEntry, RssParseError, parse_rss
from ai_daily_digest.ingestion.rss.transport import (
    HttpFetcher,
    RetryOutcome,
    TransportError,
    fetch_with_retry,
)
from ai_daily_digest.ingestion.rss.url_policy import sanitize_url
from ai_daily_digest.ingestion.service import FetchedDocument, ingest_document
from ai_daily_digest.ingestion.sources import CollectionPolicy, SourceDefinition, SourceType

LOGGER = logging.getLogger(__name__)

COLLECTOR_VERSION = "openai-rss/0.1.0"

_SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
_RepositoryFactory = Callable[[AsyncSession], IngestionWriteRepository]


class CollectionStatus(StrEnum):
    """Overall outcome for one collection run.

    - ``ok``: the feed was fetched and parsed and every entry persisted.
    - ``partial``: the feed was fetched and parsed but at least one
      entry failed to normalize or persist (siblings still ran).
    - ``failed``: the feed itself could not be fetched or parsed -- no
      entry was processed.
    """

    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class EntryFailure:
    """One entry that could not be collected. `reason` is a short,
    structured string -- it never contains the response body, the
    normalized content, credentials, or an exception's bound SQL
    parameters."""

    raw_index: int
    guid: str | None
    link: str | None
    reason: str


def _entry_failure(entry: RssEntry, reason: str) -> EntryFailure:
    """An `EntryFailure` for `entry` with its `link` and `guid` passed
    through `sanitize_url` first -- a feed link or permalink guid can
    carry a query-string credential, which must not reach a report."""
    return EntryFailure(
        raw_index=entry.raw_index,
        guid=sanitize_url(entry.guid) if entry.guid else None,
        link=sanitize_url(entry.link) if entry.link else None,
        reason=reason,
    )


@dataclass(frozen=True, slots=True)
class CollectionReport:  # pylint: disable=too-many-instance-attributes
    """The machine-readable result of one collection run. Deterministic
    under an injected `clock` (the two timestamps) -- every other field
    is a count or a structured failure."""

    source_id: str
    started_at: datetime
    completed_at: datetime
    status: CollectionStatus
    fetched_entry_count: int
    created_item_count: int
    created_snapshot_count: int
    unchanged_count: int
    failed_item_count: int
    fetch_attempts: int
    failures: tuple[EntryFailure, ...] = field(default_factory=tuple)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class _Tally:
    created_items: int = 0
    created_snapshots: int = 0
    unchanged: int = 0
    failures: list[EntryFailure] = field(default_factory=list)


async def collect_openai_rss(  # pylint: disable=too-many-arguments,too-many-locals
    *,
    source: SourceDefinition,
    policy: CollectionPolicy,
    fetcher: HttpFetcher,
    session_factory: _SessionFactory,
    repository_factory: _RepositoryFactory = PostgresSourceItemRepository,
    clock: Callable[[], datetime] = _utc_now,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    collector_version: str = COLLECTOR_VERSION,
) -> CollectionReport:
    """Run one collection pass over `source` (which must be
    `SourceType.RSS`). Never raises for an entry-level or feed-level
    problem -- every failure is captured in the returned report."""
    if source.type is not SourceType.RSS:
        raise ValueError(f"collect_openai_rss requires an RSS source, got {source.type}")

    started_at = clock()
    fetched_at = started_at
    outcome = RetryOutcome()

    try:
        response = await fetch_with_retry(
            fetcher,
            str(source.url),
            policy=policy,
            allowed_hosts=source.host_allowlist(),
            sleep=sleep,
            outcome=outcome,
        )
    except TransportError as exc:
        return _failed_report(
            source.id,
            started_at,
            clock(),
            outcome.attempts,
            EntryFailure(raw_index=-1, guid=None, link=None, reason=f"fetch failed: {exc}"),
        )

    try:
        feed = parse_rss(response.body)
    except RssParseError as exc:
        return _failed_report(
            source.id,
            started_at,
            clock(),
            outcome.attempts,
            EntryFailure(raw_index=-1, guid=None, link=None, reason=f"parse failed: {exc}"),
        )

    if not feed.entries:
        # A syntactically valid feed with zero <item>s is a source
        # anomaly, not a successful empty run: something upstream is
        # broken (a bad publish, a truncated CDN copy, a login wall that
        # still parsed). Never report OK for it.
        return _failed_report(
            source.id,
            started_at,
            clock(),
            outcome.attempts,
            EntryFailure(
                raw_index=-1,
                guid=None,
                link=None,
                reason="feed anomaly: RSS parsed but contains zero <item> elements",
            ),
        )

    tally = _Tally()
    for entry in feed.entries:
        try:
            document = normalize_entry(
                entry,
                source=source,
                first_fetched_at=fetched_at,
                fetched_at=fetched_at,
                collector_version=collector_version,
                feed_etag=response.etag,
                feed_last_modified=response.last_modified,
            )
        except EntryNormalizationError as exc:
            tally.failures.append(_entry_failure(entry, f"normalization failed: {exc}"))
            continue

        await _persist_entry(entry, document, session_factory, repository_factory, tally)

    completed_at = clock()
    status = CollectionStatus.PARTIAL if tally.failures else CollectionStatus.OK
    return CollectionReport(
        source_id=source.id,
        started_at=started_at,
        completed_at=completed_at,
        status=status,
        fetched_entry_count=len(feed.entries),
        created_item_count=tally.created_items,
        created_snapshot_count=tally.created_snapshots,
        unchanged_count=tally.unchanged,
        failed_item_count=len(tally.failures),
        fetch_attempts=outcome.attempts,
        failures=tuple(tally.failures),
    )


async def _persist_entry(
    entry: RssEntry,
    document: FetchedDocument,
    session_factory: _SessionFactory,
    repository_factory: _RepositoryFactory,
    tally: _Tally,
) -> None:
    try:
        async with session_factory() as session:
            repository = repository_factory(session)
            result = await ingest_document(session, repository, document)
        if result.item_created:
            tally.created_items += 1
        if result.snapshot_created:
            tally.created_snapshots += 1
        else:
            tally.unchanged += 1
    # One item's failure must never stop the run; ingest_document has
    # already rolled its own transaction back. The reason records only
    # the exception TYPE -- an exception's string can carry bound SQL
    # parameter values (including snapshot content), which must not leak.
    except Exception as exc:  # pylint: disable=broad-exception-caught
        LOGGER.warning(
            "rss entry persistence failed",
            extra={"raw_index": entry.raw_index, "exception_type": type(exc).__name__},
        )
        tally.failures.append(_entry_failure(entry, f"persistence failed ({type(exc).__name__})"))


def _failed_report(
    source_id: str,
    started_at: datetime,
    completed_at: datetime,
    fetch_attempts: int,
    failure: EntryFailure,
) -> CollectionReport:
    return CollectionReport(
        source_id=source_id,
        started_at=started_at,
        completed_at=completed_at,
        status=CollectionStatus.FAILED,
        fetched_entry_count=0,
        created_item_count=0,
        created_snapshot_count=0,
        unchanged_count=0,
        failed_item_count=1,
        fetch_attempts=fetch_attempts,
        failures=(failure,),
    )
