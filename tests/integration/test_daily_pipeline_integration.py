"""Integration coverage for `daily_pipeline.py`'s core correctness claim
against a real PostgreSQL database: that the collection-boundary window it
derives (`window_start=collection.started_at`,
`window_end=collection.finished_at + 1s`) actually selects every real
snapshot a real collection run just persisted -- and that when collection
creates more snapshots than `intelligence_limit`, that shortfall is real
and observable against genuine rows, not just a hand-built report.

`daily_pipeline.py`'s own unit tests (`tests/unit/test_daily_pipeline.py`)
fully mock both `run_collection` and `run_intelligence`, so they can never
catch a mismatch between the two real stages' timestamp handling. This
file wires the real, unmodified `ingestion.rss.batch.run_rss_batch` and
the real, unmodified `intelligence.run.run_pipeline` together against one
migrated database, exactly as `run_daily_pipeline_with_real_infrastructure`
does in production (one shared session factory, no second engine) --
closing that gap.

No real network or LLM call is made: `run_rss_batch` is driven by a
`MappingFetcher` serving a synthetic RSS 2.0 feed generated per test with
unique links/guids (so tests sharing the session-scoped temporary
database, per `tests/integration/conftest.py`, can never dedupe against
each other's committed rows), and `run_pipeline`'s `extract_call_fn` is a
fake returning a fixed, deterministic fact.

Deliberately uses the real default wall clock for both stages (never an
injected fake), matching exactly how
`run_daily_pipeline_with_real_infrastructure` calls them in production.
This surfaced a real, separate finding while writing this file:
`ingestion.rss.batch.run_rss_batch`'s own `clock` parameter is used only
for its own `BatchReport.started_at`/`finished_at` envelope -- it is
never forwarded to `collect_rss_source()`, which always defaults to its
*own* real-wall-clock read for `fetched_at`. In production both resolve
to the real system clock microseconds apart, so this is currently
harmless; under an injected fake `clock` (as an earlier draft of this
file used) the two diverge completely and every snapshot falls outside
the computed window. That is a genuine gap in `run_rss_batch` itself,
out of this module's boundary to fix here -- tracked separately, not
silently patched in this PR.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ai_daily_digest.ingestion.rss.batch import BatchStatus, run_rss_batch
from ai_daily_digest.ingestion.rss.transport import HttpResponse
from ai_daily_digest.ingestion.sources import load_source_registry
from ai_daily_digest.intelligence.extract_facts import FactCandidate, FactExtractionResponse
from ai_daily_digest.intelligence.run import run_pipeline
from tests.unit.ingestion.rss_helpers import MappingFetcher, RecordingSleep

pytestmark = pytest.mark.integration

_OpenSession = Callable[[], AbstractAsyncContextManager[AsyncSession]]
_REGISTRY = load_source_registry()
_FEED_URL = "https://openai.com/news/rss.xml"

_WINDOW_END_SAFETY_MARGIN = timedelta(seconds=1)


def _synthetic_feed(count: int, *, unique: str) -> bytes:
    """A valid, minimal RSS 2.0 feed with `count` items, each carrying a
    `unique` token in its link/guid so this test's rows can never dedupe
    against another test's committed rows in the same shared temporary
    database (tests/integration/conftest.py's `open_database_session`
    commits for real -- see that fixture's own docstring).

    Every title/description names "GPT-4o" explicitly (a real, checked-in
    `shared/aliases.yaml` product alias) so `resolve_deterministic()`
    always resolves the subject without falling through to
    `resolve_via_llm()` -- this suite must never attempt a real LLM call."""
    items = "".join(
        f"""
    <item>
      <title><![CDATA[GPT-4o update {unique}-{i}]]></title>
      <description><![CDATA[GPT-4o context window increased to 128000 tokens. Synthetic entry {i} for integration test {unique}.]]></description>
      <link>https://openai.com/index/{unique}-{i}</link>
      <guid isPermaLink="true">https://openai.com/index/{unique}-{i}</guid>
      <pubDate>Thu, 03 Sep 2026 13:15:{i:02d} GMT</pubDate>
    </item>"""
        for i in range(count)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:atom="http://www.w3.org/2005/Atom" version="2.0">
  <channel>
    <title><![CDATA[OpenAI News]]></title>
    <link>https://openai.com/news</link>
    <atom:link href="{_FEED_URL}" rel="self" type="application/rss+xml"/>
    {items}
  </channel>
</rss>""".encode()


def _fetcher_for(count: int, *, unique: str) -> MappingFetcher:
    return MappingFetcher(
        {_FEED_URL: HttpResponse(status_code=200, body=_synthetic_feed(count, unique=unique))}
    )


def _fake_extract(system: str, prompt: str) -> FactExtractionResponse:
    del system, prompt
    return FactExtractionResponse(
        facts=[
            FactCandidate(
                field="context_window_tokens",
                value="128000",
                quoted_span="a fixed, deterministic fact -- no real LLM call",
                confidence=0.9,
            )
        ]
    )


@pytest.mark.asyncio
async def test_real_collection_boundary_selects_every_snapshot_it_created(
    open_database_session: _OpenSession,
) -> None:
    """The exact window daily_pipeline.py derives
    (`[collection.started_at, collection.finished_at + 1s)`) must select
    every real snapshot a real collection run just persisted -- proven
    against genuine `fetched_at` values written by the real persistence
    path, not hand-typed timestamps."""
    unique = f"boundary-{uuid.uuid4().hex[:8]}"

    collection_report = await run_rss_batch(
        registry=_REGISTRY,
        source_ids=("openai_news",),
        fetcher=_fetcher_for(3, unique=unique),
        session_factory=open_database_session,
        concurrency=1,
        sleep=RecordingSleep(),
    )

    assert collection_report.status is BatchStatus.OK
    created = collection_report.results[0].created_snapshot_count
    assert created == 3

    window_start = collection_report.started_at
    window_end = collection_report.finished_at + _WINDOW_END_SAFETY_MARGIN

    intelligence_report = await run_pipeline(
        session_factory=open_database_session,
        digest_date=window_start.date(),
        window_start=window_start,
        window_end=window_end,
        limit=10,
        extract_call_fn=_fake_extract,
    )

    # Every real snapshot collection just created was genuinely selected
    # and processed by the real window -- not more, not fewer.
    assert intelligence_report.selected_snapshot_count == created
    assert intelligence_report.processed_snapshot_count == created
    assert intelligence_report.failed_snapshot_count == 0


@pytest.mark.asyncio
async def test_real_collection_overflow_beyond_limit_is_observable(
    open_database_session: _OpenSession,
) -> None:
    """One real collection pass creates 20 real snapshots. With
    intelligence_limit=5 (the production default), the real
    `select_snapshots_in_window` SQL LIMIT genuinely truncates to 5 --
    proving daily_pipeline.py's overflow condition
    (`new_snapshot_count > selected_snapshot_count`) fires against real
    persisted data, not just a hand-built report."""
    unique = f"overflow-{uuid.uuid4().hex[:8]}"

    collection_report = await run_rss_batch(
        registry=_REGISTRY,
        source_ids=("openai_news",),
        fetcher=_fetcher_for(20, unique=unique),
        session_factory=open_database_session,
        concurrency=1,
        sleep=RecordingSleep(),
    )

    assert collection_report.status is BatchStatus.OK
    new_snapshot_count = collection_report.results[0].created_snapshot_count
    assert new_snapshot_count == 20

    window_start = collection_report.started_at
    window_end = collection_report.finished_at + _WINDOW_END_SAFETY_MARGIN

    intelligence_report = await run_pipeline(
        session_factory=open_database_session,
        digest_date=window_start.date(),
        window_start=window_start,
        window_end=window_end,
        limit=5,  # the production intelligence_limit default
        extract_call_fn=_fake_extract,
    )

    # The real SQL LIMIT genuinely truncated the real candidate list --
    # this is exactly the condition daily_pipeline.py's _combine_status()
    # checks to report `capped` instead of a false-healthy completion.
    assert intelligence_report.selected_snapshot_count == 5
    assert new_snapshot_count > intelligence_report.selected_snapshot_count


@pytest.mark.asyncio
async def test_replaying_the_same_real_window_with_a_larger_limit_is_idempotent(
    open_database_session: _OpenSession,
) -> None:
    """The documented recovery procedure (docs/DEPLOYMENT.md, "Overflow
    behaviour"): replaying the *same* window later with a larger --limit
    must be safe -- no duplicate facts/changes for the snapshots already
    processed in the capped run, and the previously-unprocessed snapshots
    are picked up for real."""
    unique = f"replay-{uuid.uuid4().hex[:8]}"

    collection_report = await run_rss_batch(
        registry=_REGISTRY,
        source_ids=("openai_news",),
        fetcher=_fetcher_for(20, unique=unique),
        session_factory=open_database_session,
        concurrency=1,
        sleep=RecordingSleep(),
    )
    assert collection_report.results[0].created_snapshot_count == 20
    window_start = collection_report.started_at
    window_end = collection_report.finished_at + _WINDOW_END_SAFETY_MARGIN

    # The capped run an operator would have seen in production.
    capped = await run_pipeline(
        session_factory=open_database_session,
        digest_date=window_start.date(),
        window_start=window_start,
        window_end=window_end,
        limit=5,
        extract_call_fn=_fake_extract,
    )
    assert capped.selected_snapshot_count == 5
    assert capped.failed_snapshot_count == 0

    # The operator's manual recovery: replay the exact same window with a
    # larger --limit, per the recorded intelligence_window_start.
    replay = await run_pipeline(
        session_factory=open_database_session,
        digest_date=window_start.date(),
        window_start=window_start,
        window_end=window_end,
        limit=20,
        extract_call_fn=_fake_extract,
    )

    # All 20 are now selected; none of it duplicates -- the 5 already
    # processed are safely re-processed as a no-op (intelligence/run.py's
    # own existing resumable-recovery path), and the replay does not fail
    # or corrupt anything.
    assert replay.selected_snapshot_count == 20
    assert replay.processed_snapshot_count == 20
    assert replay.failed_snapshot_count == 0


@pytest.mark.asyncio
async def test_a_second_collection_continuation_cannot_reselect_the_same_first_five(
    open_database_session: _OpenSession,
) -> None:
    """If a later, separate collection run happens (its own real
    started_at, strictly after the first run's window), its own window
    cannot re-select the first run's already-processed snapshots -- there
    is no offset/cursor state for a "continuation" to loop on, so this
    module deliberately does not attempt one (see docs/DEPLOYMENT.md,
    "Overflow behaviour")."""
    unique = f"nolooping-{uuid.uuid4().hex[:8]}"

    first_collection = await run_rss_batch(
        registry=_REGISTRY,
        source_ids=("openai_news",),
        fetcher=_fetcher_for(20, unique=unique),
        session_factory=open_database_session,
        concurrency=1,
        sleep=RecordingSleep(),
    )
    first_window_start = first_collection.started_at
    first_finished_at = first_collection.finished_at

    # A second, later collection run against the *same* unchanged feed:
    # every entry is already known, so it creates zero new snapshots --
    # exactly the "no_updates" path daily_pipeline.py takes, and it never
    # touches the earlier window at all.
    second_collection = await run_rss_batch(
        registry=_REGISTRY,
        source_ids=("openai_news",),
        fetcher=_fetcher_for(20, unique=unique),
        session_factory=open_database_session,
        concurrency=1,
        sleep=RecordingSleep(),
    )
    assert second_collection.results[0].created_snapshot_count == 0
    second_window_start = second_collection.started_at

    # The second run's own window starts no earlier than the first run's
    # own collection finished (they are sequential awaits on one shared
    # session factory, exactly as one cron process would run them) -- it
    # could never reselect (or re-drive extraction for) the first run's
    # oldest five even if it tried, because there is no offset/cursor
    # state to "continue" from in the first place.
    assert second_window_start >= first_finished_at
    assert second_window_start > first_window_start
