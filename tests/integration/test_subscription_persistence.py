from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_daily_digest.delivery.subscriptions.models import (
    SubscriptionConsentEventModel,
    SubscriptionModel,
    SubscriptionStatus,
    SubscriptionTokenModel,
    SuppressionReason,
)
from ai_daily_digest.delivery.subscriptions.repository import SubscriptionRepository
from ai_daily_digest.delivery.subscriptions.tokens import (
    InvalidSubscriptionTokenError,
    SubscriptionTokenCodec,
    SubscriptionTokenEnvironment,
    SubscriptionTokenPurpose,
)

pytestmark = pytest.mark.integration


@dataclass
class _MutableClock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


def _codec() -> SubscriptionTokenCodec:
    return SubscriptionTokenCodec(
        environment=SubscriptionTokenEnvironment.TEST,
        keys={
            SubscriptionTokenPurpose.CONFIRM: {"test-confirm": b"c" * 32},
            SubscriptionTokenPurpose.UNSUBSCRIBE: {"test-unsubscribe": b"u" * 32},
        },
        active_key_ids={
            SubscriptionTokenPurpose.CONFIRM: "test-confirm",
            SubscriptionTokenPurpose.UNSUBSCRIBE: "test-unsubscribe",
        },
    )


@pytest.mark.asyncio
async def test_subscription_lifecycle_persists_only_digest_and_is_idempotent(
    database_session: AsyncSession,
) -> None:
    repository = SubscriptionRepository(database_session, _codec())
    result = await repository.request_subscription(f"Reader-{uuid.uuid4()}@Example.com")
    assert result.confirmation_token is not None
    raw_confirmation = result.confirmation_token.token

    token_row = await database_session.scalar(
        select(SubscriptionTokenModel).where(
            SubscriptionTokenModel.token_digest == result.confirmation_token.token_digest
        )
    )
    assert token_row is not None
    assert token_row.token_digest != raw_confirmation
    subscription = await database_session.get(SubscriptionModel, token_row.subscription_id)
    assert subscription is not None
    subscription_id = subscription.id
    await database_session.commit()

    await repository.confirm(raw_confirmation)
    with pytest.raises(InvalidSubscriptionTokenError):
        await repository.confirm(raw_confirmation)
    unsubscribe = await repository.issue_unsubscribe_token(subscription_id)
    await repository.unsubscribe(unsubscribe.token)
    await repository.unsubscribe(unsubscribe.token)

    await database_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.UNSUBSCRIBED.value
    event_count = await database_session.scalar(
        select(func.count())
        .select_from(SubscriptionConsentEventModel)
        .where(SubscriptionConsentEventModel.subscription_id == subscription.id)
    )
    assert event_count == 3

    with pytest.raises(InvalidSubscriptionTokenError):
        await repository.confirm(unsubscribe.token)


@pytest.mark.asyncio
async def test_resubscription_increments_generation_and_revokes_old_unsubscribe_token(
    database_session: AsyncSession,
) -> None:
    address = f"resubscribe-{uuid.uuid4()}@example.com"
    repository = SubscriptionRepository(database_session, _codec())
    requested = await repository.request_subscription(address)
    assert requested.confirmation_token is not None
    token_row = await database_session.scalar(
        select(SubscriptionTokenModel).where(
            SubscriptionTokenModel.token_digest == requested.confirmation_token.token_digest
        )
    )
    assert token_row is not None
    subscription = await database_session.get(SubscriptionModel, token_row.subscription_id)
    assert subscription is not None
    subscription_id = subscription.id
    await database_session.commit()

    await repository.confirm(requested.confirmation_token.token)
    unsubscribe = await repository.issue_unsubscribe_token(subscription_id)
    await repository.unsubscribe(unsubscribe.token)
    next_request = await repository.request_subscription(address)

    await database_session.refresh(subscription)
    assert next_request.confirmation_token is not None
    assert subscription.status == SubscriptionStatus.PENDING.value
    assert subscription.consent_generation == 2
    await database_session.commit()
    with pytest.raises(InvalidSubscriptionTokenError):
        await repository.unsubscribe(unsubscribe.token)


@pytest.mark.asyncio
async def test_repeated_pending_request_keeps_generation_and_confirmation_revokes_sibling(
    database_session: AsyncSession,
) -> None:
    repository = SubscriptionRepository(database_session, _codec())
    address = f"siblings-{uuid.uuid4()}@example.com"

    first = await repository.request_subscription(address)
    second = await repository.request_subscription(address)
    assert first.confirmation_token is not None
    assert second.confirmation_token is not None

    await repository.confirm(first.confirmation_token.token)

    with pytest.raises(InvalidSubscriptionTokenError):
        await repository.confirm(second.confirmation_token.token)
    subscription = await database_session.scalar(
        select(SubscriptionModel).where(SubscriptionModel.normalized_email == address)
    )
    sibling = await database_session.scalar(
        select(SubscriptionTokenModel).where(
            SubscriptionTokenModel.token_digest == second.confirmation_token.token_digest
        )
    )
    assert subscription is not None
    assert sibling is not None
    assert subscription.status == SubscriptionStatus.CONFIRMED.value
    assert subscription.consent_generation == 1
    assert sibling.revoked_at is not None


