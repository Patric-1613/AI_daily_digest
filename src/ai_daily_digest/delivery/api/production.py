"""Side-effect-free Uvicorn factory for the deployed Delivery API."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from ai_daily_digest.delivery.api.app import create_app
from ai_daily_digest.delivery.api.config import DeliverySettings, SubscriptionProductionSettings
from ai_daily_digest.delivery.api.dependencies import SubscriptionServiceFactory
from ai_daily_digest.delivery.api.readiness import DatabaseReadinessProbe
from ai_daily_digest.delivery.subscriptions.repository import SubscriptionRepository
from ai_daily_digest.delivery.subscriptions.resend import (
    ConfirmationDeliveryError,
    ResendConfirmationDelivery,
)
from ai_daily_digest.delivery.subscriptions.service import ConfirmationDelivery, SubscriptionService
from ai_daily_digest.delivery.subscriptions.tokens import (
    SubscriptionTokenCodec,
    SubscriptionTokenPurpose,
)
from ai_daily_digest.ingestion.db.repository import PostgresSourceItemRepository
from ai_daily_digest.intelligence.db.repository import PostgresDigestFeedRepository
from ai_daily_digest.shared.config import DatabaseConfig
from ai_daily_digest.shared.db.engine import build_engine, build_session_factory

UVICORN_FACTORY = "ai_daily_digest.delivery.api.production:create_production_app"
LOGGER = logging.getLogger(__name__)


class _PrivacyPreservingConfirmationDelivery:
    """Keep provider failures indistinguishable at the public subscribe boundary."""

    def __init__(self, delegate: ConfirmationDelivery) -> None:
        self._delegate = delegate

    async def send_confirmation(self, *, address: str, token: str) -> None:
        try:
            await self._delegate.send_confirmation(address=address, token=token)
        except ConfirmationDeliveryError as exc:
            LOGGER.warning(
                "Subscription confirmation delivery failed",
                extra={"delivery_error": type(exc).__name__},
            )


def _build_token_codec(settings: SubscriptionProductionSettings) -> SubscriptionTokenCodec:
    security = settings.security
    return SubscriptionTokenCodec(
        environment=security.environment,
        keys={
            SubscriptionTokenPurpose.CONFIRM: {
                security.confirmation_key_id: security.confirmation_key
            },
            SubscriptionTokenPurpose.UNSUBSCRIBE: {
                security.unsubscribe_key_id: security.unsubscribe_key
            },
        },
        active_key_ids={
            SubscriptionTokenPurpose.CONFIRM: security.confirmation_key_id,
            SubscriptionTokenPurpose.UNSUBSCRIBE: security.unsubscribe_key_id,
        },
    )


def _build_subscription_service_factory(
    settings: SubscriptionProductionSettings,
    *,
    codec: SubscriptionTokenCodec,
    http_client: httpx.AsyncClient,
) -> SubscriptionServiceFactory:
    confirmation_delivery = _PrivacyPreservingConfirmationDelivery(
        ResendConfirmationDelivery(
            settings=settings.confirmation_delivery,
            client=http_client,
        )
    )

    def build_service(session: AsyncSession) -> SubscriptionService:
        repository = SubscriptionRepository(session, codec)
        return SubscriptionService(
            repository,
            rate_limit_key=settings.security.rate_limit_key,
            confirmation_delivery=confirmation_delivery,
        )

    return build_service


def create_production_app() -> FastAPI:
    """Build the HTTP process from validated deployment environment settings."""
    settings = DeliverySettings.from_environment()
    database_config = DatabaseConfig.from_env()
    token_codec = _build_token_codec(settings.subscription) if settings.subscription else None
    engine = build_engine(database_config)
    session_factory = build_session_factory(engine)
    database_probe = DatabaseReadinessProbe(
        session_factory,
        timeout_seconds=database_config.readiness_timeout_seconds,
    )
    confirmation_http_client = httpx.AsyncClient() if settings.subscription else None
    subscription_service_factory = (
        _build_subscription_service_factory(
            settings.subscription,
            codec=token_codec,
            http_client=confirmation_http_client,
        )
        if settings.subscription is not None
        and token_codec is not None
        and confirmation_http_client is not None
        else None
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            try:
                if confirmation_http_client is not None:
                    await confirmation_http_client.aclose()
            finally:
                await engine.dispose()

    return create_app(
        docs_enabled=settings.docs_enabled,
        cursor_signing_key=settings.pagination_cursor_secret,
        frontend_origin=settings.frontend_origin,
        database_readiness_probe=database_probe,
        database_session_factory=session_factory,
        source_item_feed_repository_factory=PostgresSourceItemRepository,
        digest_feed_repository_factory=PostgresDigestFeedRepository,
        subscription_service_factory=subscription_service_factory,
        lifespan=lifespan,
    )
