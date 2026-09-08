"""Unit and integration tests for GET /v1/updates — ADR 0008 PR 4."""

from __future__ import annotations

import unicodedata
import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import HttpUrl
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_daily_digest.delivery.api.app import create_app
from ai_daily_digest.delivery.api.errors import ErrorEnvelope
from ai_daily_digest.delivery.api.pagination import (
    CursorCodec,
    canonicalize_filters,
)
from ai_daily_digest.delivery.api.routes.updates import UPDATES_RESOURCE, UPDATES_SORT
from ai_daily_digest.delivery.api.schemas import UpdateSummary
from ai_daily_digest.shared.ids import new_id
from ai_daily_digest.shared.repositories import FeedFilter, SourceItemFeedRepository
from ai_daily_digest.shared.schemas import SourceItem

TEST_KEY = b"\x2a" * 32
BASE_TIME = datetime(2026, 9, 4, 12, 0, 0, 0, tzinfo=UTC)


class InMemorySourceItemFeedRepository:
    """In-memory fake repository implementing SourceItemFeedRepository for tests."""

    def __init__(self, items: Iterable[SourceItem] = ()) -> None:
        self._items = list(items)

    async def list_source_items(
        self,
        *,
        feed_filter: FeedFilter | None = None,
        after: tuple[datetime, uuid.UUID] | None = None,
        limit: int = 20,
    ) -> Sequence[SourceItem]:
        filtered = self._items
        if feed_filter is not None:
            if feed_filter.publisher is not None:
                filtered = [item for item in filtered if item.publisher == feed_filter.publisher]
            if feed_filter.source_id is not None:
                filtered = [item for item in filtered if item.source_id == feed_filter.source_id]

        ordered = sorted(filtered, key=lambda x: (x.first_fetched_at, x.id), reverse=True)

        if after is not None:
            after_ts, after_id = after
            ordered = [
                item for item in ordered if (item.first_fetched_at, item.id) < (after_ts, after_id)
            ]

        return ordered[: limit + 1]


class _ReadyProbe:
    async def is_ready(self) -> bool:
        return True


class _TrackedSession:
    def __init__(self, owner: _TrackedSessionFactory) -> None:
        self._owner = owner

    async def __aenter__(self) -> _TrackedSession:
        self._owner.entered += 1
        return self

    async def __aexit__(self, *_: object) -> None:
        self._owner.exited += 1


class _TrackedSessionFactory:
    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0
        self.created = 0

    def __call__(self) -> _TrackedSession:
        self.created += 1
        return _TrackedSession(self)


def _make_source_item(
    *,
    item_id: uuid.UUID | None = None,
    source_id: str = "openai_news",
    publisher: str = "OpenAI",
    title: str = "Test Title",
    canonical_url: str | HttpUrl = "https://example.com/item",
    first_fetched_at: datetime | None = None,
    published_at: datetime | None = None,
    event_id: str | None = None,
    tags: list[str] | None = None,
    language: str = "en",
    latest_snapshot_id: uuid.UUID | None = None,
) -> SourceItem:
    real_id = item_id or new_id()
    url = canonical_url if isinstance(canonical_url, HttpUrl) else HttpUrl(canonical_url)
    return SourceItem(
        id=real_id,
        dedupe_key=f"sha256:{real_id}",
        source_id=source_id,
        publisher=publisher,
        title=title,
        canonical_url=url,
        first_fetched_at=first_fetched_at or BASE_TIME,
        published_at=published_at,
        event_id=event_id,
        tags=tags or [],
        language=language,
        latest_snapshot_id=latest_snapshot_id,
    )


