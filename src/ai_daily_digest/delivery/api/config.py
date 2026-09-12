"""Validated environment configuration for the deployed Delivery API."""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ai_daily_digest.delivery.api.pagination import MIN_SIGNING_KEY_BYTES
from ai_daily_digest.delivery.subscriptions.resend import (
    ConfirmationDeliveryConfigurationError,
    ResendConfirmationSettings,
)
from ai_daily_digest.delivery.subscriptions.tokens import SubscriptionTokenEnvironment

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_MAX_TRUSTED_PROXY_ENTRIES = 16
# Application-specific settings only. Render supplies FORWARDED_ALLOW_IPS to every Python
# service by default (see scripts/start_render.sh), so its mere presence must never by itself
# signal that an operator has started configuring subscriptions -- only one of these
# application-specific values may do that. See _subscription_production() below.
_SUBSCRIPTION_ACTIVATION_SETTING_NAMES = (
    "SUBSCRIPTION_TOKEN_ENVIRONMENT",
    "SUBSCRIPTION_CONFIRM_KEY_ID",
    "SUBSCRIPTION_CONFIRM_KEY",
    "SUBSCRIPTION_UNSUBSCRIBE_KEY_ID",
    "SUBSCRIPTION_UNSUBSCRIBE_KEY",
    "SUBSCRIPTION_RATE_LIMIT_KEY",
    "EMAIL_PROVIDER_API_KEY",
    "EMAIL_FROM_ADDRESS",
)
# The complete set required once subscriptions are activated: every activation setting above,
# plus the trusted-proxy allowlist.
_SUBSCRIPTION_SETTING_NAMES = (*_SUBSCRIPTION_ACTIVATION_SETTING_NAMES, "FORWARDED_ALLOW_IPS")


