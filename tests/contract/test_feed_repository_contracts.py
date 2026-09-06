"""Contract tests for shared feed repository protocols — ADR 0008 & ADR 0011."""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock

import pytest

from ai_daily_digest.ingestion.db.repository import PostgresSourceItemRepository
from ai_daily_digest.intelligence.db.repository import (
    PostgresChangeFeedRepository,
    PostgresDigestFeedRepository,
    PostgresFactStore,
)
from ai_daily_digest.shared.repositories import (
    ChangeFeedFilter,
    ChangeFeedRepository,
    DigestFeedFilter,
    DigestFeedRepository,
    FeedFilter,
    SourceItemFeedRepository,
)

pytestmark = pytest.mark.contract


def test_source_item_feed_repository_protocol_conformance() -> None:
    """SourceItemFeedRepository is runtime checkable and implemented by PostgresSourceItemRepository."""
    mock_session = AsyncMock()
    repo = PostgresSourceItemRepository(mock_session)
    assert isinstance(repo, SourceItemFeedRepository)

    sig = inspect.signature(SourceItemFeedRepository.list_source_items)
    params = list(sig.parameters.keys())
    assert "feed_filter" in params
    assert "after" in params
    assert "limit" in params


def test_change_feed_repository_protocol_conformance() -> None:
    """ChangeFeedRepository is runtime checkable and implemented by PostgresFactStore."""
    mock_session = AsyncMock()
    repo = PostgresFactStore(mock_session)
    assert isinstance(repo, ChangeFeedRepository)
    assert isinstance(PostgresChangeFeedRepository(mock_session), ChangeFeedRepository)

    sig = inspect.signature(ChangeFeedRepository.list_changes)
    params = list(sig.parameters.keys())
    assert "feed_filter" in params
    assert "after" in params
    assert "limit" in params


def test_digest_feed_repository_protocol_conformance() -> None:
    """DigestFeedRepository is runtime checkable and implemented by PostgresFactStore."""
    mock_session = AsyncMock()
    repo = PostgresFactStore(mock_session)
    assert isinstance(repo, DigestFeedRepository)
    assert isinstance(PostgresDigestFeedRepository(mock_session), DigestFeedRepository)

    sig = inspect.signature(DigestFeedRepository.list_digests)
    params = list(sig.parameters.keys())
    assert "feed_filter" in params
    assert "after" in params
    assert "limit" in params


def test_feed_filters_are_frozen_dataclasses() -> None:
    """Feed filters must be frozen dataclasses per ADR 0008."""
    f1 = FeedFilter(publisher="OpenAI", source_id="openai_news")
    with pytest.raises((AttributeError, TypeError)):
        f1.publisher = "Anthropic"  # type: ignore[misc]

    f2 = ChangeFeedFilter(company_key="openai", product_key="gpt4o", field="context_window_tokens")
    with pytest.raises((AttributeError, TypeError)):
        f2.company_key = "anthropic"  # type: ignore[misc]

    f3 = DigestFeedFilter()
    with pytest.raises((AttributeError, TypeError)):
        f3.date_from = None  # type: ignore[misc]
