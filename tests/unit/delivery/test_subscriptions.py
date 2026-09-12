from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from ai_daily_digest.delivery.api.app import create_app
from ai_daily_digest.delivery.subscriptions.models import (
    SubscriptionModel,
    SubscriptionStatus,
    SubscriptionTokenModel,
)
from ai_daily_digest.delivery.subscriptions.repository import (
    SubscriptionConfirmationResult,
    SubscriptionRepository,
    SubscriptionRequestResult,
    network_identity,
    normalize_email,
)
from ai_daily_digest.delivery.subscriptions.service import (
    SubscriptionRateLimitError,
    SubscriptionService,
)
from ai_daily_digest.delivery.subscriptions.tokens import (
    InvalidSubscriptionTokenError,
    IssuedSubscriptionToken,
    SubscriptionTokenCodec,
    SubscriptionTokenEnvironment,
    SubscriptionTokenPurpose,
)


class _AssertingConfirmationDelivery:
    def __init__(
        self,
        *,
        expected_address: str,
        expected_token: str,
        expected_unsubscribe_token: str | None = None,
    ) -> None:
        self.expected_address = expected_address
        self.expected_token_digest = hashlib.sha256(expected_token.encode("ascii")).hexdigest()
        self.calls = 0
        self.expected_unsubscribe_token_digest = (
            hashlib.sha256(expected_unsubscribe_token.encode("ascii")).hexdigest()
            if expected_unsubscribe_token is not None
            else None
        )
        self.unsubscribe_calls = 0

    async def send_confirmation(self, *, address: str, token: str) -> None:
        assert address == self.expected_address
        assert hashlib.sha256(token.encode("ascii")).hexdigest() == self.expected_token_digest
        self.calls += 1

    async def send_unsubscribe(self, *, address: str, token: str) -> None:
        assert address == self.expected_address
        assert self.expected_unsubscribe_token_digest is not None
        assert (
            hashlib.sha256(token.encode("ascii")).hexdigest()
            == self.expected_unsubscribe_token_digest
        )
        self.unsubscribe_calls += 1


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
    raw_token = "raw-confirmation-capability"
    repository.request_subscription.return_value = SubscriptionRequestResult(
        confirmation_token=IssuedSubscriptionToken(
            token=raw_token,
            token_digest="d" * 64,
            key_id="test-confirm-1",
            purpose=SubscriptionTokenPurpose.CONFIRM,
        )
    )
    delivery = _AssertingConfirmationDelivery(
        expected_address="Reader@example.com",
        expected_token=raw_token,
    )
    service = SubscriptionService(
        repository,
        rate_limit_key=b"r" * 32,
        confirmation_delivery=delivery,
    )

    await service.request_subscription("Reader@example.com", "192.0.2.8")

    assert repository.consume_rate_limit.await_count == 2
    serialized_calls = repr(repository.consume_rate_limit.await_args_list)
    assert "Reader@example.com" not in serialized_calls
    assert "192.0.2.0/24" not in serialized_calls
    repository.request_subscription.assert_awaited_once_with("Reader@example.com")
    assert delivery.calls == 1


@pytest.mark.asyncio
async def test_service_does_not_call_delivery_when_no_confirmation_was_issued() -> None:
    repository = AsyncMock(spec=SubscriptionRepository)
    repository.consume_rate_limit.return_value = True
    repository.request_subscription.return_value = SubscriptionRequestResult(
        confirmation_token=None
    )
    delivery = _AssertingConfirmationDelivery(
        expected_address="unused@example.com",
        expected_token="unused",
    )
    service = SubscriptionService(
        repository,
        rate_limit_key=b"r" * 32,
        confirmation_delivery=delivery,
    )

    await service.request_subscription("Reader@example.com", "192.0.2.8")

    assert delivery.calls == 0


@pytest.mark.asyncio
async def test_confirm_delivers_one_purpose_bound_unsubscribe_token_after_commit() -> None:
    repository = AsyncMock(spec=SubscriptionRepository)
    repository.consume_rate_limit.return_value = True
    raw_unsubscribe_token = "raw-unsubscribe-capability"
    result = SubscriptionConfirmationResult(
        address="Reader@example.com",
        unsubscribe_token=IssuedSubscriptionToken(
            token=raw_unsubscribe_token,
            token_digest=hashlib.sha256(raw_unsubscribe_token.encode("ascii")).hexdigest(),
            key_id="test-unsubscribe-1",
            purpose=SubscriptionTokenPurpose.UNSUBSCRIBE,
        ),
    )
    repository.confirm.side_effect = [result, InvalidSubscriptionTokenError()]
    delivery = _AssertingConfirmationDelivery(
        expected_address="Reader@example.com",
        expected_token="unused-confirmation-token",
        expected_unsubscribe_token=raw_unsubscribe_token,
    )
    service = SubscriptionService(
        repository,
        rate_limit_key=b"r" * 32,
        confirmation_delivery=delivery,
    )

    await service.confirm("confirmation-capability", "192.0.2.8")
    with pytest.raises(InvalidSubscriptionTokenError):
        await service.confirm("confirmation-capability", "192.0.2.8")

    assert repository.confirm.await_count == 2
    assert delivery.calls == 0
    assert delivery.unsubscribe_calls == 1


