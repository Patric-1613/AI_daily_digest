"""Shared repository protocols for cross-module queries — ADR 0002 §12.2, ADR 0008, ADR 0011."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol, runtime_checkable

from ai_daily_digest.shared.schemas import Change, Digest, SourceItem
from ai_daily_digest.shared.snapshot_resolver import SnapshotResolver

__all__ = [
    "ChangeFeedFilter",
    "ChangeFeedRepository",
    "DigestFeedFilter",
    "DigestFeedRepository",
    "DigestRepository",
    "FeedFilter",
    "SourceItemFeedRepository",
]


@dataclass(frozen=True, slots=True)
class FeedFilter:
    """Canonical typed filter criteria for source item feed queries — ADR 0008."""

    publisher: str | None = None
    source_id: str | None = None


@dataclass(frozen=True, slots=True)
class ChangeFeedFilter:
    """Canonical typed filter criteria for change feed queries — ADR 0008 §8."""

    company_key: str | None = None
    product_key: str | None = None
    field: str | None = None
    detected_from: datetime | None = None
    detected_to: datetime | None = None


@dataclass(frozen=True, slots=True)
class DigestFeedFilter:
    """Canonical typed filter criteria for digest feed queries — ADR 0008 §8."""

    date_from: date | None = None
    date_to: date | None = None


@runtime_checkable
class SourceItemFeedRepository(Protocol):
    """Asynchronous read protocol for cursor-paginated source item feeds.

    This protocol represents a cross-module seam: the delivery API depends on
    a query capability that ingestion's source persistence provides. The
    concrete PostgreSQL adapter is ingestion-owned, while the protocol lives in
    shared/ (ADR 0002 §12.2).
    """

    async def list_source_items(
        self,
        *,
        feed_filter: FeedFilter | None = None,
        after: tuple[datetime, uuid.UUID] | None = None,
        limit: int = 20,
    ) -> Sequence[SourceItem]:
        """Fetch up to limit + 1 items ordered by (first_fetched_at DESC, id DESC).

        Args:
            feed_filter: Canonical typed filter criteria (publisher, source_id), if any.
            after: Keyset continuation tuple (first_fetched_at, id), if resuming.
            limit: Maximum items to return in the page (the repository returns up to limit + 1
                   to support forward cursor generation).

        Returns:
            A sequence of up to limit + 1 SourceItem instances matching the filters and
            keyset predicate.
        """


@runtime_checkable
class ChangeFeedRepository(Protocol):
    """Asynchronous read protocol for cursor-paginated change feeds — ADR 0008 & ADR 0011.

    This protocol represents a cross-module seam: the delivery API (/v1/changes)
    depends on a query capability that intelligence's change persistence provides.
    """

    async def list_changes(
        self,
        *,
        feed_filter: ChangeFeedFilter | None = None,
        after: tuple[datetime, uuid.UUID] | None = None,
        limit: int = 20,
    ) -> Sequence[Change]:
        """Fetch up to limit + 1 changes ordered by (detected_at DESC, id DESC).

        Args:
            feed_filter: Optional filter criteria (company_key, product_key, field).
            after: Keyset continuation tuple (detected_at, id), if resuming.
            limit: Maximum items to return in the page (returns up to limit + 1
                   to support forward cursor generation).

        Returns:
            A sequence of up to limit + 1 Change instances matching the filters and
            keyset predicate.
        """


@runtime_checkable
class DigestFeedRepository(Protocol):
    """Asynchronous read protocol for cursor-paginated digest feeds — ADR 0008 & ADR 0011.

    This protocol represents a cross-module seam: the delivery API (/v1/digests)
    depends on a query capability that intelligence's digest persistence provides.
    """

    async def list_digests(
        self,
        *,
        feed_filter: DigestFeedFilter | None = None,
        after: tuple[date, uuid.UUID] | None = None,
        limit: int = 20,
    ) -> Sequence[Digest]:
        """Fetch up to limit + 1 published digests ordered by (digest_date DESC, id DESC).

        IMPORTANT: Only digests with status='published' are returned.

        Args:
            feed_filter: Optional filter criteria (start_date, end_date).
            after: Keyset continuation tuple (digest_date, id), if resuming.
            limit: Maximum items to return in the page (the repository returns up to limit + 1
                   to support forward cursor generation).

        Returns:
            A sequence of up to limit + 1 published Digest instances matching the
            filters and keyset predicate.
        """

    async def get_published_digest(self, digest_id: uuid.UUID) -> Digest | None:
        """Fetch a single published digest by its ID, with claims and citations loaded.

        IMPORTANT: Only digests with status='published' are returned. If the digest
        does not exist or is in 'draft'/'review' status, None is returned.

        Args:
            digest_id: Unique UUID of the digest.

        Returns:
            The published Digest instance if found and published; otherwise None.
        """


@runtime_checkable
class DigestRepository(Protocol):
    """Asynchronous repository protocol for intelligence digest persistence and publication
    — ADR 0011."""

    async def persist_digest(self, digest: Digest) -> Digest:
        """Persist a digest aggregate with its ordered claims and citations.

        Idempotent on repeat calls with identical attributes.
        """

    async def publish_digest(
        self,
        digest_id: uuid.UUID,
        *,
        known_snapshot_ids: set[uuid.UUID],
        snapshot_resolver: SnapshotResolver,
    ) -> Digest:
        """Publish an existing digest via the intelligence validation gate.

        Delegates to validate.py::publish_digest() and updates database state.
        If validation succeeds, claims are updated to 'supported' before the digest
        is marked 'published'. If validation fails, the digest is routed to 'review'.

        Raises:
            ValueError: If digest_id is not found.
        """

    async def get_digest_by_id(self, digest_id: uuid.UUID) -> Digest | None:
        """Retrieve a digest aggregate by its unique ID, preserving claim and citation order."""

    async def get_latest_published_digest(self) -> Digest | None:
        """Retrieve the most recently published digest by (digest_date DESC, id DESC)."""