def _parse_boolean(*, name: str, value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ValueError(f"{name} must be a boolean value")


def _validate_frontend_origin(value: str) -> str:
    origin = value.strip()
    parsed = urlsplit(origin)
    has_invalid_url_part = (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or bool(parsed.path)
        or bool(parsed.query)
        or bool(parsed.fragment)
    )
    has_unsafe_origin_syntax = (
        parsed.username is not None or parsed.password is not None or "*" in origin or "," in origin
    )
    if has_invalid_url_part or has_unsafe_origin_syntax:
        raise ValueError("FRONTEND_ORIGIN must be one exact HTTP(S) origin without a path")
    if parsed.scheme == "http" and parsed.hostname not in _LOCAL_HOSTS:
        raise ValueError("FRONTEND_ORIGIN must use HTTPS unless it is a local-development origin")
    return origin


@dataclass(frozen=True)
class SubscriptionSecuritySettings:
    environment: SubscriptionTokenEnvironment
    confirmation_key_id: str
    confirmation_key: bytes = field(repr=False)
    unsubscribe_key_id: str
    unsubscribe_key: bytes = field(repr=False)
    rate_limit_key: bytes = field(repr=False)


@dataclass(frozen=True)
class SubscriptionProductionSettings:
    """Complete, fail-closed subscription composition settings."""

    security: SubscriptionSecuritySettings = field(repr=False)
    confirmation_delivery: ResendConfirmationSettings = field(repr=False)
    forwarded_allow_ips: str = field(repr=False)


def _subscription_security(values: Mapping[str, str]) -> SubscriptionSecuritySettings | None:
    names = (
        "SUBSCRIPTION_TOKEN_ENVIRONMENT",
        "SUBSCRIPTION_CONFIRM_KEY_ID",
        "SUBSCRIPTION_CONFIRM_KEY",
        "SUBSCRIPTION_UNSUBSCRIBE_KEY_ID",
        "SUBSCRIPTION_UNSUBSCRIBE_KEY",
        "SUBSCRIPTION_RATE_LIMIT_KEY",
    )
    configured = {name: values.get(name, "").strip() for name in names}
    if not any(configured.values()):
        return None
    if any(not value for value in configured.values()):
        raise ValueError("subscription security configuration is incomplete")
    try:
        environment = SubscriptionTokenEnvironment(configured["SUBSCRIPTION_TOKEN_ENVIRONMENT"])
    except ValueError as exc:
        raise ValueError("SUBSCRIPTION_TOKEN_ENVIRONMENT is invalid") from exc
    keys = (
        configured["SUBSCRIPTION_CONFIRM_KEY"].encode(),
        configured["SUBSCRIPTION_UNSUBSCRIBE_KEY"].encode(),
        configured["SUBSCRIPTION_RATE_LIMIT_KEY"].encode(),
    )
    if any(len(key) < MIN_SIGNING_KEY_BYTES for key in keys):
        raise ValueError("subscription security keys must each be at least 32 bytes")
    return SubscriptionSecuritySettings(
        environment=environment,
        confirmation_key_id=configured["SUBSCRIPTION_CONFIRM_KEY_ID"],
        confirmation_key=keys[0],
        unsubscribe_key_id=configured["SUBSCRIPTION_UNSUBSCRIBE_KEY_ID"],
        unsubscribe_key=keys[1],
        rate_limit_key=keys[2],
    )


def _trusted_proxy_allowlist(value: str) -> str:
    raw_entries = value.strip().split(",")
    if (
        not value.strip()
        or len(raw_entries) > _MAX_TRUSTED_PROXY_ENTRIES
        or any(not entry.strip() for entry in raw_entries)
    ):
        raise ValueError("FORWARDED_ALLOW_IPS must be a bounded IP/CIDR allowlist")

    canonical_entries: list[str] = []
    for raw_entry in raw_entries:
        entry = raw_entry.strip()
        if entry == "*":
            raise ValueError("FORWARDED_ALLOW_IPS cannot trust every proxy")
        if "/" in entry:
            try:
                # Uvicorn's ProxyHeadersMiddleware parses networks with
                # ``strict=True``. Match that boundary so a value accepted by
                # application configuration cannot silently become an
                # untrusted literal when the raw environment value reaches
                # Uvicorn's ``--forwarded-allow-ips`` option.
                network = ipaddress.ip_network(entry, strict=True)
            except ValueError:
                raise ValueError("FORWARDED_ALLOW_IPS contains an invalid IP or CIDR") from None
            if network.prefixlen == 0:
                raise ValueError("FORWARDED_ALLOW_IPS cannot contain an unbounded network")
            canonical = str(network)
        else:
            try:
                address = ipaddress.ip_address(entry)
            except ValueError:
                raise ValueError("FORWARDED_ALLOW_IPS contains an invalid IP or CIDR") from None
            if address.is_unspecified:
                raise ValueError("FORWARDED_ALLOW_IPS cannot contain an unspecified address")
            canonical = str(address)
        if canonical in canonical_entries:
            raise ValueError("FORWARDED_ALLOW_IPS contains a duplicate entry")
        canonical_entries.append(canonical)
    return ",".join(canonical_entries)


def _subscription_production(
    values: Mapping[str, str],
) -> SubscriptionProductionSettings | None:
    configured = {name: values.get(name, "").strip() for name in _SUBSCRIPTION_SETTING_NAMES}
    activated = any(configured[name] for name in _SUBSCRIPTION_ACTIVATION_SETTING_NAMES)
    if not activated:
        # FORWARDED_ALLOW_IPS alone (Render's automatic default, e.g. "*") must never activate
        # subscriptions: no application-specific setting is present, so routes stay disabled
        # regardless of what value Render happens to have injected.
        return None
    if any(not value for value in configured.values()):
        raise ValueError("subscription production configuration is incomplete")

    security = _subscription_security(values)
    if security is None:  # Defensive: the complete-set check above makes this unreachable.
        raise ValueError("subscription production configuration is incomplete")
    try:
        delivery = ResendConfirmationSettings.from_environment(values)
    except ConfirmationDeliveryConfigurationError:
        raise ValueError("subscription confirmation delivery configuration is invalid") from None
    return SubscriptionProductionSettings(
        security=security,
        confirmation_delivery=delivery,
        forwarded_allow_ips=_trusted_proxy_allowlist(configured["FORWARDED_ALLOW_IPS"]),
    )


@dataclass(frozen=True)
class DeliverySettings:
    """Deployment settings loaded explicitly when the Uvicorn factory runs."""

    frontend_origin: str
    docs_enabled: bool = True
    pagination_cursor_secret: bytes | None = field(default=None, repr=False)
    subscription: SubscriptionProductionSettings | None = field(default=None, repr=False)

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> DeliverySettings:
        """Validate settings without performing infrastructure connections."""
        values = os.environ if environ is None else environ
        raw_origin = values.get("FRONTEND_ORIGIN", "")
        if not raw_origin.strip():
            raise ValueError("FRONTEND_ORIGIN is required")

        docs_enabled = _parse_boolean(
            name="DOCS_ENABLED",
            value=values.get("DOCS_ENABLED", "true"),
        )

        raw_cursor_secret = values.get("PAGINATION_CURSOR_SECRET", "")
        cursor_secret = raw_cursor_secret.encode("utf-8") if raw_cursor_secret else None
        if cursor_secret is not None and len(cursor_secret) < MIN_SIGNING_KEY_BYTES:
            raise ValueError(
                f"PAGINATION_CURSOR_SECRET must be at least {MIN_SIGNING_KEY_BYTES} bytes"
            )

        return cls(
            frontend_origin=_validate_frontend_origin(raw_origin),
            docs_enabled=docs_enabled,
            pagination_cursor_secret=cursor_secret,
            subscription=_subscription_production(values),
        )
