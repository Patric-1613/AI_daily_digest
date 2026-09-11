"""Privacy-preserving application service for public subscription actions."""

from __future__ import annotations

from datetime import timedelta
from typing import Protocol

from ai_daily_digest.delivery.subscriptions.repository import (
    SubscriptionRepository,
    keyed_identity_digest,
    network_identity,
    normalize_email,
)


class SubscriptionRateLimitError(RuntimeError):
    pass


class ConfirmationDelivery(Protocol):
    """Transient boundary for delivering a newly issued confirmation capability."""

    async def send_confirmation(self, *, address: str, token: str) -> None:
        """Send one confirmation without retaining or logging its sensitive inputs."""


class SubscriptionService:
    def __init__(
        self,
        repository: SubscriptionRepository,
        *,
        rate_limit_key: bytes,
        confirmation_delivery: ConfirmationDelivery,
    ) -> None:
        if len(rate_limit_key) < 32:
            raise ValueError("subscription rate-limit key must be at least 32 bytes")
        self._repository = repository
        self._rate_limit_key = rate_limit_key
        self._confirmation_delivery = confirmation_delivery

    async def request_subscription(self, email: str, network: str) -> None:
        normalized = normalize_email(email)
        await self._require_limit(
            scope="subscribe_address",
            purpose="address",
            value=normalized,
            window=timedelta(hours=1),
            limit=3,
        )
        await self._require_limit(
            scope="subscribe_network",
            purpose="network",
            value=network_identity(network),
            window=timedelta(hours=1),
            limit=20,
        )
        result = await self._repository.request_subscription(normalized)
        if result.confirmation_token is not None:
            # request_subscription() has committed before this provider-neutral I/O boundary.
            # The capability is passed transiently and is never returned by the HTTP layer.
            await self._confirmation_delivery.send_confirmation(
                address=normalized,
                token=result.confirmation_token.token,
            )

    async def confirm(self, token: str, network: str) -> None:
        await self._token_limit(network)
        await self._repository.confirm(token)

    async def unsubscribe(self, token: str, network: str) -> None:
        await self._token_limit(network)
        await self._repository.unsubscribe(token)

    async def _token_limit(self, network: str) -> None:
        await self._require_limit(
            scope="subscription_token_network",
            purpose="network",
            value=network_identity(network),
            window=timedelta(minutes=10),
            limit=30,
        )

    async def _require_limit(
        self, *, scope: str, purpose: str, value: str, window: timedelta, limit: int
    ) -> None:
        digest = keyed_identity_digest(self._rate_limit_key, purpose=purpose, value=value)
        if not await self._repository.consume_rate_limit(
            scope=scope, identity_digest=digest, window=window, limit=limit
        ):
            raise SubscriptionRateLimitError()
