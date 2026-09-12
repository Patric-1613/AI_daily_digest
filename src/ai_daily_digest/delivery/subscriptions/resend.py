"""Resend HTTPS adapter for transient subscription lifecycle email delivery."""

from __future__ import annotations

import hashlib
import html
import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from email.utils import formataddr, parseaddr
from urllib.parse import quote, urlsplit

import httpx

from ai_daily_digest.delivery.subscriptions.repository import normalize_email
from ai_daily_digest.delivery.subscriptions.tokens import MAX_TOKEN_LENGTH

RESEND_EMAILS_URL = "https://api.resend.com/emails"
CONFIRMATION_PATH = "/subscriptions/confirm"
UNSUBSCRIBE_PATH = "/subscriptions/unsubscribe"
REQUEST_TIMEOUT_SECONDS = 5.0


class ConfirmationDeliveryError(RuntimeError):
    """Base class for privacy-safe confirmation-delivery failures."""

    def __init__(self, message: str = "Confirmation delivery failed.") -> None:
        super().__init__(message)


class ConfirmationDeliveryAuthenticationError(ConfirmationDeliveryError):
    """The provider rejected its server-side credential."""


class ConfirmationDeliveryRateLimitError(ConfirmationDeliveryError):
    """The provider refused the request because its rate limit was reached."""


class ConfirmationDeliveryTimeoutError(ConfirmationDeliveryError):
    """The provider did not produce a result within the bounded timeout."""


class ConfirmationDeliveryUnavailableError(ConfirmationDeliveryError):
    """The provider was unreachable or reported a transient server failure."""


class ConfirmationDeliveryRejectedError(ConfirmationDeliveryError):
    """The provider rejected a well-formed delivery request."""


class ConfirmationDeliveryMalformedResponseError(ConfirmationDeliveryError):
    """The provider reported success without a usable delivery identifier."""


class ConfirmationDeliveryConfigurationError(ConfirmationDeliveryError):
    """Required adapter configuration is missing or unsafe."""

    def __init__(self) -> None:
        super().__init__("Confirmation delivery configuration is invalid.")


@dataclass(frozen=True)
class _DeliveryTemplate:
    path: str
    purpose: str
    subject: str
    introduction: str
    action_label: str
    closing: str


_CONFIRMATION_TEMPLATE = _DeliveryTemplate(
    path=CONFIRMATION_PATH,
    purpose="confirmation",
    subject="Confirm your AI Daily Digest subscription",
    introduction="Confirm your AI Daily Digest subscription.",
    action_label="Confirm subscription",
    closing="If you did not request this, you can ignore this email.",
)
_UNSUBSCRIBE_TEMPLATE = _DeliveryTemplate(
    path=UNSUBSCRIBE_PATH,
    purpose="unsubscribe",
    subject="Your AI Daily Digest unsubscribe link",
    introduction="Your AI Daily Digest subscription is confirmed.",
    action_label="Unsubscribe",
    closing="Keep this email so you can unsubscribe at any time.",
)


@dataclass(frozen=True)
class ResendConfirmationSettings:
    """Validated adapter-only settings using the repository's existing names."""

    api_key: str = field(repr=False)
    from_address: str = field(repr=False)
    frontend_origin: str

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> ResendConfirmationSettings:
        """Load only the values required by the confirmation adapter."""
        values = os.environ if environ is None else environ
        api_key = values.get("EMAIL_PROVIDER_API_KEY", "").strip()
        from_address = values.get("EMAIL_FROM_ADDRESS", "").strip()
        frontend_origin = values.get("FRONTEND_ORIGIN", "").strip()
        if not api_key or not from_address or not frontend_origin:
            raise ConfirmationDeliveryConfigurationError()

        return cls(
            api_key=api_key,
            from_address=_validated_sender(from_address),
            frontend_origin=_validated_public_origin(frontend_origin),
        )


