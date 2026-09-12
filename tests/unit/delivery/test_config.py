"""Deployment-environment configuration tests."""

from __future__ import annotations

import pytest

from ai_daily_digest.delivery.api.config import DeliverySettings

FRONTEND_ORIGIN = "https://ai-daily-digest.onrender.com"


def _subscription_environment() -> dict[str, str]:
    return {
        "FRONTEND_ORIGIN": FRONTEND_ORIGIN,
        "SUBSCRIPTION_TOKEN_ENVIRONMENT": "prod",
        "SUBSCRIPTION_CONFIRM_KEY_ID": "prod-confirm-2026-09",
        "SUBSCRIPTION_CONFIRM_KEY": "c" * 32,
        "SUBSCRIPTION_UNSUBSCRIBE_KEY_ID": "prod-unsubscribe-2026-09",
        "SUBSCRIPTION_UNSUBSCRIBE_KEY": "u" * 32,
        "SUBSCRIPTION_RATE_LIMIT_KEY": "r" * 32,
        "EMAIL_PROVIDER_API_KEY": "provider-key-that-must-not-leak",
        "EMAIL_FROM_ADDRESS": "digest@example.com",
        "FORWARDED_ALLOW_IPS": "10.0.0.9, 2001:db8::/64",
    }


def test_settings_load_exact_origin_and_optional_runtime_values() -> None:
    secret = "s" * 32

    settings = DeliverySettings.from_environment(
        {
            "FRONTEND_ORIGIN": FRONTEND_ORIGIN,
            "DOCS_ENABLED": "false",
            "PAGINATION_CURSOR_SECRET": secret,
        }
    )

    assert settings.frontend_origin == FRONTEND_ORIGIN
    assert settings.docs_enabled is False
    assert settings.pagination_cursor_secret == secret.encode()
    assert secret not in repr(settings)


def test_cursor_secret_is_optional_until_a_repository_is_configured() -> None:
    settings = DeliverySettings.from_environment({"FRONTEND_ORIGIN": "http://localhost:3000"})

    assert settings.docs_enabled is True
    assert settings.pagination_cursor_secret is None
    assert settings.subscription is None


def test_subscription_production_configuration_is_complete_and_secret_safe() -> None:
    environment = _subscription_environment()
    settings = DeliverySettings.from_environment(environment)

    subscription = settings.subscription
    assert subscription is not None
    security = subscription.security
    assert security.confirmation_key_id == "prod-confirm-2026-09"
    assert security.unsubscribe_key_id == "prod-unsubscribe-2026-09"
    assert subscription.confirmation_delivery.frontend_origin == FRONTEND_ORIGIN
    assert subscription.forwarded_allow_ips == "10.0.0.9,2001:db8::/64"
    for sensitive_name in (
        "SUBSCRIPTION_CONFIRM_KEY",
        "SUBSCRIPTION_UNSUBSCRIBE_KEY",
        "SUBSCRIPTION_RATE_LIMIT_KEY",
        "EMAIL_PROVIDER_API_KEY",
        "EMAIL_FROM_ADDRESS",
    ):
        assert environment[sensitive_name] not in repr(settings)


def test_partial_or_weak_subscription_security_configuration_fails_closed() -> None:
    with pytest.raises(ValueError, match="incomplete"):
        DeliverySettings.from_environment(
            {
                "FRONTEND_ORIGIN": "https://example.com",
                "SUBSCRIPTION_TOKEN_ENVIRONMENT": "prod",
            }
        )

    weak_environment = _subscription_environment()
    weak_environment["SUBSCRIPTION_CONFIRM_KEY"] = "short"
    with pytest.raises(ValueError, match="at least 32 bytes"):
        DeliverySettings.from_environment(weak_environment)


def test_provider_or_proxy_configuration_without_the_security_set_fails_closed() -> None:
    with pytest.raises(ValueError, match="incomplete"):
        DeliverySettings.from_environment(
            {
                "FRONTEND_ORIGIN": FRONTEND_ORIGIN,
                "EMAIL_PROVIDER_API_KEY": "provider-key",
                "EMAIL_FROM_ADDRESS": "digest@example.com",
                "FORWARDED_ALLOW_IPS": "10.0.0.9",
            }
        )


@pytest.mark.parametrize(
    "allowlist",
    [
        "*",
        "0.0.0.0/0",
        "::/0",
        "proxy.internal",
        "10.0.0.9,,10.0.0.10",
        "10.0.0.9,10.0.0.9",
    ],
)
def test_wildcard_unbounded_or_malformed_proxy_configuration_fails_closed(
    allowlist: str,
) -> None:
    environment = _subscription_environment()
    environment["FORWARDED_ALLOW_IPS"] = allowlist

    with pytest.raises(ValueError, match="FORWARDED_ALLOW_IPS"):
        DeliverySettings.from_environment(environment)


@pytest.mark.parametrize(
    "origin",
    [
        "",
        "*",
        "https://*.example.com",
        "https://one.example,https://two.example",
        "https://user:password@example.com",
        "https://example.com/path",
        "https://example.com/",
        "https://example.com?query=yes",
        "http://example.com",
    ],
)
def test_invalid_or_non_exact_frontend_origins_fail_closed(origin: str) -> None:
    with pytest.raises(ValueError, match="FRONTEND_ORIGIN"):
        DeliverySettings.from_environment({"FRONTEND_ORIGIN": origin})


def test_invalid_boolean_and_short_cursor_secret_are_rejected() -> None:
    with pytest.raises(ValueError, match="DOCS_ENABLED"):
        DeliverySettings.from_environment(
            {"FRONTEND_ORIGIN": "https://example.com", "DOCS_ENABLED": "sometimes"}
        )

    with pytest.raises(ValueError, match="PAGINATION_CURSOR_SECRET"):
        DeliverySettings.from_environment(
            {
                "FRONTEND_ORIGIN": "https://example.com",
                "PAGINATION_CURSOR_SECRET": "too-short",
            }
        )
