"""FastAPI application factory for the public Delivery API."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import Lifespan

from ai_daily_digest.delivery.api.dependencies import (
    DigestFeedRepositoryFactory,
    ReadinessProbe,
    ReadinessRegistry,
    SourceItemFeedRepositoryFactory,
    SubscriptionServiceFactory,
    build_readiness_registry,
)
from ai_daily_digest.delivery.api.errors import (
    INTERNAL_ERROR_CODE,
    error_response,
    install_exception_handlers,
)
from ai_daily_digest.delivery.api.pagination import CursorCodec
from ai_daily_digest.delivery.api.routes.digests import router as digests_router
from ai_daily_digest.delivery.api.routes.health import router as health_router
from ai_daily_digest.delivery.api.routes.subscriptions import router as subscriptions_router
from ai_daily_digest.delivery.api.routes.updates import router as updates_router
from ai_daily_digest.delivery.subscriptions.service import SubscriptionService
from ai_daily_digest.shared.ids import new_id
from ai_daily_digest.shared.repositories import DigestFeedRepository, SourceItemFeedRepository

API_TITLE = "AI Daily Digest API"
API_VERSION = "0.1.0"

LOGGER = logging.getLogger(__name__)

type RequestHandler = Callable[[Request], Awaitable[Response]]


def _validate_repository_wiring(
    *,
    fixed_repository: SourceItemFeedRepository | None,
    database_session_factory: async_sessionmaker[AsyncSession] | None,
    repository_factory: SourceItemFeedRepositoryFactory | None,
    database_readiness_probe: ReadinessProbe | None,
) -> bool:
    """Validate repository lifecycle combinations and report whether the route is enabled."""
    has_scoped_repository = repository_factory is not None
    if has_scoped_repository and database_session_factory is None:
        raise ValueError(
            "database_session_factory and source_item_feed_repository_factory "
            "must be configured together"
        )
    if fixed_repository is not None and has_scoped_repository:
        raise ValueError("configure either a fixed or request-scoped repository, not both")
    if has_scoped_repository and database_readiness_probe is None:
        raise ValueError("a database readiness probe is required for a database-backed route")
    return fixed_repository is not None or has_scoped_repository


def _build_readiness_configuration(
    *,
    required_dependencies: Iterable[str],
    readiness_probes: Mapping[str, ReadinessProbe] | None,
    database_readiness_probe: ReadinessProbe | None,
) -> ReadinessRegistry:
    """Merge the optional database probe without hiding duplicate caller input."""
    merged_probes = dict(readiness_probes or {})
    merged_required = tuple(required_dependencies)
    if database_readiness_probe is not None:
        merged_probes["database"] = database_readiness_probe
        if "database" not in merged_required:
            merged_required = (*merged_required, "database")
    return build_readiness_registry(
        required_dependencies=merged_required,
        probes=merged_probes,
    )


def _validate_digest_repository_wiring(
    *,
    fixed_repository: DigestFeedRepository | None,
    database_session_factory: async_sessionmaker[AsyncSession] | None,
    repository_factory: DigestFeedRepositoryFactory | None,
    database_readiness_probe: ReadinessProbe | None,
) -> bool:
    """Validate the optional digest repository without constraining other DB routes."""
    if fixed_repository is not None and repository_factory is not None:
        raise ValueError("configure either a fixed or request-scoped digest repository, not both")
    if repository_factory is not None and database_session_factory is None:
        raise ValueError("database_session_factory is required for the digest repository factory")
    if repository_factory is not None and database_readiness_probe is None:
        raise ValueError("a database readiness probe is required for a database-backed route")
    return fixed_repository is not None or repository_factory is not None


def create_app(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    *,
    docs_enabled: bool = True,
    required_dependencies: Iterable[str] = (),
    readiness_probes: Mapping[str, ReadinessProbe] | None = None,
    database_readiness_probe: ReadinessProbe | None = None,
    source_item_feed_repository: SourceItemFeedRepository | None = None,
    database_session_factory: async_sessionmaker[AsyncSession] | None = None,
    source_item_feed_repository_factory: SourceItemFeedRepositoryFactory | None = None,
    digest_feed_repository: DigestFeedRepository | None = None,
    digest_feed_repository_factory: DigestFeedRepositoryFactory | None = None,
    subscription_service: SubscriptionService | None = None,
    subscription_service_factory: SubscriptionServiceFactory | None = None,
    cursor_codec: CursorCodec | None = None,
    cursor_signing_key: bytes | None = None,
    frontend_origin: str | None = None,
    lifespan: Lifespan[FastAPI] | None = None,
) -> FastAPI:
    """Create an independent, side-effect-free FastAPI application instance.

    `database_readiness_probe` and `source_item_feed_repository` are both
    optional and both `None` by default: a foundation-only app with no
    database configured passes neither, and `required_dependencies` stays
    exactly what the caller passed. When `database_readiness_probe` **is**
    given, it is wired into readiness under the name `"database"` and that
    name is added to the required set automatically (ADR 0002 section 14:
    "When `DATABASE_URL` is configured and any route needs the database,
    `'database'` goes into `required_dependencies`") -- a caller that
    configures the probe does not also need to remember to list
    `"database"` itself.

    `source_item_feed_repository`, when given, is stored on `app.state`
    and mounts `GET /v1/updates` (ADR 0008 PR 4). Production instead
    passes a paired `database_session_factory` and
    `source_item_feed_repository_factory`; the dependency then opens and
    closes one short-lived session per request (ADR 0002 section 13).
    Mounting is
    fail-closed on cursor configuration: a configured repository with
    neither `cursor_codec` nor `cursor_signing_key` raises `ValueError`
    at `create_app()` time rather than serving pagination with no way to
    produce a valid cursor.

    `frontend_origin`, when given, registers `CORSMiddleware` restricted
    to that exact origin without credentials, permitting GET and POST
    methods and standard headers.
    """
    repository_is_configured = _validate_repository_wiring(
        fixed_repository=source_item_feed_repository,
        database_session_factory=database_session_factory,
        repository_factory=source_item_feed_repository_factory,
        database_readiness_probe=database_readiness_probe,
    )
    digest_repository_is_configured = _validate_digest_repository_wiring(
        fixed_repository=digest_feed_repository,
        database_session_factory=database_session_factory,
        repository_factory=digest_feed_repository_factory,
        database_readiness_probe=database_readiness_probe,
    )
    if subscription_service is not None and subscription_service_factory is not None:
        raise ValueError(
            "configure either a fixed or request-scoped subscription service, not both"
        )
    if subscription_service_factory is not None and database_session_factory is None:
        raise ValueError(
            "database_session_factory is required for the subscription service factory"
        )
    subscription_is_configured = (
        subscription_service is not None or subscription_service_factory is not None
    )
    if (
        database_session_factory is not None
        and source_item_feed_repository_factory is None
        and digest_feed_repository_factory is None
        and subscription_service_factory is None
    ):
        raise ValueError("database_session_factory must be configured with a repository factory")

    # A tuple, not `set(required_dependencies)`: build_readiness_registry()
    # itself rejects a duplicate name ("required dependency names must
    # be unique") -- silently deduplicating here would swallow that
    # caller mistake instead of surfacing it. "database" is appended
    # only when it is not already present, so create_app() never
    # introduces a duplicate of its own.
    readiness_registry = _build_readiness_configuration(
        required_dependencies=required_dependencies,
        readiness_probes=readiness_probes,
        database_readiness_probe=database_readiness_probe,
    )
    app = FastAPI(
        title=API_TITLE,
        version=API_VERSION,
        openapi_version="3.1.0",
        openapi_url="/openapi.json",
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        contact=None,
        servers=None,
        lifespan=lifespan,
    )
    app.state.readiness_registry = readiness_registry
    app.state.source_item_feed_repository = source_item_feed_repository
    app.state.database_session_factory = database_session_factory
    app.state.source_item_feed_repository_factory = source_item_feed_repository_factory
    app.state.digest_feed_repository = digest_feed_repository
    app.state.digest_feed_repository_factory = digest_feed_repository_factory
    app.state.subscription_service = subscription_service
    app.state.subscription_service_factory = subscription_service_factory
    app.state.frontend_origin = frontend_origin

    if frontend_origin is not None:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[frontend_origin],
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["Accept", "Content-Type"],
        )

    if cursor_codec is not None:
        app.state.cursor_codec = cursor_codec
    elif cursor_signing_key is not None:
        app.state.cursor_codec = CursorCodec(cursor_signing_key)
    elif repository_is_configured or digest_repository_is_configured:
        raise ValueError(
            "cursor_codec or cursor_signing_key is required when a paginated repository "
            "is configured"
        )
    else:
        app.state.cursor_codec = None

    @app.middleware("http")
    async def add_request_context(request: Request, call_next: RequestHandler) -> Response:
        request.state.request_id = new_id()
        try:
            return await call_next(request)
        # This is the final HTTP safety boundary: unknown application failures
        # must become the generic 500 envelope and must never escape to clients.
        except Exception as exc:  # pylint: disable=broad-exception-caught
            LOGGER.error(
                "Unhandled API exception",
                extra={
                    "request_id": str(request.state.request_id),
                    "exception_type": type(exc).__name__,
                },
            )
            return error_response(
                request,
                status_code=500,
                code=INTERNAL_ERROR_CODE,
                message="An unexpected server error occurred.",
            )

    install_exception_handlers(app)
    app.include_router(health_router)
    if repository_is_configured:
        app.include_router(updates_router)
    if digest_repository_is_configured:
        app.include_router(digests_router)
    if subscription_is_configured:
        app.include_router(subscriptions_router)
    return app
