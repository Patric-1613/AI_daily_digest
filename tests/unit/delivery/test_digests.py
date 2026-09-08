"""Tests for the published digest feed endpoint."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import date
from typing import cast
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_daily_digest.delivery.api.app import create_app
from ai_daily_digest.delivery.api.errors import ErrorEnvelope
from ai_daily_digest.delivery.api.schemas import DigestSummary
from ai_daily_digest.shared.ids import new_id
from ai_daily_digest.shared.repositories import DigestFeedFilter, DigestFeedRepository
from ai_daily_digest.shared.schemas import Digest, DigestStatus

TEST_KEY = b"\x2a" * 32


class InMemoryDigestFeedRepository:
    """Deterministic fake implementing the published-only read protocol."""

    def __init__(self, items: Iterable[Digest] = ()) -> None:
        self._items = list(items)

    async def list_digests(
        self,
        *,
        feed_filter: DigestFeedFilter | None = None,
        after: tuple[date, uuid.UUID] | None = None,
        limit: int = 20,
    ) -> Sequence[Digest]:
        filtered = [item for item in self._items if item.status is DigestStatus.PUBLISHED]
        if feed_filter is not None:
            if feed_filter.date_from is not None:
                filtered = [item for item in filtered if item.digest_date >= feed_filter.date_from]
            if feed_filter.date_to is not None:
                filtered = [item for item in filtered if item.digest_date < feed_filter.date_to]
        ordered = sorted(filtered, key=lambda item: (item.digest_date, item.id), reverse=True)
        if after is not None:
            ordered = [item for item in ordered if (item.digest_date, item.id) < after]
        return ordered[: limit + 1]


def _digest(day: int, *, title: str | None = None) -> Digest:
    return Digest(
        id=new_id(),
        digest_date=date(2026, 9, day),
        status=DigestStatus.PUBLISHED,
        title=title or f"Digest {day}",
        claims=[],
    )


def _client(items: Iterable[Digest]) -> TestClient:
    return TestClient(
        create_app(
            digest_feed_repository=InMemoryDigestFeedRepository(items),
            cursor_signing_key=TEST_KEY,
        )
    )


def test_get_digests_returns_newest_published_summaries_without_claims() -> None:
    client = _client([_digest(5), _digest(7), _digest(6)])

    response = client.get("/v1/digests")

    assert response.status_code == 200
    assert [item["title"] for item in response.json()["items"]] == [
        "Digest 7",
        "Digest 6",
        "Digest 5",
    ]
    assert response.json()["next_cursor"] is None
    assert all("claims" not in item for item in response.json()["items"])
    for item in response.json()["items"]:
        DigestSummary.model_validate(item)


def test_get_digests_paginates_and_binds_cursor_to_date_filters() -> None:
    client = _client([_digest(4), _digest(5), _digest(6), _digest(7)])

    first = client.get("/v1/digests?limit=1&date_from=2026-09-05&date_to=2026-09-08")
    assert first.status_code == 200
    assert first.json()["items"][0]["title"] == "Digest 7"
    cursor = first.json()["next_cursor"]
    assert cursor is not None

    second = client.get(
        "/v1/digests",
        params={
            "limit": 1,
            "date_from": "2026-09-05",
            "date_to": "2026-09-08",
            "cursor": cursor,
        },
    )
    assert second.status_code == 200
    assert second.json()["items"][0]["title"] == "Digest 6"

    mismatch = client.get("/v1/digests", params={"cursor": cursor})
    assert mismatch.status_code == 400
    assert ErrorEnvelope.model_validate(mismatch.json()).error.code == "invalid_cursor"


@pytest.mark.parametrize(
    "query",
    [
        "date_from=2026-09-08&date_to=2026-09-08",
        "date_from=2026-09-09&date_to=2026-09-08",
        "date_from=not-a-date",
    ],
)
def test_get_digests_rejects_invalid_date_ranges(query: str) -> None:
    response = _client([]).get(f"/v1/digests?{query}")

    assert response.status_code == 422
    assert ErrorEnvelope.model_validate(response.json()).error.code == "validation_error"


def test_get_digests_rejects_invalid_cursor_before_repository_call() -> None:
    repository = AsyncMock(spec=DigestFeedRepository)
    client = TestClient(create_app(digest_feed_repository=repository, cursor_signing_key=TEST_KEY))

    response = client.get("/v1/digests?cursor=not-a-cursor")

    assert response.status_code == 400
    assert ErrorEnvelope.model_validate(response.json()).error.code == "invalid_cursor"
    repository.list_digests.assert_not_called()


def test_get_digests_is_omitted_without_repository() -> None:
    client = TestClient(create_app())

    assert client.get("/v1/digests").status_code == 404
    assert "/v1/digests" not in client.get("/openapi.json").json()["paths"]


def test_get_digests_fails_closed_if_repository_breaks_published_only_contract() -> None:
    repository = AsyncMock(spec=DigestFeedRepository)
    repository.list_digests.return_value = [
        Digest(
            id=new_id(),
            digest_date=date(2026, 9, 8),
            status=DigestStatus.DRAFT,
            title="Private draft",
            claims=[],
        )
    ]
    client = TestClient(
        create_app(digest_feed_repository=repository, cursor_signing_key=TEST_KEY),
        raise_server_exceptions=False,
    )

    response = client.get("/v1/digests")

    assert response.status_code == 500
    assert ErrorEnvelope.model_validate(response.json()).error.code == "internal_error"
    assert "Private draft" not in response.text


def test_digest_repository_configuration_fails_closed() -> None:
    repository = InMemoryDigestFeedRepository([])

    with pytest.raises(ValueError, match="paginated repository"):
        create_app(digest_feed_repository=repository)
    with pytest.raises(ValueError, match="database_session_factory is required"):
        create_app(digest_feed_repository_factory=lambda _: repository)
    with pytest.raises(ValueError, match="readiness probe is required"):
        create_app(
            database_session_factory=cast(async_sessionmaker[AsyncSession], object()),
            digest_feed_repository_factory=lambda _: repository,
        )
    with pytest.raises(ValueError, match="either a fixed or request-scoped digest repository"):
        create_app(
            digest_feed_repository=repository,
            database_session_factory=cast(async_sessionmaker[AsyncSession], object()),
            digest_feed_repository_factory=lambda _: repository,
        )
