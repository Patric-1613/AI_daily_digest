"""Privacy-preserving application service for public subscription actions."""

from __future__ import annotations

from datetime import timedelta

from ai_daily_digest.delivery.subscriptions.repository import (
    SubscriptionRepository,
    keyed_identity_digest,
    network_identity,
    normalize_email,
)


class SubscriptionRateLimitError(RuntimeError):
    pass


class SubscriptionService:
    def __init__(self, repository: SubscriptionRepository, *, rate_limit_key: bytes) -> None:
        if len(rate_limit_key) < 32:
            raise ValueError("subscription rate-limit key must be at least 32 bytes")
        self._repository = repository
        self._rate_limit_key = rate_limit_key

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
        # Raw confirmation capability is deliberately not returned by the HTTP layer.
        await self._repository.request_subscription(normalized)

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
