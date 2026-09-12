"""Transactional PostgreSQL subscription lifecycle from ADR 0013."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai_daily_digest.delivery.subscriptions.models import (
    ConsentEventType,
    SubscriptionConsentEventModel,
    SubscriptionModel,
    SubscriptionRateLimitModel,
    SubscriptionStatus,
    SubscriptionTokenModel,
    SuppressionReason,
)
from ai_daily_digest.delivery.subscriptions.tokens import (
    InvalidSubscriptionTokenError,
    IssuedSubscriptionToken,
    SubscriptionTokenCodec,
    SubscriptionTokenPurpose,
)
from ai_daily_digest.shared.ids import new_id

CONFIRMATION_LIFETIME = timedelta(hours=24)
TOKEN_RETENTION = timedelta(days=90)


class Clock(Protocol):
    def __call__(self) -> datetime: ...


def utc_now() -> datetime:
    return datetime.now(UTC)


def normalize_email(address: str) -> str:
    """Validate and normalize without changing provider-owned local-part case."""
    normalized = unicodedata.normalize("NFC", address.strip())
    if len(normalized) > 320 or any(ord(char) < 32 or char.isspace() for char in normalized):
        raise ValueError("invalid email address")
    if normalized.count("@") != 1:
        raise ValueError("invalid email address")
    local, domain = normalized.rsplit("@", 1)
    if not local or not domain or local.startswith(".") or local.endswith(".") or ".." in local:
        raise ValueError("invalid email address")
    labels = domain.lower().split(".")
    if len(labels) < 2 or any(
        not label or label.startswith("-") or label.endswith("-") for label in labels
    ):
        raise ValueError("invalid email address")
    try:
        ascii_domain = ".".join(label.encode("idna").decode("ascii") for label in labels)
    except UnicodeError as exc:
        raise ValueError("invalid email address") from exc
    if any(
        not all(character.isalnum() or character == "-" for character in label)
        for label in ascii_domain.split(".")
    ):
        raise ValueError("invalid email address")
    return f"{local}@{ascii_domain}"


def network_identity(address: str) -> str:
    ip = ipaddress.ip_address(address)
    prefix = 24 if ip.version == 4 else 56
    return str(ipaddress.ip_network(f"{ip}/{prefix}", strict=False))


def keyed_identity_digest(key: bytes, *, purpose: str, value: str) -> str:
    return hmac.new(key, f"{purpose}\0{value}".encode(), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class SubscriptionRequestResult:
    confirmation_token: IssuedSubscriptionToken | None


@dataclass(frozen=True)
class SubscriptionConfirmationResult:
    """Transient values produced by one committed confirmation transition."""

    address: str = field(repr=False)
    unsubscribe_token: IssuedSubscriptionToken = field(repr=False)


class SubscriptionRepository:
    """One request-scoped repository; no transaction crosses provider I/O."""

    def __init__(
        self, session: AsyncSession, codec: SubscriptionTokenCodec, *, clock: Clock = utc_now
    ) -> None:
        self._session = session
        self._codec = codec
        self._clock = clock

    async def request_subscription(self, address: str) -> SubscriptionRequestResult:
        email = normalize_email(address)
        now = self._clock()
        async with self._session.begin():
            # Serialize both the existing-row and first-insert paths without logging the address.
            await self._session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:address, 0))"),
                {"address": email},
            )
            subscription = await self._session.scalar(
                select(SubscriptionModel)
                .where(SubscriptionModel.normalized_email == email)
                .with_for_update()
            )
            if subscription is None:
                subscription = SubscriptionModel(
                    id=new_id(),
                    normalized_email=email,
                    status=SubscriptionStatus.PENDING.value,
                    consent_generation=1,
                    consented_at=now,
                    confirmed_at=None,
                    unsubscribed_at=None,
                    suppressed_at=None,
                    suppression_reason=None,
                    created_at=now,
                    updated_at=now,
                )
                self._session.add(subscription)
                # The models deliberately avoid cross-table ORM relationships; flush the
                # parent explicitly so the database foreign key orders audit/token inserts.
                await self._session.flush()
                self._add_event(subscription, ConsentEventType.CONSENT_REQUESTED, now)
            elif subscription.status == SubscriptionStatus.UNSUBSCRIBED.value:
                previous_generation = subscription.consent_generation
                await self._session.execute(
                    update(SubscriptionTokenModel)
                    .where(
                        SubscriptionTokenModel.subscription_id == subscription.id,
                        SubscriptionTokenModel.consent_generation == previous_generation,
                        SubscriptionTokenModel.revoked_at.is_(None),
                    )
                    .values(revoked_at=now)
                )
                subscription.consent_generation += 1
                subscription.status = SubscriptionStatus.PENDING.value
                subscription.consented_at = now
                subscription.confirmed_at = None
                subscription.unsubscribed_at = None
                subscription.updated_at = now
                self._add_event(subscription, ConsentEventType.CONSENT_REQUESTED, now)
            elif subscription.status in {
                SubscriptionStatus.CONFIRMED.value,
                SubscriptionStatus.SUPPRESSED.value,
            }:
                return SubscriptionRequestResult(confirmation_token=None)

            issued = self._codec.issue(SubscriptionTokenPurpose.CONFIRM)
            self._session.add(
                SubscriptionTokenModel(
                    id=new_id(),
                    subscription_id=subscription.id,
                    purpose=issued.purpose.value,
                    token_digest=issued.token_digest,
                    key_id=issued.key_id,
                    consent_generation=subscription.consent_generation,
                    created_at=now,
                    expires_at=now + CONFIRMATION_LIFETIME,
                    used_at=None,
                    revoked_at=None,
                )
            )
        return SubscriptionRequestResult(confirmation_token=issued)

    async def confirm(self, raw_token: str) -> SubscriptionConfirmationResult:
        verified = self._codec.verify(raw_token, expected_purpose=SubscriptionTokenPurpose.CONFIRM)
        now = self._clock()
        async with self._session.begin():
            subscription_id = await self._token_subscription_id(verified.token_digest)
            subscription = await self._locked_subscription(subscription_id)
            token = await self._locked_token(verified.token_digest)
            token_is_invalid = (
                token.subscription_id != subscription.id
                or token.key_id != verified.key_id
                or token.purpose != verified.purpose.value
                or token.used_at is not None
                or token.revoked_at is not None
            )
            if token_is_invalid:
                raise InvalidSubscriptionTokenError()
            expires_at = token.expires_at
            if expires_at is None:
                raise InvalidSubscriptionTokenError()
            lifecycle_is_invalid = (
                expires_at <= now
                or token.consent_generation != subscription.consent_generation
                or subscription.status != SubscriptionStatus.PENDING.value
            )
            if lifecycle_is_invalid:
                raise InvalidSubscriptionTokenError()
            token.used_at = now
            subscription.status = SubscriptionStatus.CONFIRMED.value
            subscription.confirmed_at = now
            subscription.updated_at = now
            self._add_event(subscription, ConsentEventType.CONFIRMED, now)
            await self._session.execute(
                update(SubscriptionTokenModel)
                .where(
                    SubscriptionTokenModel.subscription_id == subscription.id,
                    SubscriptionTokenModel.consent_generation == subscription.consent_generation,
                    SubscriptionTokenModel.purpose == SubscriptionTokenPurpose.CONFIRM.value,
                    SubscriptionTokenModel.id != token.id,
                    SubscriptionTokenModel.used_at.is_(None),
                    SubscriptionTokenModel.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            unsubscribe_token = self._codec.issue(SubscriptionTokenPurpose.UNSUBSCRIBE)
            self._session.add(
                SubscriptionTokenModel(
                    id=new_id(),
                    subscription_id=subscription.id,
                    purpose=unsubscribe_token.purpose.value,
                    token_digest=unsubscribe_token.token_digest,
                    key_id=unsubscribe_token.key_id,
                    consent_generation=subscription.consent_generation,
                    created_at=now,
                    expires_at=None,
                    used_at=None,
                    revoked_at=None,
                )
            )
            normalized_address = subscription.normalized_email
        return SubscriptionConfirmationResult(
            address=normalized_address,
            unsubscribe_token=unsubscribe_token,
        )

    async def issue_unsubscribe_token(self, subscription_id: uuid.UUID) -> IssuedSubscriptionToken:
        now = self._clock()
        async with self._session.begin():
            subscription = await self._locked_subscription(subscription_id)
            if subscription.status != SubscriptionStatus.CONFIRMED.value:
                raise InvalidSubscriptionTokenError()
            issued = self._codec.issue(SubscriptionTokenPurpose.UNSUBSCRIBE)
            self._session.add(
                SubscriptionTokenModel(
                    id=new_id(),
                    subscription_id=subscription.id,
                    purpose=issued.purpose.value,
                    token_digest=issued.token_digest,
                    key_id=issued.key_id,
                    consent_generation=subscription.consent_generation,
                    created_at=now,
                    expires_at=None,
                    used_at=None,
                    revoked_at=None,
                )
            )
        return issued

    async def unsubscribe(self, raw_token: str) -> None:
        verified = self._codec.verify(
            raw_token, expected_purpose=SubscriptionTokenPurpose.UNSUBSCRIBE
        )
        now = self._clock()
        async with self._session.begin():
            subscription_id = await self._token_subscription_id(verified.token_digest)
            subscription = await self._locked_subscription(subscription_id)
            token = await self._locked_token(verified.token_digest)
            token_is_invalid = (
                token.subscription_id != subscription.id
                or token.key_id != verified.key_id
                or token.purpose != verified.purpose.value
                or token.revoked_at is not None
            )
            lifecycle_is_invalid = (
                token.consent_generation != subscription.consent_generation
                or subscription.status
                not in {SubscriptionStatus.CONFIRMED.value, SubscriptionStatus.UNSUBSCRIBED.value}
            )
            if token_is_invalid or lifecycle_is_invalid:
                raise InvalidSubscriptionTokenError()
            if token.used_at is None:
                token.used_at = now
            if subscription.status == SubscriptionStatus.CONFIRMED.value:
                subscription.status = SubscriptionStatus.UNSUBSCRIBED.value
                subscription.unsubscribed_at = now
                subscription.updated_at = now
                self._add_event(subscription, ConsentEventType.UNSUBSCRIBED, now)

    async def suppress(self, subscription_id: uuid.UUID, reason: SuppressionReason) -> None:
        """Apply a trusted suppression; no public route can call this operation."""
        now = self._clock()
        async with self._session.begin():
            subscription = await self._locked_subscription(subscription_id)
            if subscription.status == SubscriptionStatus.SUPPRESSED.value:
                return
            subscription.status = SubscriptionStatus.SUPPRESSED.value
            subscription.suppressed_at = now
            subscription.suppression_reason = reason.value
            subscription.updated_at = now
            await self._session.execute(
                update(SubscriptionTokenModel)
                .where(
                    SubscriptionTokenModel.subscription_id == subscription.id,
                    SubscriptionTokenModel.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            self._add_event(
                subscription,
                ConsentEventType.SUPPRESSED,
                now,
                suppression_reason=reason,
            )

    async def consume_rate_limit(
        self, *, scope: str, identity_digest: str, window: timedelta, limit: int
    ) -> bool:
        now = self._clock()
        seconds = int(window.total_seconds())
        epoch = int(now.timestamp())
        started = datetime.fromtimestamp(epoch - epoch % seconds, tz=UTC)
        expires = started + window + timedelta(hours=24)
        statement = (
            pg_insert(SubscriptionRateLimitModel)
            .values(
                scope=scope,
                identity_digest=identity_digest,
                window_started_at=started,
                request_count=1,
                expires_at=expires,
            )
            .on_conflict_do_update(
                index_elements=["scope", "identity_digest", "window_started_at"],
                set_={"request_count": SubscriptionRateLimitModel.request_count + 1},
            )
            .returning(SubscriptionRateLimitModel.request_count)
        )
        count = (await self._session.execute(statement)).scalar_one()
        await self._session.commit()
        return count <= limit

    async def cleanup_terminal_state(self) -> tuple[int, int]:
        now = self._clock()
        cutoff = now - TOKEN_RETENTION
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        latest_terminal = func.greatest(
            func.coalesce(SubscriptionTokenModel.expires_at, epoch),
            func.coalesce(SubscriptionTokenModel.used_at, epoch),
            func.coalesce(SubscriptionTokenModel.revoked_at, epoch),
        )
        token_result = await self._session.execute(
            delete(SubscriptionTokenModel).where(
                or_(
                    SubscriptionTokenModel.expires_at.is_not(None),
                    SubscriptionTokenModel.used_at.is_not(None),
                    SubscriptionTokenModel.revoked_at.is_not(None),
                ),
                latest_terminal <= cutoff,
                or_(
                    SubscriptionTokenModel.purpose == SubscriptionTokenPurpose.CONFIRM.value,
                    SubscriptionTokenModel.revoked_at.is_not(None),
                ),
            )
        )
        limit_result = await self._session.execute(
            delete(SubscriptionRateLimitModel).where(SubscriptionRateLimitModel.expires_at <= now)
        )
        await self._session.commit()
        return (
            cast(Any, token_result).rowcount or 0,
            cast(Any, limit_result).rowcount or 0,
        )

    async def _token_subscription_id(self, digest: str) -> uuid.UUID:
        """Locate the parent before locks so every lifecycle path locks parent first."""
        subscription_id = await self._session.scalar(
            select(SubscriptionTokenModel.subscription_id).where(
                SubscriptionTokenModel.token_digest == digest
            )
        )
        if subscription_id is None:
            raise InvalidSubscriptionTokenError()
        return subscription_id

    async def _locked_token(self, digest: str) -> SubscriptionTokenModel:
        token = await self._session.scalar(
            select(SubscriptionTokenModel)
            .where(SubscriptionTokenModel.token_digest == digest)
            .with_for_update()
        )
        if token is None:
            raise InvalidSubscriptionTokenError()
        return token

    async def _locked_subscription(self, subscription_id: uuid.UUID) -> SubscriptionModel:
        subscription = await self._session.scalar(
            select(SubscriptionModel)
            .where(SubscriptionModel.id == subscription_id)
            .with_for_update()
        )
        if subscription is None:
            raise InvalidSubscriptionTokenError()
        return subscription

    def _add_event(
        self,
        subscription: SubscriptionModel,
        event: ConsentEventType,
        now: datetime,
        *,
        suppression_reason: SuppressionReason | None = None,
    ) -> None:
        self._session.add(
            SubscriptionConsentEventModel(
                id=new_id(),
                subscription_id=subscription.id,
                consent_generation=subscription.consent_generation,
                event_type=event.value,
                occurred_at=now,
                suppression_reason=(suppression_reason.value if suppression_reason else None),
            )
        )