@pytest.mark.asyncio
async def test_expired_confirmation_token_fails_without_state_change(
    database_session: AsyncSession,
) -> None:
    clock = _MutableClock(datetime(2026, 9, 11, 12, tzinfo=UTC))
    repository = SubscriptionRepository(database_session, _codec(), clock=clock)
    address = f"expired-{uuid.uuid4()}@example.com"
    requested = await repository.request_subscription(address)
    assert requested.confirmation_token is not None

    clock.now += timedelta(hours=24)
    with pytest.raises(InvalidSubscriptionTokenError):
        await repository.confirm(requested.confirmation_token.token)

    subscription = await database_session.scalar(
        select(SubscriptionModel).where(SubscriptionModel.normalized_email == address)
    )
    assert subscription is not None
    assert subscription.status == SubscriptionStatus.PENDING.value
    assert subscription.confirmed_at is None


@pytest.mark.asyncio
async def test_current_generation_unsubscribe_siblings_are_idempotent(
    database_session: AsyncSession,
) -> None:
    repository = SubscriptionRepository(database_session, _codec())
    requested = await repository.request_subscription(f"unsubscribe-{uuid.uuid4()}@example.com")
    assert requested.confirmation_token is not None
    confirmation_row = await database_session.scalar(
        select(SubscriptionTokenModel).where(
            SubscriptionTokenModel.token_digest == requested.confirmation_token.token_digest
        )
    )
    assert confirmation_row is not None
    subscription_id = confirmation_row.subscription_id
    await database_session.commit()
    await repository.confirm(requested.confirmation_token.token)

    first = await repository.issue_unsubscribe_token(subscription_id)
    second = await repository.issue_unsubscribe_token(subscription_id)
    await repository.unsubscribe(first.token)
    await repository.unsubscribe(second.token)

    subscription = await database_session.get(SubscriptionModel, subscription_id)
    used_tokens = (
        await database_session.scalars(
            select(SubscriptionTokenModel).where(
                SubscriptionTokenModel.token_digest.in_([first.token_digest, second.token_digest])
            )
        )
    ).all()
    event_count = await database_session.scalar(
        select(func.count())
        .select_from(SubscriptionConsentEventModel)
        .where(SubscriptionConsentEventModel.subscription_id == subscription_id)
    )
    assert subscription is not None
    assert subscription.status == SubscriptionStatus.UNSUBSCRIBED.value
    assert len(used_tokens) == 2
    assert all(token.used_at is not None for token in used_tokens)
    assert event_count == 3


@pytest.mark.asyncio
async def test_trusted_suppression_is_terminal_for_all_public_actions(
    database_session: AsyncSession,
) -> None:
    address = f"suppressed-{uuid.uuid4()}@example.com"
    repository = SubscriptionRepository(database_session, _codec())
    requested = await repository.request_subscription(address)
    assert requested.confirmation_token is not None
    token_row = await database_session.scalar(
        select(SubscriptionTokenModel).where(
            SubscriptionTokenModel.token_digest == requested.confirmation_token.token_digest
        )
    )
    assert token_row is not None
    subscription_id = token_row.subscription_id
    await database_session.commit()

    await repository.suppress(subscription_id, SuppressionReason.ADMINISTRATIVE)
    repeated = await repository.request_subscription(address)

    subscription = await database_session.get(SubscriptionModel, subscription_id)
    assert subscription is not None
    assert subscription.status == SubscriptionStatus.SUPPRESSED.value
    assert repeated.confirmation_token is None
    await database_session.commit()
    with pytest.raises(InvalidSubscriptionTokenError):
        await repository.confirm(requested.confirmation_token.token)


@pytest.mark.asyncio
async def test_concurrent_subscription_requests_converge_on_one_row(
    open_database_session: Callable[[], AbstractAsyncContextManager[AsyncSession]],
) -> None:
    factory = open_database_session
    address = f"concurrent-{uuid.uuid4()}@example.com"

    async def request_once() -> None:
        async with factory() as session:
            await SubscriptionRepository(session, _codec()).request_subscription(address)

    await asyncio.gather(request_once(), request_once())
    async with factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(SubscriptionModel)
            .where(SubscriptionModel.normalized_email == address)
        )
    assert count == 1


@pytest.mark.asyncio
async def test_concurrent_rate_limit_updates_allow_only_the_configured_capacity(
    open_database_session: Callable[[], AbstractAsyncContextManager[AsyncSession]],
) -> None:
    identity = uuid.uuid4().hex

    async def consume_once() -> bool:
        async with open_database_session() as session:
            return await SubscriptionRepository(session, _codec()).consume_rate_limit(
                scope="concurrency_test",
                identity_digest=identity,
                window=timedelta(minutes=10),
                limit=1,
            )

    assert sorted(await asyncio.gather(consume_once(), consume_once())) == [False, True]
