"""Network-free tests for the Resend confirmation-delivery adapter."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import traceback
from collections.abc import Callable

import httpx
import pytest

from ai_daily_digest.delivery.subscriptions.resend import (
    REQUEST_TIMEOUT_SECONDS,
    RESEND_EMAILS_URL,
    ConfirmationDeliveryAuthenticationError,
    ConfirmationDeliveryConfigurationError,
    ConfirmationDeliveryError,
    ConfirmationDeliveryMalformedResponseError,
    ConfirmationDeliveryRateLimitError,
    ConfirmationDeliveryRejectedError,
    ConfirmationDeliveryTimeoutError,
    ConfirmationDeliveryUnavailableError,
    ResendConfirmationDelivery,
    ResendConfirmationSettings,
)

API_KEY = "test-provider-key-that-must-not-leak"
FROM_ADDRESS = "digest@example.com"
RECIPIENT = "Reader@example.com"
TOKEN = "v1.test-confirm-1.confirm_subscription.secret-token.signature"
FRONTEND_ORIGIN = "https://digest.example"


def _settings(
    *,
    api_key: str = API_KEY,
    from_address: str = FROM_ADDRESS,
    frontend_origin: str = FRONTEND_ORIGIN,
) -> ResendConfirmationSettings:
    return ResendConfirmationSettings.from_environment(
        {
            "EMAIL_PROVIDER_API_KEY": api_key,
            "EMAIL_FROM_ADDRESS": from_address,
            "FRONTEND_ORIGIN": frontend_origin,
        }
    )


def _adapter(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    settings: ResendConfirmationSettings | None = None,
) -> tuple[ResendConfirmationDelivery, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ResendConfirmationDelivery(settings=settings or _settings(), client=client), client


@pytest.mark.asyncio
async def test_success_builds_expected_recipient_sender_template_and_fragment() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == RESEND_EMAILS_URL
        assert request.method == "POST"
        body = json.loads(request.read())
        assert body["from"] == FROM_ADDRESS
        assert body["to"] == [RECIPIENT]
        assert body["subject"] == "Confirm your AI Daily Digest subscription"
        assert "https://digest.example/subscriptions/confirm#token=" in body["html"]
        assert "https://digest.example/subscriptions/confirm#token=" in body["text"]
        assert "/subscriptions/confirm?token=" not in body["html"]
        assert hmac.compare_digest(request.headers["authorization"], f"Bearer {API_KEY}")
        assert request.extensions["timeout"] == {
            "connect": REQUEST_TIMEOUT_SECONDS,
            "read": REQUEST_TIMEOUT_SECONDS,
            "write": REQUEST_TIMEOUT_SECONDS,
            "pool": REQUEST_TIMEOUT_SECONDS,
        }
        return httpx.Response(200, json={"id": "provider-message-id"})

    adapter, client = _adapter(handler)
    async with client:
        await adapter.send_confirmation(address=RECIPIENT, token=TOKEN)


@pytest.mark.asyncio
async def test_idempotency_key_is_stable_and_contains_no_sensitive_input() -> None:
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers["idempotency-key"])
        return httpx.Response(200, json={"id": "provider-message-id"})

    adapter, client = _adapter(handler)
    async with client:
        await adapter.send_confirmation(address=RECIPIENT, token=TOKEN)
        await adapter.send_confirmation(address=RECIPIENT, token=TOKEN)

    assert len(set(keys)) == 1
    assert RECIPIENT not in keys[0]
    assert TOKEN not in keys[0]
    assert hashlib.sha256(TOKEN.encode("ascii")).hexdigest() in keys[0]


@pytest.mark.asyncio
async def test_fragment_token_is_percent_encoded_and_never_becomes_a_query() -> None:
    token_with_delimiters = "opaque?value&next=one/two"

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        assert "#token=opaque%3Fvalue%26next%3Done%2Ftwo" in body["text"]
        assert "?token=" not in body["text"]
        return httpx.Response(200, json={"id": "provider-message-id"})

    adapter, client = _adapter(handler)
    async with client:
        await adapter.send_confirmation(address=RECIPIENT, token=token_with_delimiters)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected_error"),
    [
        (400, ConfirmationDeliveryRejectedError),
        (401, ConfirmationDeliveryAuthenticationError),
        (403, ConfirmationDeliveryAuthenticationError),
        (429, ConfirmationDeliveryRateLimitError),
        (500, ConfirmationDeliveryUnavailableError),
        (503, ConfirmationDeliveryUnavailableError),
    ],
)
async def test_provider_failures_map_to_safe_typed_errors(
    status_code: int,
    expected_error: type[Exception],
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text="provider detail must not escape")

    adapter, client = _adapter(handler)
    async with client:
        with pytest.raises(expected_error) as captured:
            await adapter.send_confirmation(address=RECIPIENT, token=TOKEN)

    assert "provider detail" not in str(captured.value)


@pytest.mark.asyncio
async def test_provider_failure_is_not_automatically_retried() -> None:
    request_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(503, text="unavailable")

    adapter, client = _adapter(handler)
    async with client:
        with pytest.raises(ConfirmationDeliveryUnavailableError):
            await adapter.send_confirmation(address=RECIPIENT, token=TOKEN)

    assert request_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["not-json", "{}", '{"id":""}', "[]"])
async def test_malformed_success_response_fails_closed(body: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    adapter, client = _adapter(handler)
    async with client:
        with pytest.raises(ConfirmationDeliveryMalformedResponseError):
            await adapter.send_confirmation(address=RECIPIENT, token=TOKEN)


@pytest.mark.asyncio
async def test_timeout_and_transport_failure_are_safe_typed_errors() -> None:
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("provider timeout", request=request)

    timeout_adapter, timeout_client = _adapter(timeout_handler)
    async with timeout_client:
        with pytest.raises(ConfirmationDeliveryTimeoutError):
            await timeout_adapter.send_confirmation(address=RECIPIENT, token=TOKEN)

    def transport_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("provider unavailable", request=request)

    transport_adapter, transport_client = _adapter(transport_handler)
    async with transport_client:
        with pytest.raises(ConfirmationDeliveryUnavailableError):
            await transport_adapter.send_confirmation(address=RECIPIENT, token=TOKEN)


@pytest.mark.asyncio
async def test_failures_and_logs_do_not_expose_sensitive_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("safe timeout", request=request)

    caplog.set_level(logging.DEBUG)
    adapter, client = _adapter(handler)
    async with client:
        with pytest.raises(ConfirmationDeliveryTimeoutError) as captured:
            await adapter.send_confirmation(address=RECIPIENT, token=TOKEN)

    rendered_exception = "".join(
        traceback.format_exception(
            type(captured.value), captured.value, captured.value.__traceback__
        )
    )
    observable_text = f"{caplog.text}\n{captured.value!s}\n{captured.value!r}\n{rendered_exception}"
    for sensitive_value in (RECIPIENT, TOKEN, API_KEY, f"{FRONTEND_ORIGIN}/subscriptions/confirm"):
        assert sensitive_value not in observable_text


@pytest.mark.parametrize(
    "environment",
    [
        {},
        {"EMAIL_PROVIDER_API_KEY": API_KEY},
        {
            "EMAIL_PROVIDER_API_KEY": API_KEY,
            "EMAIL_FROM_ADDRESS": FROM_ADDRESS,
        },
        {
            "EMAIL_PROVIDER_API_KEY": API_KEY,
            "EMAIL_FROM_ADDRESS": FROM_ADDRESS,
            "FRONTEND_ORIGIN": "http://digest.example",
        },
        {
            "EMAIL_PROVIDER_API_KEY": API_KEY,
            "EMAIL_FROM_ADDRESS": FROM_ADDRESS,
            "FRONTEND_ORIGIN": "https://digest.example/path",
        },
        {
            "EMAIL_PROVIDER_API_KEY": API_KEY,
            "EMAIL_FROM_ADDRESS": "digest@EXAMPLE.COM",
            "FRONTEND_ORIGIN": FRONTEND_ORIGIN,
        },
        {
            "EMAIL_PROVIDER_API_KEY": API_KEY,
            "EMAIL_FROM_ADDRESS": FROM_ADDRESS,
            "FRONTEND_ORIGIN": "https://localhost",
        },
        {
            "EMAIL_PROVIDER_API_KEY": API_KEY,
            "EMAIL_FROM_ADDRESS": FROM_ADDRESS,
            "FRONTEND_ORIGIN": "https://127.0.0.1",
        },
        {
            "EMAIL_PROVIDER_API_KEY": API_KEY,
            "EMAIL_FROM_ADDRESS": FROM_ADDRESS,
            "FRONTEND_ORIGIN": "https://digest.example:invalid",
        },
        {
            "EMAIL_PROVIDER_API_KEY": API_KEY,
            "EMAIL_FROM_ADDRESS": "digest@example.com\nBcc: victim@example.com",
            "FRONTEND_ORIGIN": FRONTEND_ORIGIN,
        },
    ],
)
def test_incomplete_or_unsafe_configuration_fails_closed(
    environment: dict[str, str],
) -> None:
    with pytest.raises(ConfirmationDeliveryConfigurationError) as captured:
        ResendConfirmationSettings.from_environment(environment)

    message = str(captured.value)
    assert API_KEY not in message
    assert FROM_ADDRESS not in message
    assert "http://digest.example" not in message


def test_configuration_representation_excludes_provider_key_and_sender() -> None:
    settings = _settings()

    rendered = repr(settings)
    assert API_KEY not in rendered
    assert FROM_ADDRESS not in rendered
    assert FRONTEND_ORIGIN in rendered


def test_configuration_accepts_a_canonical_display_name_sender() -> None:
    sender = "AI Daily Digest <digest@example.com>"

    settings = _settings(from_address=sender)

    assert settings.from_address == sender
    assert sender not in repr(settings)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("address", "token"),
    [
        ("reader@EXAMPLE.COM", TOKEN),
        ("invalid-address", TOKEN),
        (RECIPIENT, ""),
        (RECIPIENT, "x" * 257),
        (RECIPIENT, "non-ascii-£"),
    ],
)
async def test_invalid_sensitive_inputs_fail_before_http(
    address: str,
    token: str,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid input must not reach the HTTP transport")

    adapter, client = _adapter(handler)
    async with client:
        with pytest.raises(ConfirmationDeliveryError, match="Confirmation delivery failed"):
            await adapter.send_confirmation(address=address, token=token)
