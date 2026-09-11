"""Delivery-owned subscription persistence models from ADRs 0012 and 0013."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from ai_daily_digest.shared.db.metadata import Base


class SubscriptionStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    UNSUBSCRIBED = "unsubscribed"
    SUPPRESSED = "suppressed"


class SuppressionReason(StrEnum):
    HARD_BOUNCE = "hard_bounce"
    ABUSE_COMPLAINT = "abuse_complaint"
    ADMINISTRATIVE = "administrative"


class ConsentEventType(StrEnum):
    CONSENT_REQUESTED = "consent_requested"
    CONFIRMED = "confirmed"
    UNSUBSCRIBED = "unsubscribed"
    SUPPRESSED = "suppressed"


class SubscriptionModel(Base):
    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    normalized_email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    consent_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    consented_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unsubscribed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suppressed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suppression_reason: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','confirmed','unsubscribed','suppressed')",
            name="ck_subscriptions_status",
        ),
        CheckConstraint("consent_generation >= 1", name="ck_subscriptions_generation"),
        CheckConstraint(
            "suppression_reason IS NULL OR suppression_reason IN "
            "('hard_bounce','abuse_complaint','administrative')",
            name="ck_subscriptions_suppression_reason",
        ),
        CheckConstraint(
            "(status = 'pending' AND confirmed_at IS NULL AND unsubscribed_at IS NULL "
            "AND suppressed_at IS NULL AND suppression_reason IS NULL) OR "
            "(status = 'confirmed' AND confirmed_at IS NOT NULL AND unsubscribed_at IS NULL "
            "AND suppressed_at IS NULL AND suppression_reason IS NULL) OR "
            "(status = 'unsubscribed' AND confirmed_at IS NOT NULL "
            "AND unsubscribed_at IS NOT NULL AND suppressed_at IS NULL "
            "AND suppression_reason IS NULL) OR "
            "(status = 'suppressed' AND suppressed_at IS NOT NULL "
            "AND suppression_reason IS NOT NULL)",
            name="ck_subscriptions_state_timestamps",
        ),
    )


class SubscriptionTokenModel(Base):
    __tablename__ = "subscription_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("subscriptions.id", ondelete="RESTRICT"), nullable=False
    )
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    token_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    key_id: Mapped[str] = mapped_column(String(32), nullable=False)
    consent_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "purpose IN ('confirm_subscription','unsubscribe')",
            name="ck_subscription_tokens_purpose",
        ),
        CheckConstraint("consent_generation >= 1", name="ck_subscription_tokens_generation"),
        CheckConstraint(
            "(purpose = 'confirm_subscription' AND expires_at IS NOT NULL) OR "
            "(purpose = 'unsubscribe' AND expires_at IS NULL)",
            name="ck_subscription_tokens_expiry",
        ),
        Index(
            "ix_subscription_tokens_subscription_generation",
            "subscription_id",
            "consent_generation",
        ),
    )


class SubscriptionConsentEventModel(Base):
    __tablename__ = "subscription_consent_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("subscriptions.id", ondelete="RESTRICT"), nullable=False
    )
    consent_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    suppression_reason: Mapped[str | None] = mapped_column(String(32))

    __table_args__ = (
        UniqueConstraint(
            "subscription_id",
            "consent_generation",
            "event_type",
            name="uq_subscription_consent_event",
        ),
        CheckConstraint("consent_generation >= 1", name="ck_sub_consent_generation"),
        CheckConstraint(
            "event_type IN ('consent_requested','confirmed','unsubscribed','suppressed')",
            name="ck_sub_consent_type",
        ),
        CheckConstraint(
            "(event_type = 'suppressed' AND suppression_reason IN "
            "('hard_bounce','abuse_complaint','administrative')) OR "
            "(event_type <> 'suppressed' AND suppression_reason IS NULL)",
            name="ck_sub_consent_reason",
        ),
    )


class SubscriptionRateLimitModel(Base):
    __tablename__ = "subscription_rate_limits"

    scope: Mapped[str] = mapped_column(String(48), primary_key=True)
    identity_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("request_count >= 1", name="ck_subscription_rate_limits_count"),
        Index("ix_subscription_rate_limits_expiry", "expires_at"),
    )
