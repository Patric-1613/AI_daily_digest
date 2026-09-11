"""Deployment-environment configuration tests."""

from __future__ import annotations

import pytest

from ai_daily_digest.delivery.api.config import DeliverySettings


def test_settings_load_exact_origin_and_optional_runtime_values() -> None:
    secret = "s" * 32

    settings = DeliverySettings.from_environment(
        {
            "FRONTEND_ORIGIN": "https://ai-daily-digest.onrender.com",
            "DOCS_ENABLED": "false",
            "PAGINATION_CURSOR_SECRET": secret,
        }
    )

    assert settings.frontend_origin == "https://ai-daily-digest.onrender.com"
    assert settings.docs_enabled is False
    assert settings.pagination_cursor_secret == secret.encode()
    assert secret not in repr(settings)


def test_cursor_secret_is_optional_until_a_repository_is_configured() -> None:
    settings = DeliverySettings.from_environment({"FRONTEND_ORIGIN": "http://localhost:3000"})

    assert settings.docs_enabled is True
    assert settings.pagination_cursor_secret is None
    assert settings.subscription_security is None


def test_subscription_security_configuration_is_all_or_nothing_and_secret_safe() -> None:
    confirmation_secret = "c" * 32
    unsubscribe_secret = "u" * 32
    rate_limit_secret = "r" * 32
    settings = DeliverySettings.from_environment(
        {
            "FRONTEND_ORIGIN": "https://ai-daily-digest.onrender.com",
            "SUBSCRIPTION_TOKEN_ENVIRONMENT": "prod",
            "SUBSCRIPTION_CONFIRM_KEY_ID": "prod-confirm-2026-09",
            "SUBSCRIPTION_CONFIRM_KEY": confirmation_secret,
            "SUBSCRIPTION_UNSUBSCRIBE_KEY_ID": "prod-unsubscribe-2026-09",
            "SUBSCRIPTION_UNSUBSCRIBE_KEY": unsubscribe_secret,
            "SUBSCRIPTION_RATE_LIMIT_KEY": rate_limit_secret,
        }
    )

    security = settings.subscription_security
    assert security is not None
    assert security.confirmation_key_id == "prod-confirm-2026-09"
    assert security.unsubscribe_key_id == "prod-unsubscribe-2026-09"
    assert confirmation_secret not in repr(settings)
    assert unsubscribe_secret not in repr(settings)
    assert rate_limit_secret not in repr(settings)


def test_partial_or_weak_subscription_security_configuration_fails_closed() -> None:
    with pytest.raises(ValueError, match="incomplete"):
        DeliverySettings.from_environment(
            {
                "FRONTEND_ORIGIN": "https://example.com",
                "SUBSCRIPTION_TOKEN_ENVIRONMENT": "prod",
            }
        )

    with pytest.raises(ValueError, match="at least 32 bytes"):
        DeliverySettings.from_environment(
            {
                "FRONTEND_ORIGIN": "https://example.com",
                "SUBSCRIPTION_TOKEN_ENVIRONMENT": "prod",
                "SUBSCRIPTION_CONFIRM_KEY_ID": "confirm",
                "SUBSCRIPTION_CONFIRM_KEY": "short",
                "SUBSCRIPTION_UNSUBSCRIBE_KEY_ID": "unsubscribe",
                "SUBSCRIPTION_UNSUBSCRIBE_KEY": "short",
                "SUBSCRIPTION_RATE_LIMIT_KEY": "short",
            }
        )


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
