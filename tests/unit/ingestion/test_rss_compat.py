"""`collect_openai_rss` compatibility wrapper (`ingestion/rss/collector.py`).

PR #71 exported `collect_openai_rss` with a keyword-only signature that
**already required** `source=`. This is now a real async wrapper (not a
Python alias) that keeps that exact signature and forwards every argument
to `collect_rss_source`. These tests prove the delegation, the argument
forwarding, and that non-RSS rejection is unchanged. No network, no
database."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ai_daily_digest.ingestion.persistence import IngestionWriteRepository
from ai_daily_digest.ingestion.rss import collector as collector_module
from ai_daily_digest.ingestion.rss.collector import (
    RSS_COLLECTOR_VERSION,
    CollectionReport,
    CollectionStatus,
    collect_openai_rss,
    collect_rss_source,
)
from ai_daily_digest.ingestion.rss.transport import HttpResponse
from ai_daily_digest.ingestion.sources import SourceDefinition, load_source_registry
from tests.unit.ingestion.fake_repository import InMemorySourceItemRepository
from tests.unit.ingestion.rss_helpers import (
    FakeFetcher,
    RecordingSleep,
    load_fixture,
    noop_session_factory,
    stepping_clock,
)

_REGISTRY = load_source_registry()
_OPENAI = _REGISTRY.get("openai_news")


def _repo_factory(
    repository: IngestionWriteRepository,
) -> Callable[[AsyncSession], IngestionWriteRepository]:
    return lambda _session: repository


def test_wrapper_keeps_the_pr71_keyword_only_signature() -> None:
    sig = inspect.signature(collect_openai_rss)
    params = sig.parameters
    # keyword-only, `source` required (no default) -- exactly PR #71.
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())
    assert params["source"].default is inspect.Parameter.empty
    assert params["policy"].default is inspect.Parameter.empty
    assert params["fetcher"].default is inspect.Parameter.empty
    assert params["session_factory"].default is inspect.Parameter.empty
    assert set(params) == set(inspect.signature(collect_rss_source).parameters)
    assert collect_openai_rss is not collect_rss_source  # a wrapper, not an alias


@pytest.mark.asyncio
async def test_wrapper_produces_the_same_report_as_the_generic_collector() -> None:
    def _kwargs() -> dict[str, object]:
        return {
            "source": _OPENAI,
            "policy": _REGISTRY.collection_policy,
            "fetcher": FakeFetcher(
                HttpResponse(status_code=200, body=load_fixture("openai_news_sample.xml"))
            ),
            "session_factory": noop_session_factory(),
            "repository_factory": _repo_factory(InMemorySourceItemRepository()),
            "clock": stepping_clock(start=datetime(2026, 9, 6, 12, 0, tzinfo=UTC)),
            "sleep": RecordingSleep(),
        }

    via_wrapper = await collect_openai_rss(**_kwargs())  # type: ignore[arg-type]
    via_generic = await collect_rss_source(**_kwargs())  # type: ignore[arg-type]
    assert via_wrapper == via_generic
    assert via_wrapper.status is CollectionStatus.OK
    assert via_wrapper.source_id == "openai_news"
    assert via_wrapper.created_item_count == 4


@pytest.mark.asyncio
async def test_wrapper_forwards_every_argument_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def _spy(**kwargs: object) -> CollectionReport:
        seen.update(kwargs)
        return CollectionReport(
            source_id="openai_news",
            started_at=datetime(2026, 9, 6, 12, 0, tzinfo=UTC),
            completed_at=datetime(2026, 9, 6, 12, 0, 1, tzinfo=UTC),
            status=CollectionStatus.OK,
            fetched_entry_count=0,
            created_item_count=0,
            created_snapshot_count=0,
            unchanged_count=0,
            failed_item_count=0,
            fetch_attempts=1,
        )

    monkeypatch.setattr(collector_module, "collect_rss_source", _spy)

    fetcher = FakeFetcher(HttpResponse(status_code=200, body=b"<rss/>"))
    session_factory = noop_session_factory()
    repo_factory = _repo_factory(InMemorySourceItemRepository())
    clock = stepping_clock()
    sleep = RecordingSleep()

    await collect_openai_rss(
        source=_OPENAI,
        policy=_REGISTRY.collection_policy,
        fetcher=fetcher,
        session_factory=session_factory,  # type: ignore[arg-type]
        repository_factory=repo_factory,
        clock=clock,
        sleep=sleep,
        collector_version="explicit/9.9",
    )

    assert seen == {
        "source": _OPENAI,
        "policy": _REGISTRY.collection_policy,
        "fetcher": fetcher,
        "session_factory": session_factory,
        "repository_factory": repo_factory,
        "clock": clock,
        "sleep": sleep,
        "collector_version": "explicit/9.9",
    }


@pytest.mark.asyncio
async def test_wrapper_defaults_collector_version_to_the_rss_adapter_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def _spy(**kwargs: object) -> CollectionReport:
        seen.update(kwargs)
        return CollectionReport(
            source_id="openai_news",
            started_at=datetime(2026, 9, 6, 12, 0, tzinfo=UTC),
            completed_at=datetime(2026, 9, 6, 12, 0, 1, tzinfo=UTC),
            status=CollectionStatus.OK,
            fetched_entry_count=0,
            created_item_count=0,
            created_snapshot_count=0,
            unchanged_count=0,
            failed_item_count=0,
            fetch_attempts=1,
        )

    monkeypatch.setattr(collector_module, "collect_rss_source", _spy)

    await collect_openai_rss(
        source=_OPENAI,
        policy=_REGISTRY.collection_policy,
        fetcher=FakeFetcher(HttpResponse(status_code=200, body=b"<rss/>")),
        session_factory=noop_session_factory(),  # type: ignore[arg-type]
    )

    assert seen["collector_version"] == RSS_COLLECTOR_VERSION == "rss/0.1.0"


@pytest.mark.asyncio
async def test_wrapper_still_rejects_a_non_rss_source() -> None:
    html_source = SourceDefinition.model_validate(
        {
            "id": "anthropic_news",
            "publisher": "Anthropic",
            "type": "html",
            "url": "https://www.anthropic.com/news",
            "priority": 0,
            "cadence_minutes": 360,
        }
    )
    with pytest.raises(ValueError, match="requires a source of type 'rss'"):
        await collect_openai_rss(
            source=html_source,
            policy=_REGISTRY.collection_policy,
            fetcher=FakeFetcher(HttpResponse(status_code=200, body=b"<rss/>")),
            session_factory=noop_session_factory(),  # type: ignore[arg-type]
        )
