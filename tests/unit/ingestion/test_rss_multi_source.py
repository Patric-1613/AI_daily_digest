"""Day 4: the RSS collector is source-neutral.

`collect_rss_source` runs the same tested adapter for every configured
`SourceType.RSS` source (openai_news, langchain_pypi, langgraph_pypi).
These offline tests prove selection is not hardcoded to OpenAI, the
per-source host allowlist is applied, non-RSS input is rejected, and the
PyPI feeds normalize correctly. No network, no database."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ai_daily_digest.ingestion.persistence import IngestionWriteRepository
from ai_daily_digest.ingestion.rss.collector import (
    CollectionReport,
    CollectionStatus,
    collect_openai_rss,
    collect_rss_source,
)
from ai_daily_digest.ingestion.rss.normalize import dedupe_key
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

_RSS_SOURCES = ("openai_news", "langchain_pypi", "langgraph_pypi")
_SAMPLE_FIXTURE = {
    "openai_news": "openai_news_sample.xml",
    "langchain_pypi": "pypi_langchain_sample.xml",
    "langgraph_pypi": "pypi_langgraph_sample.xml",
}
_EXPECTED_ENTRIES = {"openai_news": 4, "langchain_pypi": 3, "langgraph_pypi": 3}


def _repo_factory(
    repository: IngestionWriteRepository,
) -> Callable[[AsyncSession], IngestionWriteRepository]:
    return lambda _session: repository


async def _collect(
    source_id: str,
    fixture: str,
    repository: InMemorySourceItemRepository,
) -> CollectionReport:
    return await collect_rss_source(
        source=_REGISTRY.get(source_id),
        policy=_REGISTRY.collection_policy,
        fetcher=FakeFetcher(HttpResponse(status_code=200, body=load_fixture(fixture))),
        session_factory=noop_session_factory(),  # type: ignore[arg-type]
        repository_factory=_repo_factory(repository),
        clock=stepping_clock(start=datetime(2026, 9, 6, 12, 0, tzinfo=UTC)),
        sleep=RecordingSleep(),
    )


# -- the generic adapter works for every configured RSS source ----------


@pytest.mark.asyncio
@pytest.mark.parametrize("source_id", _RSS_SOURCES)
async def test_generic_adapter_collects_each_configured_rss_source(source_id: str) -> None:
    repository = InMemorySourceItemRepository()
    report = await _collect(source_id, _SAMPLE_FIXTURE[source_id], repository)

    assert report.source_id == source_id
    assert report.status is CollectionStatus.OK
    assert report.fetched_entry_count == _EXPECTED_ENTRIES[source_id]
    assert report.created_item_count == _EXPECTED_ENTRIES[source_id]
    assert report.created_snapshot_count == _EXPECTED_ENTRIES[source_id]
    assert report.failed_item_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("source_id", _RSS_SOURCES)
async def test_the_fetch_uses_the_selected_source_url_and_allowlist(source_id: str) -> None:
    source = _REGISTRY.get(source_id)
    fetcher = FakeFetcher(
        HttpResponse(status_code=200, body=load_fixture(_SAMPLE_FIXTURE[source_id]))
    )
    await collect_rss_source(
        source=source,
        policy=_REGISTRY.collection_policy,
        fetcher=fetcher,
        session_factory=noop_session_factory(),  # type: ignore[arg-type]
        repository_factory=_repo_factory(InMemorySourceItemRepository()),
        clock=stepping_clock(),
        sleep=RecordingSleep(),
    )

    assert fetcher.received_urls == [str(source.url)]
    assert fetcher.received_allowed_hosts[0] == source.host_allowlist()


def test_collect_openai_rss_is_a_backwards_compatible_alias() -> None:
    assert collect_openai_rss is collect_rss_source


# -- non-RSS input is rejected clearly ---------------------------------


@pytest.mark.asyncio
async def test_collect_rss_source_rejects_a_non_rss_source_definition() -> None:
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
        await collect_rss_source(
            source=html_source,
            policy=_REGISTRY.collection_policy,
            fetcher=FakeFetcher(HttpResponse(status_code=200, body=b"<rss/>")),
            session_factory=noop_session_factory(),  # type: ignore[arg-type]
        )


# -- per-source host allowlist is applied independently ----------------


@pytest.mark.asyncio
async def test_openai_feed_entries_are_rejected_under_the_pypi_allowlist() -> None:
    """Feeding OpenAI's feed body while the *source* is `langchain_pypi`:
    every entry link is on openai.com, off the pypi.org allowlist -> the
    whole run is PARTIAL with no items created (each entry failed its
    source policy check)."""
    repository = InMemorySourceItemRepository()
    report = await _collect("langchain_pypi", "openai_news_sample.xml", repository)

    assert report.status is CollectionStatus.PARTIAL
    assert report.created_item_count == 0
    assert report.failed_item_count == 4
    assert all("allowlist" in f.reason for f in report.failures)


@pytest.mark.asyncio
async def test_pypi_feed_entries_are_rejected_under_the_openai_allowlist() -> None:
    repository = InMemorySourceItemRepository()
    report = await _collect("openai_news", "pypi_langchain_sample.xml", repository)

    assert report.status is CollectionStatus.PARTIAL
    assert report.created_item_count == 0
    assert report.failed_item_count == 3


@pytest.mark.asyncio
async def test_off_domain_pypi_entry_fails_while_siblings_persist_no_false_success() -> None:
    repository = InMemorySourceItemRepository()
    report = await _collect("langchain_pypi", "pypi_langchain_offsite_entry.xml", repository)

    assert report.status is CollectionStatus.PARTIAL  # never a false OK
    assert report.fetched_entry_count == 3
    assert report.created_item_count == 2
    assert report.failed_item_count == 1
    assert report.failures[0].raw_index == 1
    joined = f"{report.failures[0].reason} {report.failures[0].link} {report.failures[0].guid}"
    assert "token=abc123" not in joined
    assert "abc123" not in joined


# -- PyPI entries normalize correctly --------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_id", "fixture", "project_prefix"),
    [
        ("langchain_pypi", "pypi_langchain_sample.xml", "https://pypi.org/project/langchain/"),
        ("langgraph_pypi", "pypi_langgraph_sample.xml", "https://pypi.org/project/langgraph/"),
    ],
)
async def test_pypi_entries_normalize_with_correct_identity(
    source_id: str, fixture: str, project_prefix: str
) -> None:
    repository = InMemorySourceItemRepository()
    await _collect(source_id, fixture, repository)

    items = list(await repository.list_source_items(limit=10))
    assert len(items) == 3
    assert {item.source_id for item in items} == {source_id}
    assert {item.publisher for item in items} == {"Python Package Index"}
    assert {item.language for item in items} == {"en"}
    for item in items:
        canonical = str(item.canonical_url)
        assert canonical.startswith(project_prefix)
        assert not canonical.endswith("/")  # trailing slash canonicalised away
        assert item.dedupe_key == dedupe_key(canonical)
        assert item.published_at is not None and item.published_at.tzinfo is not None
        assert list(item.tags) == []
        assert list(item.authors) == []
    # The registry keeps the human subject even though it is not persisted.
    assert _REGISTRY.get(source_id).subject in {"LangChain", "LangGraph"}


@pytest.mark.asyncio
async def test_pypi_changed_release_note_creates_a_new_snapshot_only() -> None:
    repository = InMemorySourceItemRepository()
    await _collect("langchain_pypi", "pypi_langchain_sample.xml", repository)

    changed = await _collect("langchain_pypi", "pypi_langchain_changed.xml", repository)

    assert changed.created_item_count == 0
    assert changed.created_snapshot_count == 1
    assert changed.unchanged_count == 2
    assert changed.status is CollectionStatus.OK


# -- unsafe XML stays rejected regardless of which source drives it -----


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fixture",
    ["openai_news_plain_dtd.xml", "openai_news_with_entity.xml", "openai_news_malformed_xml.xml"],
)
async def test_unsafe_xml_is_a_source_level_failure_for_a_pypi_source(fixture: str) -> None:
    repository = InMemorySourceItemRepository()
    report = await _collect("langchain_pypi", fixture, repository)

    assert report.status is CollectionStatus.FAILED
    assert report.fetched_entry_count == 0
    assert report.created_item_count == 0
    assert "parse failed" in report.failures[0].reason