def test_get_updates_default_limit_and_projection() -> None:
    items = [
        _make_source_item(
            title=f"Article {i:02d}",
            first_fetched_at=BASE_TIME + timedelta(minutes=i),
        )
        for i in range(25)
    ]
    repo = InMemorySourceItemFeedRepository(items)
    client = TestClient(create_app(source_item_feed_repository=repo, cursor_signing_key=TEST_KEY))

    response = client.get("/v1/updates")
    assert response.status_code == 200
    data = response.json()

    assert "items" in data
    assert "next_cursor" in data
    assert len(data["items"]) == 20
    assert data["next_cursor"] is not None

    # Check that items are ordered descending
    first_item = data["items"][0]
    assert first_item["title"] == "Article 24"
    assert "dedupe_key" not in first_item

    # Verify each item validates as UpdateSummary
    for item_dict in data["items"]:
        summary = UpdateSummary.model_validate(item_dict)
        assert summary.title.startswith("Article ")


def test_get_updates_multi_page_traversal() -> None:
    items = [
        _make_source_item(
            title=f"Item {i}",
            first_fetched_at=BASE_TIME + timedelta(minutes=i),
        )
        for i in range(5)
    ]
    repo = InMemorySourceItemFeedRepository(items)
    client = TestClient(create_app(source_item_feed_repository=repo, cursor_signing_key=TEST_KEY))

    # Page 1 (limit=2)
    resp1 = client.get("/v1/updates?limit=2")
    assert resp1.status_code == 200
    p1 = resp1.json()
    assert [x["title"] for x in p1["items"]] == ["Item 4", "Item 3"]
    assert p1["next_cursor"] is not None

    # Page 2 (limit=2)
    resp2 = client.get(f"/v1/updates?limit=2&cursor={p1['next_cursor']}")
    assert resp2.status_code == 200
    p2 = resp2.json()
    assert [x["title"] for x in p2["items"]] == ["Item 2", "Item 1"]
    assert p2["next_cursor"] is not None

    # Page 3 (final page, limit=2)
    resp3 = client.get(f"/v1/updates?limit=2&cursor={p2['next_cursor']}")
    assert resp3.status_code == 200
    p3 = resp3.json()
    assert [x["title"] for x in p3["items"]] == ["Item 0"]
    assert p3["next_cursor"] is None

    # Page 4 (past end): synthesise a valid cursor pointing to the position of the final
    # returned item so the repository is asked for items strictly before it, returning []
    # without needing to reach into implementation details.
    codec = CursorCodec(TEST_KEY)
    canonical_filters = canonicalize_filters(
        resource=UPDATES_RESOURCE,
        sort=UPDATES_SORT,
        filters={},
    )
    last_item_json = p3["items"][-1]  # Item 0 — the oldest item returned on page 3
    past_end_cursor = codec.next_cursor(
        filters=canonical_filters,
        last_sort_value=datetime.fromisoformat(last_item_json["first_fetched_at"]),
        last_id=uuid.UUID(last_item_json["id"]),
    )
    resp_empty = client.get(f"/v1/updates?limit=2&cursor={past_end_cursor}")
    assert resp_empty.status_code == 200
    p_empty = resp_empty.json()
    assert p_empty["items"] == []
    assert p_empty["next_cursor"] is None


def test_get_updates_empty_repository() -> None:
    repo = InMemorySourceItemFeedRepository([])
    client = TestClient(create_app(source_item_feed_repository=repo, cursor_signing_key=TEST_KEY))

    response = client.get("/v1/updates")
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


def test_get_updates_exact_limit() -> None:
    items = [
        _make_source_item(
            title=f"Item {i}",
            first_fetched_at=BASE_TIME + timedelta(minutes=i),
        )
        for i in range(2)
    ]
    repo = InMemorySourceItemFeedRepository(items)
    client = TestClient(create_app(source_item_feed_repository=repo, cursor_signing_key=TEST_KEY))

    response = client.get("/v1/updates?limit=2")
    assert response.status_code == 200
    data = response.json()
    assert len(data["items"]) == 2
    assert data["next_cursor"] is None


