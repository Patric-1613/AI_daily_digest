"""Production application-factory and CORS tests."""

from __future__ import annotations

from typing import cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import ai_daily_digest.delivery.api.production as production_module
from ai_daily_digest.delivery.api.production import create_production_app

FRONTEND_ORIGIN = "https://ai-daily-digest.onrender.com"


class _FakeSession:
    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def execute(self, _: object) -> None:
        return None


class _FakeSessionFactory:
    def __call__(self) -> _FakeSession:
        return _FakeSession()


class _FakeEngine:
    def __init__(self) -> None:
        self.dispose_calls = 0

    async def dispose(self) -> None:
        self.dispose_calls += 1


def _configure_production(monkeypatch: pytest.MonkeyPatch) -> _FakeEngine:
    monkeypatch.setenv("FRONTEND_ORIGIN", FRONTEND_ORIGIN)
    monkeypatch.setenv("PAGINATION_CURSOR_SECRET", "s" * 32)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:password@database.internal/digest")
    engine = _FakeEngine()
    session_factory = _FakeSessionFactory()
    monkeypatch.setattr(
        production_module,
        "build_engine",
        lambda _: cast(AsyncEngine, engine),
    )
    monkeypatch.setattr(
        production_module,
        "build_session_factory",
        lambda _: cast(async_sessionmaker[AsyncSession], session_factory),
    )
    return engine


def test_production_module_has_no_process_global_app() -> None:
    assert not hasattr(production_module, "app")


def test_production_factory_wires_database_updates_and_readiness_without_connecting_at_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_production(monkeypatch)
    monkeypatch.setenv("DOCS_ENABLED", "false")

    client = TestClient(create_production_app())

    assert client.get("/v1/health/live").json() == {"status": "ok"}
    assert client.get("/v1/health/ready").json() == {
        "status": "ready",
        "checks": [{"name": "database", "status": "ready"}],
    }
    assert client.get("/docs").status_code == 404
    openapi = client.get("/openapi.json").json()
    assert "/v1/updates" in openapi["paths"]


def test_production_lifespan_disposes_the_single_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _configure_production(monkeypatch)

    with TestClient(create_production_app()) as client:
        assert client.get("/v1/health/live").status_code == 200
        assert engine.dispose_calls == 0

    assert engine.dispose_calls == 1


def test_production_factory_keeps_subscription_routes_disabled_until_delivery_and_proxy_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_production(monkeypatch)
    confirmation_secret = "c" * 32
    unsubscribe_secret = "u" * 32
    rate_limit_secret = "r" * 32
    monkeypatch.setenv("SUBSCRIPTION_TOKEN_ENVIRONMENT", "prod")
    monkeypatch.setenv("SUBSCRIPTION_CONFIRM_KEY_ID", "prod-confirm-2026-09")
    monkeypatch.setenv("SUBSCRIPTION_CONFIRM_KEY", confirmation_secret)
    monkeypatch.setenv("SUBSCRIPTION_UNSUBSCRIBE_KEY_ID", "prod-unsubscribe-2026-09")
    monkeypatch.setenv("SUBSCRIPTION_UNSUBSCRIBE_KEY", unsubscribe_secret)
    monkeypatch.setenv("SUBSCRIPTION_RATE_LIMIT_KEY", rate_limit_secret)

    app = create_production_app()

    paths = app.openapi()["paths"]
    assert "/v1/subscriptions" not in paths
    assert "/v1/subscriptions/confirm" not in paths
    assert "/v1/subscriptions/unsubscribe" not in paths
    assert app.state.subscription_service_factory is None
    assert confirmation_secret not in repr(app.state)
    assert unsubscribe_secret not in repr(app.state)
    assert rate_limit_secret not in repr(app.state)


def test_cors_allows_only_configured_origin_and_never_allows_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_production(monkeypatch)
    client = TestClient(create_production_app())

    allowed = client.options(
        "/v1/health/live",
        headers={
            "Origin": FRONTEND_ORIGIN,
            "Access-Control-Request-Method": "GET",
        },
    )
    lookalike = client.options(
        "/v1/health/live",
        headers={
            "Origin": f"{FRONTEND_ORIGIN}.attacker.example",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == FRONTEND_ORIGIN
    assert "access-control-allow-credentials" not in allowed.headers
    assert lookalike.status_code == 400
    assert "access-control-allow-origin" not in lookalike.headers
    assert "access-control-allow-credentials" not in lookalike.headers


def test_production_factory_rejects_missing_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FRONTEND_ORIGIN", raising=False)

    with pytest.raises(ValueError, match="FRONTEND_ORIGIN is required"):
        create_production_app()


def test_production_factory_requires_database_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FRONTEND_ORIGIN", FRONTEND_ORIGIN)
    monkeypatch.setenv("PAGINATION_CURSOR_SECRET", "s" * 32)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValueError, match="DATABASE_URL is not set"):
        create_production_app()


class _FakeDatabaseProbe:
    async def is_ready(self) -> bool:
        return True


def test_create_app_wires_both_database_readiness_probe_and_cors_middleware() -> None:
    """Prove create_app retains dual contract: database probe in readiness and CORS middleware."""
    from ai_daily_digest.delivery.api.app import create_app

    app = create_app(
        database_readiness_probe=_FakeDatabaseProbe(),
        frontend_origin=FRONTEND_ORIGIN,
    )

    registry = app.state.readiness_registry
    assert "database" in registry.required_dependencies
    assert "database" in registry.probes

    client = TestClient(app)
    ready_resp = client.get("/v1/health/ready")
    assert ready_resp.status_code == 200
    assert ready_resp.json() == {
        "status": "ready",
        "checks": [{"name": "database", "status": "ready"}],
    }

    cors_resp = client.options(
        "/v1/health/live",
        headers={
            "Origin": FRONTEND_ORIGIN,
            "Access-Control-Request-Method": "GET",
        },
    )
    assert cors_resp.status_code == 200
    assert cors_resp.headers["access-control-allow-origin"] == FRONTEND_ORIGIN
    assert "access-control-allow-credentials" not in cors_resp.headers
