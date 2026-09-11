from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from ai_daily_digest.delivery.api.app import create_app
from ai_daily_digest.delivery.subscriptions.repository import (
    SubscriptionRepository,
    network_identity,
    normalize_email,
)
from ai_daily_digest.delivery.subscriptions.service import (
    SubscriptionRateLimitError,
    SubscriptionService,
)
from ai_daily_digest.delivery.subscriptions.tokens import InvalidSubscriptionTokenError


def test_email_normalization_preserves_local_part_and_normalizes_domain() -> None:
    assert normalize_email("  Reader.Name@EXAMPLE.COM  ") == "Reader.Name@example.com"


@pytest.mark.parametrize(
    "address", ["missing-at", "a@localhost", ".a@example.com", "a b@example.com"]
)
def test_email_normalization_rejects_invalid_values(address: str) -> None:
    with pytest.raises(ValueError, match="invalid email address"):
        normalize_email(address)


def test_network_identity_masks_addresses_before_hashing() -> None:
    assert network_identity("192.0.2.123") == "192.0.2.0/24"
    assert network_identity("2001:db8:abcd:1200::1") == "2001:db8:abcd:1200::/56"


@pytest.mark.asyncio
async def test_service_enforces_address_and_network_limits_without_plaintext_keys() -> None:
    repository = AsyncMock(spec=SubscriptionRepository)
    repository.consume_rate_limit.return_value = True
    service = SubscriptionService(repository, rate_limit_key=b"r" * 32)

    await service.request_subscription("Reader@example.com", "192.0.2.8")

    assert repository.consume_rate_limit.await_count == 2
    serialized_calls = repr(repository.consume_rate_limit.await_args_list)
    assert "Reader@example.com" not in serialized_calls
    assert "192.0.2.0/24" not in serialized_calls
    repository.request_subscription.assert_awaited_once_with("Reader@example.com")


def _client(service: SubscriptionService | AsyncMock) -> TestClient:
    return TestClient(
        create_app(
            subscription_service=service,
            frontend_origin="https://web.example",
        )
    )


def test_subscription_request_returns_generic_privacy_safe_response() -> None:
    service = AsyncMock(spec=SubscriptionService)
    response = _client(service).post(
        "/v1/subscriptions",
        json={"email": "Reader@Example.com", "consent_to_daily_digest": True},
    )
    assert response.status_code == 202
    assert response.json() == {
        "message": "If the address is eligible, a confirmation email will be sent."
    }
    service.request_subscription.assert_awaited_once()
    assert "Reader@Example.com" not in response.text


def test_token_routes_require_exact_frontend_origin() -> None:
    service = AsyncMock(spec=SubscriptionService)
    response = _client(service).post(
        "/v1/subscriptions/confirm",
        json={"token": "opaque"},
        headers={"Origin": "https://attacker.example"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "invalid_request_origin"
    service.confirm.assert_not_awaited()


def test_invalid_token_uses_one_safe_error_envelope() -> None:
    service = AsyncMock(spec=SubscriptionService)
    service.unsubscribe.side_effect = InvalidSubscriptionTokenError()
    response = _client(service).post(
        "/v1/subscriptions/unsubscribe",
        json={"token": "opaque"},
        headers={"Origin": "https://web.example"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_subscription_token"
    assert response.json()["error"]["details"] == {}
    assert "opaque" not in response.text


def test_rate_limit_uses_generic_429() -> None:
    service = AsyncMock(spec=SubscriptionService)
    service.request_subscription.side_effect = SubscriptionRateLimitError()
    response = _client(service).post(
        "/v1/subscriptions",
        json={"email": "reader@example.com", "consent_to_daily_digest": True},
    )
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "subscription_rate_limited"


def test_subscription_payload_rejects_missing_consent_and_extra_fields() -> None:
    service = AsyncMock(spec=SubscriptionService)
    client = _client(service)

    missing_consent = client.post(
        "/v1/subscriptions",
        json={"email": "reader@example.com"},
    )
    extra_field = client.post(
        "/v1/subscriptions",
        json={
            "email": "reader@example.com",
            "consent_to_daily_digest": True,
            "token": "must-not-be-accepted",
        },
    )

    assert missing_consent.status_code == 422
    assert extra_field.status_code == 422
    assert missing_consent.json()["error"]["code"] == "validation_error"
    assert "must-not-be-accepted" not in extra_field.text
    service.request_subscription.assert_not_awaited()