def test_get_updates_equal_timestamp_id_tie_breaker() -> None:
    id_lower = uuid.UUID("019f1af9-0000-7000-8000-000000000001")
    id_higher = uuid.UUID("019f1af9-0000-7000-8000-000000000002")

    item_low = _make_source_item(item_id=id_lower, title="Lower ID", first_fetched_at=BASE_TIME)
    item_high = _make_source_item(item_id=id_higher, title="Higher ID", first_fetched_at=BASE_TIME)

    repo = InMemorySourceItemFeedRepository([item_low, item_high])
    client = TestClient(create_app(source_item_feed_repository=repo, cursor_signing_key=TEST_KEY))

    resp1 = client.get("/v1/updates?limit=1")
    assert resp1.status_code == 200
    data1 = resp1.json()
    assert len(data1["items"]) == 1
    assert data1["items"][0]["title"] == "Higher ID"
    assert data1["next_cursor"] is not None

    resp2 = client.get(f"/v1/updates?limit=1&cursor={data1['next_cursor']}")
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert len(data2["items"]) == 1
    assert data2["items"][0]["title"] == "Lower ID"
    assert data2["next_cursor"] is None


@pytest.mark.parametrize("invalid_limit", [0, 101, -1, "abc", ""])
def test_get_updates_invalid_limit(invalid_limit: object) -> None:
    repo = InMemorySourceItemFeedRepository([])
    client = TestClient(create_app(source_item_feed_repository=repo, cursor_signing_key=TEST_KEY))

    response = client.get(f"/v1/updates?limit={invalid_limit}")
    assert response.status_code == 422
    payload = ErrorEnvelope.model_validate(response.json())
    assert payload.error.code == "validation_error"


@pytest.mark.parametrize(
    "bad_cursor",
    [
        "not.a.cursor",
        "invalid_base64",
        "abc.def",
        "x" * 600,
        "",
    ],
)
def test_get_updates_invalid_cursor(bad_cursor: str) -> None:
    mock_repo = AsyncMock(spec=SourceItemFeedRepository)
    client = TestClient(
        create_app(source_item_feed_repository=mock_repo, cursor_signing_key=TEST_KEY)
    )

    response = client.get(f"/v1/updates?cursor={bad_cursor}")
    assert response.status_code == 400
    payload = ErrorEnvelope.model_validate(response.json())
    assert payload.error.code == "invalid_cursor"
    assert payload.error.message == "The pagination cursor is invalid for this request."
    mock_repo.list_source_items.assert_not_called()


def test_get_updates_canonical_filtering() -> None:
    item_openai = _make_source_item(publisher="OpenAI", source_id="openai_news")
    item_anthropic = _make_source_item(publisher="Anthropic", source_id="anthropic_news")

    repo = InMemorySourceItemFeedRepository([item_openai, item_anthropic])
    client = TestClient(create_app(source_item_feed_repository=repo, cursor_signing_key=TEST_KEY))

    # Exact filter
    r_openai = client.get("/v1/updates?publisher=OpenAI")
    assert r_openai.status_code == 200
    assert len(r_openai.json()["items"]) == 1
    assert r_openai.json()["items"][0]["publisher"] == "OpenAI"

    # Whitespace trimmed matches
    r_padded = client.get("/v1/updates?publisher=%20OpenAI%20")
    assert r_padded.status_code == 200
    assert len(r_padded.json()["items"]) == 1
    assert r_padded.json()["items"][0]["publisher"] == "OpenAI"

    # Case sensitive does not match
    r_lower = client.get("/v1/updates?publisher=openai")
    assert r_lower.status_code == 200
    assert len(r_lower.json()["items"]) == 0

    # NFC normalization match
    nfc_cafe = unicodedata.normalize("NFC", "Café")
    nfd_cafe = unicodedata.normalize("NFD", "Café")
    item_cafe = _make_source_item(publisher=nfc_cafe)
    repo_cafe = InMemorySourceItemFeedRepository([item_cafe])
    client_cafe = TestClient(
        create_app(source_item_feed_repository=repo_cafe, cursor_signing_key=TEST_KEY)
    )

    r_nfd = client_cafe.get(f"/v1/updates?publisher={nfd_cafe}")
    assert r_nfd.status_code == 200
    assert len(r_nfd.json()["items"]) == 1

    # Whitespace-only filter returns 422
    r_empty = client.get("/v1/updates?publisher=%20%20")
    assert r_empty.status_code == 422
    payload = ErrorEnvelope.model_validate(r_empty.json())
    assert payload.error.code == "validation_error"