def _validated_sender(value: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ConfirmationDeliveryConfigurationError()
    display_name, mailbox = parseaddr(value)
    try:
        normalized_mailbox = normalize_email(mailbox)
    except ValueError:
        raise ConfirmationDeliveryConfigurationError() from None
    canonical = (
        formataddr((display_name, normalized_mailbox)) if display_name else normalized_mailbox
    )
    if canonical != value:
        raise ConfirmationDeliveryConfigurationError()
    return value


def _validated_public_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        raise ConfirmationDeliveryConfigurationError() from None
    hostname = parsed.hostname
    is_local_hostname = hostname == "localhost" or (
        hostname is not None and hostname.endswith(".localhost")
    )
    is_non_public_ip = hostname is not None and _is_non_public_ip(hostname)
    invalid = (
        parsed.scheme != "https"
        or hostname is None
        or bool(parsed.path)
        or bool(parsed.query)
        or bool(parsed.fragment)
        or parsed.username is not None
        or parsed.password is not None
        or "*" in value
        or "," in value
        or is_local_hostname
        or is_non_public_ip
    )
    if invalid:
        raise ConfirmationDeliveryConfigurationError()
    return value


def _is_non_public_ip(hostname: str) -> bool:
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return not address.is_global


def _subscription_action_url(*, frontend_origin: str, path: str, token: str) -> str:
    if not token or not token.isascii() or len(token) > MAX_TOKEN_LENGTH:
        raise ConfirmationDeliveryError()
    encoded_token = quote(token, safe="")
    return f"{frontend_origin}{path}#token={encoded_token}"


def _idempotency_key(*, purpose: str, token: str) -> str:
    token_digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    return f"subscription-{purpose}-{token_digest}"


class ResendConfirmationDelivery:
    """Deliver subscription lifecycle links without retaining sensitive inputs.

    The caller owns the injected client lifecycle. This adapter deliberately
    performs one attempt only. Its stable, non-sensitive idempotency key lets a
    later delivery policy safely repeat an uncertain request without exposing
    the recipient or bearer capability.
    """

    def __init__(
        self,
        *,
        settings: ResendConfirmationSettings,
        client: httpx.AsyncClient,
    ) -> None:
        self._settings = settings
        self._client = client

    async def send_confirmation(self, *, address: str, token: str) -> None:
        """Submit one bounded confirmation-email request."""
        await self._send(
            address=address,
            token=token,
            template=_CONFIRMATION_TEMPLATE,
        )

    async def send_unsubscribe(self, *, address: str, token: str) -> None:
        """Submit one bounded unsubscribe-link email request after confirmation."""
        await self._send(
            address=address,
            token=token,
            template=_UNSUBSCRIBE_TEMPLATE,
        )

    async def _send(
        self,
        *,
        address: str,
        token: str,
        template: _DeliveryTemplate,
    ) -> None:
        """Submit one bounded HTTPS request and validate the provider result."""
        try:
            recipient = normalize_email(address)
        except ValueError:
            raise ConfirmationDeliveryError() from None
        if recipient != address:
            raise ConfirmationDeliveryError()

        action_url = _subscription_action_url(
            frontend_origin=self._settings.frontend_origin,
            path=template.path,
            token=token,
        )
        safe_html_url = html.escape(action_url, quote=True)
        request_body = {
            "from": self._settings.from_address,
            "to": [recipient],
            "subject": template.subject,
            "html": (
                f"<p>{html.escape(template.introduction)}</p>"
                f'<p><a href="{safe_html_url}">{html.escape(template.action_label)}</a></p>'
                f"<p>{html.escape(template.closing)}</p>"
            ),
            "text": f"{template.introduction}\n{action_url}\n\n{template.closing}",
        }
        headers = {
            "Authorization": f"Bearer {self._settings.api_key}",
            "Idempotency-Key": _idempotency_key(purpose=template.purpose, token=token),
        }

        try:
            response = await self._client.post(
                RESEND_EMAILS_URL,
                headers=headers,
                json=request_body,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.TimeoutException:
            raise ConfirmationDeliveryTimeoutError() from None
        except httpx.RequestError:
            raise ConfirmationDeliveryUnavailableError() from None

        if response.status_code in {401, 403}:
            raise ConfirmationDeliveryAuthenticationError()
        if response.status_code == 429:
            raise ConfirmationDeliveryRateLimitError()
        if 500 <= response.status_code <= 599:
            raise ConfirmationDeliveryUnavailableError()
        if not 200 <= response.status_code <= 299:
            raise ConfirmationDeliveryRejectedError()

        try:
            provider_result = response.json()
        except ValueError:
            raise ConfirmationDeliveryMalformedResponseError() from None
        if (
            not isinstance(provider_result, dict)
            or not isinstance(provider_result.get("id"), str)
            or not provider_result["id"].strip()
        ):
            raise ConfirmationDeliveryMalformedResponseError()
