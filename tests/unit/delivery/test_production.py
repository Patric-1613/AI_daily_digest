"""Production application-factory and CORS tests."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

import ai_daily_digest.delivery.api.production as production_module
from ai_daily_digest.delivery.api.config import DeliverySettings
from ai_daily_digest.delivery.api.production import create_production_app
from ai_daily_digest.delivery.subscriptions.resend import (
    ConfirmationDeliveryUnavailableError,
)
from ai_daily_digest.delivery.subscriptions.service import (
    ConfirmationDelivery,
    SubscriptionService,
)

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


class _FakeHttpClient:
    def __init__(self) -> None:
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1


class _FailingConfirmationAdapter:
    def __init__(self, *, settings: object, client: object) -> None:
        del settings, client

    async def send_confirmation(self, *, address: str, token: str) -> None:
        del address, token
        raise ConfirmationDeliveryUnavailableError()


class _DeliveryCallingService:
    def __init__(
        self,
        repository: object,
        *,
        rate_limit_key: bytes,
        confirmation_delivery: ConfirmationDelivery,
    ) -> None:
        del repository, rate_limit_key
        self._confirmation_delivery = confirmation_delivery

    async def request_subscription(self, email: str, network: str) -> None:
        del network
        await self._confirmation_delivery.send_confirmation(
            address=email,
            token="opaque-test-token-that-must-not-leak",
        )

    async def confirm(self, token: str, network: str) -> None:
        del token, network

    async def unsubscribe(self, token: str, network: str) -> None:
        del token, network


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


def _enable_subscriptions(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    values = {
        "SUBSCRIPTION_TOKEN_ENVIRONMENT": "prod",
        "SUBSCRIPTION_CONFIRM_KEY_ID": "prod-confirm-2026-09",
        "SUBSCRIPTION_CONFIRM_KEY": "c" * 32,
        "SUBSCRIPTION_UNSUBSCRIBE_KEY_ID": "prod-unsubscribe-2026-09",
        "SUBSCRIPTION_UNSUBSCRIBE_KEY": "u" * 32,
        "SUBSCRIPTION_RATE_LIMIT_KEY": "r" * 32,
        "EMAIL_PROVIDER_API_KEY": "provider-key-that-must-not-leak",
        "EMAIL_FROM_ADDRESS": "digest@example.com",
        "FORWARDED_ALLOW_IPS": "10.0.0.0/24",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


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


def test_production_factory_keeps_subscription_routes_disabled_when_set_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_production(monkeypatch)

    app = create_production_app()

    paths = app.openapi()["paths"]
    assert "/v1/subscriptions" not in paths
    assert "/v1/subscriptions/confirm" not in paths
    assert "/v1/subscriptions/unsubscribe" not in paths
    assert app.state.subscription_service_factory is None


def test_production_factory_rejects_partial_subscription_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_production(monkeypatch)
    monkeypatch.setenv("SUBSCRIPTION_TOKEN_ENVIRONMENT", "prod")

    with pytest.raises(ValueError, match="subscription production configuration is incomplete"):
        create_production_app()


def test_production_factory_wires_subscription_components_and_closes_http_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _configure_production(monkeypatch)
    configured = _enable_subscriptions(monkeypatch)
    http_client = _FakeHttpClient()
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda: cast(httpx.AsyncClient, http_client),
    )

    app = create_production_app()
    factory = app.state.subscription_service_factory
    assert factory is not None
    service = factory(cast(AsyncSession, _FakeSession()))
    assert isinstance(service, SubscriptionService)
    assert type(service._repository).__name__ == "SubscriptionRepository"  # pylint: disable=protected-access
    assert type(service._confirmation_delivery).__name__ == (  # pylint: disable=protected-access
        "_PrivacyPreservingConfirmationDelivery"
    )

    with TestClient(app) as client:
        paths = client.get("/openapi.json").json()["paths"]
        assert "/v1/subscriptions" in paths
        assert "/v1/subscriptions/confirm" in paths
        assert "/v1/subscriptions/unsubscribe" in paths
        assert client.get("/v1/health/ready").json()["status"] == "ready"
        serialized_public_output = repr(paths)
        for sensitive_name in (
            "SUBSCRIPTION_CONFIRM_KEY",
            "SUBSCRIPTION_UNSUBSCRIBE_KEY",
            "SUBSCRIPTION_RATE_LIMIT_KEY",
            "EMAIL_PROVIDER_API_KEY",
        ):
            assert configured[sensitive_name] not in serialized_public_output
        assert http_client.close_calls == 0
        assert engine.dispose_calls == 0

    assert http_client.close_calls == 1
    assert engine.dispose_calls == 1


def test_provider_failure_keeps_generic_subscription_response_without_leakage(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _configure_production(monkeypatch)
    configured = _enable_subscriptions(monkeypatch)
    http_client = _FakeHttpClient()
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda: cast(httpx.AsyncClient, http_client),
    )
    monkeypatch.setattr(
        production_module,
        "ResendConfirmationDelivery",
        _FailingConfirmationAdapter,
    )
    monkeypatch.setattr(production_module, "SubscriptionService", _DeliveryCallingService)
    caplog.set_level(logging.WARNING, logger=production_module.__name__)

    with TestClient(create_production_app()) as client:
        response = client.post(
            "/v1/subscriptions",
            json={
                "email": "Reader@example.com",
                "consent_to_daily_digest": True,
            },
        )

    assert response.status_code == 202
    assert response.json() == {
        "message": "If the address is eligible, a confirmation email will be sent."
    }
    observable = f"{response.text}\n{caplog.text}"
    for sensitive in (
        "Reader@example.com",
        "opaque-test-token-that-must-not-leak",
        configured["EMAIL_PROVIDER_API_KEY"],
    ):
        assert sensitive not in observable


def test_render_start_command_passes_an_explicit_proxy_allowlist_to_uvicorn() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    start_script = (repository_root / "scripts" / "start_render.sh").read_text()

    assert "--proxy-headers" in start_script
    assert '--forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-}"' in start_script


@pytest.mark.parametrize(
    ("proxy_address", "expect_forwarded_client"),
    [
        ("10.0.0.9", True),
        ("2001:db8::5", True),
        ("192.0.2.10", False),
    ],
)
def test_validated_proxy_allowlist_matches_uvicorn_proxy_trust(
    monkeypatch: pytest.MonkeyPatch,
    proxy_address: str,
    expect_forwarded_client: bool,
) -> None:
    _configure_production(monkeypatch)
    configured = _enable_subscriptions(monkeypatch)
    configured["FORWARDED_ALLOW_IPS"] = "10.0.0.9,2001:db8::/64"
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", configured["FORWARDED_ALLOW_IPS"])
    settings = DeliverySettings.from_environment()
    assert settings.subscription is not None

    app = FastAPI()

    @app.get("/client")
    async def client_address(request: Request) -> dict[str, str | None]:
        return {"host": request.client.host if request.client else None}

    app.add_middleware(
        ProxyHeadersMiddleware,
        trusted_hosts=settings.subscription.forwarded_allow_ips,
    )
    forwarded_client = "203.0.113.25"
    with TestClient(app, client=(proxy_address, 50000)) as client:
        response = client.get("/client", headers={"X-Forwarded-For": forwarded_client})

    expected_client = forwarded_client if expect_forwarded_client else proxy_address
    assert response.json() == {"host": expected_client}


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