class _FakeTransaction:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def __aenter__(self) -> None:
        self._events.append("begin")

    async def __aexit__(self, *_: object) -> None:
        self._events.append("commit")


class _ConfirmSession:
    def __init__(
        self,
        *,
        subscription: SubscriptionModel,
        confirmation_token: SubscriptionTokenModel,
    ) -> None:
        self._scalar_results: list[object] = [
            subscription.id,
            subscription,
            confirmation_token,
        ]
        self.events: list[str] = []
        self.added: list[object] = []

    def begin(self) -> _FakeTransaction:
        return _FakeTransaction(self.events)

    async def scalar(self, _statement: object) -> object:
        return self._scalar_results.pop(0)

    async def execute(self, _statement: object) -> None:
        self.events.append("execute")

    def add(self, value: object) -> None:
        self.added.append(value)
        self.events.append("add")


@pytest.mark.asyncio
async def test_repository_confirmation_persists_only_unsubscribe_digest_before_return() -> None:
    now = datetime(2026, 9, 13, 10, tzinfo=UTC)
    codec = SubscriptionTokenCodec(
        environment=SubscriptionTokenEnvironment.TEST,
        keys={
            SubscriptionTokenPurpose.CONFIRM: {"test-confirm": b"c" * 32},
            SubscriptionTokenPurpose.UNSUBSCRIBE: {"test-unsubscribe": b"u" * 32},
        },
        active_key_ids={
            SubscriptionTokenPurpose.CONFIRM: "test-confirm",
            SubscriptionTokenPurpose.UNSUBSCRIBE: "test-unsubscribe",
        },
        random_bytes=lambda _: b"r" * 32,
    )
    confirmation = codec.issue(SubscriptionTokenPurpose.CONFIRM)
    subscription_id = uuid.UUID("018d5d5e-1000-7000-8000-000000000001")
    subscription = SubscriptionModel(
        id=subscription_id,
        normalized_email="Reader@example.com",
        status=SubscriptionStatus.PENDING.value,
        consent_generation=1,
        consented_at=now,
        confirmed_at=None,
        unsubscribed_at=None,
        suppressed_at=None,
        suppression_reason=None,
        created_at=now,
        updated_at=now,
    )
    confirmation_row = SubscriptionTokenModel(
        id=uuid.UUID("018d5d5e-1000-7000-8000-000000000002"),
        subscription_id=subscription_id,
        purpose=confirmation.purpose.value,
        token_digest=confirmation.token_digest,
        key_id=confirmation.key_id,
        consent_generation=1,
        created_at=now,
        expires_at=now + timedelta(hours=24),
        used_at=None,
        revoked_at=None,
    )
    session = _ConfirmSession(
        subscription=subscription,
        confirmation_token=confirmation_row,
    )
    repository = SubscriptionRepository(
        cast(AsyncSession, session),
        codec,
        clock=lambda: now,
    )

    result = await repository.confirm(confirmation.token)

    persisted_tokens = [
        value for value in session.added if isinstance(value, SubscriptionTokenModel)
    ]
    assert session.events[-1] == "commit"
    assert len(persisted_tokens) == 1
    persisted = persisted_tokens[0]
    assert persisted.purpose == SubscriptionTokenPurpose.UNSUBSCRIBE.value
    assert persisted.expires_at is None
    assert persisted.token_digest == result.unsubscribe_token.token_digest
    assert persisted.token_digest != result.unsubscribe_token.token
    assert result.unsubscribe_token.token not in repr(result)
    assert result.address not in repr(result)
    codec.verify(
        result.unsubscribe_token.token,
        expected_purpose=SubscriptionTokenPurpose.UNSUBSCRIBE,
    )
    with pytest.raises(InvalidSubscriptionTokenError):
        codec.verify(
            result.unsubscribe_token.token,
            expected_purpose=SubscriptionTokenPurpose.CONFIRM,
        )


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


def test_subscription_request_uses_cf_connecting_ip_on_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("RENDER_SERVICE_TYPE", "web")
    service = AsyncMock(spec=SubscriptionService)

    response = _client(service).post(
        "/v1/subscriptions",
        json={"email": "reader@example.com", "consent_to_daily_digest": True},
        headers={"CF-Connecting-IP": "198.51.100.7", "X-Forwarded-For": "1.2.3.4"},
    )

    assert response.status_code == 202
    service.request_subscription.assert_awaited_once_with("reader@example.com", "198.51.100.7")


def test_subscription_request_fails_closed_without_cf_connecting_ip_on_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("RENDER_SERVICE_TYPE", "web")
    service = AsyncMock(spec=SubscriptionService)

    response = _client(service).post(
        "/v1/subscriptions",
        json={"email": "reader@example.com", "consent_to_daily_digest": True},
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    service.request_subscription.assert_not_awaited()
    assert "reader@example.com" not in response.text


def test_subscription_request_uses_direct_peer_when_not_on_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("RENDER_SERVICE_TYPE", raising=False)
    service = AsyncMock(spec=SubscriptionService)

    response = _client(service).post(
        "/v1/subscriptions",
        json={"email": "reader@example.com", "consent_to_daily_digest": True},
        headers={"CF-Connecting-IP": "198.51.100.7"},
    )

    assert response.status_code == 202
    awaited_network = service.request_subscription.await_args.args[1]
    assert awaited_network != "198.51.100.7"