def test_get_updates_cursor_filter_fingerprint_mismatch() -> None:
    items = [
        _make_source_item(publisher="OpenAI", first_fetched_at=BASE_TIME + timedelta(minutes=i))
        for i in range(5)
    ]
    repo = InMemorySourceItemFeedRepository(items)
    client = TestClient(create_app(source_item_feed_repository=repo, cursor_signing_key=TEST_KEY))

    # Get cursor bound to publisher=OpenAI
    r_page1 = client.get("/v1/updates?publisher=OpenAI&limit=2")
    assert r_page1.status_code == 200
    cursor = r_page1.json()["next_cursor"]
    assert cursor is not None

    # Reusing cursor with different filter fails with 400 invalid_cursor
    r_mismatch = client.get(f"/v1/updates?publisher=Anthropic&limit=2&cursor={cursor}")
    assert r_mismatch.status_code == 400
    payload = ErrorEnvelope.model_validate(r_mismatch.json())
    assert payload.error.code == "invalid_cursor"


def test_get_updates_omitted_when_repository_not_configured() -> None:
    client = TestClient(create_app(source_item_feed_repository=None))
    response = client.get("/v1/updates")
    assert response.status_code == 404
    openapi = client.get("/openapi.json").json()
    assert "/v1/updates" not in openapi.get("paths", {})


def test_create_app_requires_cursor_key_when_repository_configured() -> None:
    repo = InMemorySourceItemFeedRepository([])
    with pytest.raises(ValueError, match="cursor_codec or cursor_signing_key is required"):
        create_app(source_item_feed_repository=repo)


def test_database_repository_is_built_with_one_short_lived_session_per_request() -> None:
    sessions = _TrackedSessionFactory()
    repositories: list[InMemorySourceItemFeedRepository] = []

    def build_repository(_: AsyncSession) -> InMemorySourceItemFeedRepository:
        repository = InMemorySourceItemFeedRepository([])
        repositories.append(repository)
        return repository

    app = create_app(
        database_readiness_probe=_ReadyProbe(),
        database_session_factory=cast(async_sessionmaker[AsyncSession], sessions),
        source_item_feed_repository_factory=build_repository,
        cursor_signing_key=TEST_KEY,
    )
    client = TestClient(app)

    assert client.get("/v1/updates").status_code == 200
    assert client.get("/v1/updates").status_code == 200

    assert len(repositories) == 2
    assert sessions.created == 2
    assert sessions.entered == 2
    assert sessions.exited == 2


def test_create_app_rejects_incomplete_or_unsafe_scoped_repository_wiring() -> None:
    sessions = cast(async_sessionmaker[AsyncSession], _TrackedSessionFactory())

    with pytest.raises(ValueError, match="must be configured"):
        create_app(database_session_factory=sessions)

    with pytest.raises(ValueError, match="readiness probe is required"):
        create_app(
            database_session_factory=sessions,
            source_item_feed_repository_factory=lambda _: InMemorySourceItemFeedRepository([]),
        )

    with pytest.raises(ValueError, match="either a fixed or request-scoped repository"):
        create_app(
            source_item_feed_repository=InMemorySourceItemFeedRepository([]),
            database_readiness_probe=_ReadyProbe(),
            database_session_factory=sessions,
            source_item_feed_repository_factory=lambda _: InMemorySourceItemFeedRepository([]),
        )
