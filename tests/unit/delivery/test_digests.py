"""Tests for the published digest feed endpoint."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import date
from typing import cast
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import HttpUrl, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_daily_digest.delivery.api.app import create_app
from ai_daily_digest.delivery.api.errors import ErrorEnvelope
from ai_daily_digest.delivery.api.schemas import (
    DigestCitationDetail,
    DigestDetail,
    DigestSummary,
)
from ai_daily_digest.shared.ids import new_id
from ai_daily_digest.shared.repositories import DigestFeedFilter, DigestFeedRepository
from ai_daily_digest.shared.schemas import (
    ClaimValidationStatus,
    Digest,
    DigestCitation,
    DigestClaim,
    DigestStatus,
)

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

    async def get_published_digest(self, digest_id: uuid.UUID) -> Digest | None:
        for item in self._items:
            if item.id == digest_id and item.status is DigestStatus.PUBLISHED:
                return item
        return None


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


def test_get_digest_detail_returns_published_digest_with_grounded_claims() -> None:
    snap_id = new_id()
    claim_id = new_id()
    citation = DigestCitation(
        snapshot_id=snap_id,
        canonical_url=cast(HttpUrl, "https://anthropic.com/news/claude-2-1"),
        source_title="Claude 2.1 Announcement",
    )
    claim = DigestClaim(
        id=claim_id,
        text="Claude 2.1 now supports 200k tokens context window.",
        citation_snapshot_ids=[snap_id],
        citations=[citation],
        validation_status=ClaimValidationStatus.SUPPORTED,
    )
    digest = Digest(
        id=new_id(),
        digest_date=date(2026, 9, 12),
        status=DigestStatus.PUBLISHED,
        title="AI Daily Digest — 12 September 2026",
        claims=[claim],
    )
    client = _client([digest])

    response = client.get(f"/v1/digests/{digest.id}")

    assert response.status_code == 200
    data = response.json()
    validated = DigestDetail.model_validate(data)
    assert validated.id == digest.id
    assert validated.digest_date == date(2026, 9, 12)
    assert validated.status == DigestStatus.PUBLISHED
    assert validated.title == "AI Daily Digest — 12 September 2026"
    assert len(validated.claims) == 1
    assert validated.claims[0].id == claim_id
    assert validated.claims[0].text == "Claude 2.1 now supports 200k tokens context window."
    assert len(validated.claims[0].citations) == 1
    assert validated.claims[0].citations[0].snapshot_id == snap_id
    assert (
        str(validated.claims[0].citations[0].canonical_url)
        == "https://anthropic.com/news/claude-2-1"
    )
    assert validated.claims[0].citations[0].source_title == "Claude 2.1 Announcement"
    assert validated.claims[0].validation_status == ClaimValidationStatus.SUPPORTED


def test_get_digest_detail_supports_multiple_claims_and_citations_in_order() -> None:
    s1, s2 = new_id(), new_id()
    cit1 = DigestCitation(
        snapshot_id=s1,
        canonical_url=cast(HttpUrl, "https://example.com/1"),
        source_title="Source 1",
    )
    cit2 = DigestCitation(
        snapshot_id=s2,
        canonical_url=cast(HttpUrl, "https://example.com/2"),
        source_title="Source 2",
    )
    c1 = DigestClaim(
        id=new_id(),
        text="First change claim.",
        citation_snapshot_ids=[s1],
        citations=[cit1],
        validation_status=ClaimValidationStatus.SUPPORTED,
    )
    c2 = DigestClaim(
        id=new_id(),
        text="Second change claim.",
        citation_snapshot_ids=[s1, s2],
        citations=[cit1, cit2],
        validation_status=ClaimValidationStatus.SUPPORTED,
    )
    digest = Digest(
        id=new_id(),
        digest_date=date(2026, 9, 12),
        status=DigestStatus.PUBLISHED,
        title="Multi-claim digest",
        claims=[c1, c2],
    )
    client = _client([digest])

    response = client.get(f"/v1/digests/{digest.id}")

    assert response.status_code == 200
    data = response.json()
    assert [c["id"] for c in data["claims"]] == [str(c1.id), str(c2.id)]
    assert len(data["claims"][1]["citations"]) == 2
    assert data["claims"][1]["citations"][0]["snapshot_id"] == str(s1)
    assert data["claims"][1]["citations"][1]["snapshot_id"] == str(s2)


def test_get_digest_detail_empty_published_digest_returns_empty_claims() -> None:
    digest = _digest(10, title="Empty published digest")
    client = _client([digest])

    response = client.get(f"/v1/digests/{digest.id}")

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(digest.id)
    assert data["claims"] == []


def test_get_digest_detail_missing_id_returns_safe_404() -> None:
    client = _client([])
    missing_id = new_id()

    response = client.get(f"/v1/digests/{missing_id}")

    assert response.status_code == 404
    error = ErrorEnvelope.model_validate(response.json()).error
    assert error.code == "digest_not_found"
    assert error.message == "The requested digest was not found."


def test_get_digest_detail_draft_digest_returns_404_fail_closed() -> None:
    draft = Digest(
        id=new_id(),
        digest_date=date(2026, 9, 12),
        status=DigestStatus.DRAFT,
        title="Unpublished draft",
        claims=[],
    )
    client = _client([draft])

    response = client.get(f"/v1/digests/{draft.id}")

    assert response.status_code == 404
    error = ErrorEnvelope.model_validate(response.json()).error
    assert error.code == "digest_not_found"
    assert "Unpublished draft" not in response.text


def test_get_digest_detail_review_digest_returns_404_fail_closed() -> None:
    review = Digest(
        id=new_id(),
        digest_date=date(2026, 9, 12),
        status=DigestStatus.REVIEW,
        title="Review required digest",
        claims=[
            DigestClaim(
                id=new_id(),
                text="Unvalidated claim",
                citation_snapshot_ids=[new_id()],
                citations=[
                    DigestCitation(
                        snapshot_id=new_id(),
                        canonical_url=cast(HttpUrl, "https://example.com"),
                        source_title="Source",
                    )
                ],
                validation_status=ClaimValidationStatus.UNSUPPORTED,
            )
        ],
    )
    client = _client([review])

    response = client.get(f"/v1/digests/{review.id}")

    assert response.status_code == 404
    error = ErrorEnvelope.model_validate(response.json()).error
    assert error.code == "digest_not_found"
    assert "Unvalidated claim" not in response.text


def test_get_digest_detail_invalid_string_uuid_returns_422() -> None:
    client = _client([])

    response = client.get("/v1/digests/not-a-uuid")

    assert response.status_code == 422
    error = ErrorEnvelope.model_validate(response.json()).error
    assert error.code == "validation_error"


def test_get_digest_detail_valid_non_v7_uuid_returns_422() -> None:
    client = _client([])
    uuid_v4 = uuid.uuid4()

    response = client.get(f"/v1/digests/{uuid_v4}")

    assert response.status_code == 422
    error = ErrorEnvelope.model_validate(response.json()).error
    assert error.code == "validation_error"


def test_get_digest_detail_fails_closed_if_published_digest_has_unsupported_claim() -> None:
    claim = DigestClaim(
        id=new_id(),
        text="A claim with unsupported status in published digest",
        citation_snapshot_ids=[new_id()],
        citations=[
            DigestCitation(
                snapshot_id=new_id(),
                canonical_url=cast(HttpUrl, "https://example.com"),
                source_title="Source",
            )
        ],
        validation_status=ClaimValidationStatus.UNSUPPORTED,
    )
    digest = Digest(
        id=new_id(),
        digest_date=date(2026, 9, 12),
        status=DigestStatus.PUBLISHED,
        title="Corrupted published digest",
        claims=[claim],
    )
    client = _client([digest])

    response = client.get(f"/v1/digests/{digest.id}")

    assert response.status_code == 500
    error = ErrorEnvelope.model_validate(response.json()).error
    assert error.code == "internal_error"
    assert "unsupported status" not in response.text


def test_digest_citation_detail_rejects_unsafe_javascript_and_data_schemes() -> None:
    snap_id = new_id()

    # javascript: scheme is rejected at the schema boundary
    with pytest.raises(ValidationError):
        DigestCitationDetail(
            snapshot_id=snap_id,
            canonical_url=cast(HttpUrl, "javascript:alert(1)"),
            source_title="Malicious Link",
        )

    # data: scheme is rejected
    with pytest.raises(ValidationError):
        DigestCitationDetail(
            snapshot_id=snap_id,
            canonical_url=cast(HttpUrl, "data:text/html,<script>alert(1)</script>"),
            source_title="Data Link",
        )

    # empty string URL is rejected
    with pytest.raises(ValidationError):
        DigestCitationDetail(
            snapshot_id=snap_id,
            canonical_url=cast(HttpUrl, ""),
            source_title="Empty URL Link",
        )

    # Same validation strictly applies to shared DigestCitation
    with pytest.raises(ValidationError):
        DigestCitation(
            snapshot_id=snap_id,
            canonical_url=cast(HttpUrl, "javascript:alert(1)"),
            source_title="Malicious Link",
        )


def test_digest_citation_detail_rejects_empty_or_whitespace_source_title() -> None:
    snap_id = new_id()

    # Empty source_title is rejected
    with pytest.raises(ValidationError):
        DigestCitationDetail(
            snapshot_id=snap_id,
            canonical_url=cast(HttpUrl, "https://example.com/source"),
            source_title="",
        )

    # Whitespace-only source_title is stripped and rejected
    with pytest.raises(ValidationError):
        DigestCitationDetail(
            snapshot_id=snap_id,
            canonical_url=cast(HttpUrl, "https://example.com/source"),
            source_title="   \t \n ",
        )

    # Empty / whitespace-only source_title on shared DigestCitation is also rejected
    with pytest.raises(ValidationError):
        DigestCitation(
            snapshot_id=snap_id,
            canonical_url=cast(HttpUrl, "https://example.com/source"),
            source_title="",
        )
    with pytest.raises(ValidationError):
        DigestCitation(
            snapshot_id=snap_id,
            canonical_url=cast(HttpUrl, "https://example.com/source"),
            source_title="   \t \n ",
        )


def test_get_digest_detail_fails_closed_when_persisted_citation_has_malformed_url_or_empty_title() -> (
    None
):
    # A published digest with a malformed/missing citation fails closed with 500
    claim_id = new_id()
    # Construct a raw ungrounded/malformed claim representation
    claim = DigestClaim.model_construct(
        id=claim_id,
        text="Claim with corrupt citation",
        citation_snapshot_ids=[new_id()],
        citations=[
            DigestCitation.model_construct(
                snapshot_id=new_id(),
                canonical_url="javascript:alert(1)",  # bypassed construct
                source_title="Corrupt Link",
            )
        ],
        validation_status=ClaimValidationStatus.SUPPORTED,
    )
    digest = Digest(
        id=new_id(),
        digest_date=date(2026, 9, 12),
        status=DigestStatus.PUBLISHED,
        title="Corrupted citation digest",
        claims=[claim],
    )
    client = _client([digest])

    response = client.get(f"/v1/digests/{digest.id}")

    assert response.status_code == 500
    error = ErrorEnvelope.model_validate(response.json()).error
    assert error.code == "internal_error"
    assert "javascript:" not in response.text


def test_get_digest_detail_fails_closed_when_claim_has_mixed_valid_and_corrupt_citations() -> None:
    # A published claim containing one valid citation and one corrupt citation must fail closed with
    # 500 and not leak partial citations.
    claim_id = new_id()
    valid_snap_id = new_id()
    corrupt_snap_id = new_id()
    claim = DigestClaim.model_construct(
        id=claim_id,
        text="Claim with mixed valid and corrupt citations",
        citation_snapshot_ids=[valid_snap_id, corrupt_snap_id],
        citations=[
            DigestCitation.model_construct(
                snapshot_id=valid_snap_id,
                canonical_url="https://anthropic.com/news/claude-3-5",
                source_title="Valid Title",
            ),
            DigestCitation.model_construct(
                snapshot_id=corrupt_snap_id,
                canonical_url="javascript:alert(1)",
                source_title="   ",  # whitespace title
            ),
        ],
        validation_status=ClaimValidationStatus.SUPPORTED,
    )
    digest = Digest(
        id=new_id(),
        digest_date=date(2026, 9, 12),
        status=DigestStatus.PUBLISHED,
        title="Mixed citation digest",
        claims=[claim],
    )
    client = _client([digest])

    response = client.get(f"/v1/digests/{digest.id}")

    assert response.status_code == 500
    error = ErrorEnvelope.model_validate(response.json()).error
    assert error.code == "internal_error"
    # Verify no partial response content or unsafe schema leaked
    assert "Valid Title" not in response.text
    assert "javascript:" not in response.text
