"""Public delivery summary schemas — ADR 0008 §12."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from ai_daily_digest.shared.ids import Uuid7Id
from ai_daily_digest.shared.schemas import ClaimValidationStatus, DigestStatus

__all__ = [
    "DigestCitationDetail",
    "DigestClaimDetail",
    "DigestDetail",
    "DigestSummary",
    "UpdateSummary",
]


class DigestCitationDetail(BaseModel):
    """Public projection of a claim citation linking to its source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot_id: Uuid7Id
    canonical_url: HttpUrl
    source_title: str = Field(min_length=1)


class DigestClaimDetail(BaseModel):
    """Public detail projection of a published digest claim."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: Uuid7Id
    text: str
    citations: list[DigestCitationDetail] = Field(min_length=1)
    validation_status: ClaimValidationStatus


class DigestDetail(BaseModel):
    """Public detail projection of a published digest with grounded claims."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: Uuid7Id
    digest_date: date
    status: DigestStatus
    title: str
    claims: list[DigestClaimDetail] = Field(default_factory=list)


class DigestSummary(BaseModel):
    """Public list projection of a published digest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: Uuid7Id
    digest_date: date
    status: DigestStatus
    title: str


class UpdateSummary(BaseModel):
    """Public projection of a source update — ADR 0008 §12.

    List endpoints return delivery-specific summary models rather than raw shared
    domain records, ensuring internal fields (such as dedupe_key, raw storage
    locations, and content_text) are never exposed publicly.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: Uuid7Id
    source_id: str
    publisher: str
    title: str
    canonical_url: HttpUrl | str
    published_at: datetime | None = None
    first_fetched_at: datetime
    event_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    language: str | None = None
    latest_snapshot_id: Uuid7Id | None = None
