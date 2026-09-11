"""Side-effect-free Uvicorn factory for the deployed Delivery API."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ai_daily_digest.delivery.api.app import create_app
from ai_daily_digest.delivery.api.config import DeliverySettings
from ai_daily_digest.delivery.api.readiness import DatabaseReadinessProbe
from ai_daily_digest.ingestion.db.repository import PostgresSourceItemRepository
from ai_daily_digest.intelligence.db.repository import PostgresDigestFeedRepository
from ai_daily_digest.shared.config import DatabaseConfig
from ai_daily_digest.shared.db.engine import build_engine, build_session_factory

UVICORN_FACTORY = "ai_daily_digest.delivery.api.production:create_production_app"


def create_production_app() -> FastAPI:
    """Build the HTTP process from validated deployment environment settings."""
    settings = DeliverySettings.from_environment()
    database_config = DatabaseConfig.from_env()
    engine = build_engine(database_config)
    session_factory = build_session_factory(engine)
    database_probe = DatabaseReadinessProbe(
        session_factory,
        timeout_seconds=database_config.readiness_timeout_seconds,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
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
        # Fail closed until issue #53 supplies a confirmation-delivery adapter and
        # configures Uvicorn/Render with an explicit trusted-proxy allowlist. Without
        # both, the API would either discard the only raw confirmation capability or
        # rate-limit every reader under Render's connection-peer identity.
        subscription_service_factory=None,
        lifespan=lifespan,
    )
