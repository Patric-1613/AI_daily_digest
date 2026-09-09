"""Purpose-bound subscription bearer tokens from accepted ADR 0012."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

ENVELOPE_VERSION = "v1"
TOKEN_RANDOM_BYTES = 32
TOKEN_SEGMENT_LENGTH = 43
MAX_TOKEN_LENGTH = 256
MIN_SIGNING_KEY_BYTES = 32

_KEY_ID = re.compile(r"\A[a-z][a-z0-9_-]{0,31}\Z")
_URLSAFE_SEGMENT = re.compile(r"\A[A-Za-z0-9_-]{43}\Z")


class SubscriptionTokenPurpose(StrEnum):
    """The closed, purpose-specific subscription capability set."""

    CONFIRM = "confirm_subscription"
    UNSUBSCRIBE = "unsubscribe"


class InvalidSubscriptionTokenError(ValueError):
    """A deliberately detail-free public-safe token validation failure."""

    def __init__(self) -> None:
        super().__init__("The subscription token is invalid or no longer usable.")


@dataclass(frozen=True)
class IssuedSubscriptionToken:
    """A newly issued raw capability plus values safe to persist."""

    token: str = field(repr=False)
    token_digest: str
    key_id: str
    purpose: SubscriptionTokenPurpose


@dataclass(frozen=True)
class VerifiedSubscriptionToken:
    """Authenticated token metadata; lifecycle checks still require database state."""

    token_digest: str
    key_id: str
    purpose: SubscriptionTokenPurpose


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_canonical(value: str) -> bytes:
    if _URLSAFE_SEGMENT.fullmatch(value) is None:
        raise InvalidSubscriptionTokenError()
    try:
        decoded = base64.b64decode(value + "=", altchars=b"-_", validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise InvalidSubscriptionTokenError() from exc
    if len(decoded) != TOKEN_RANDOM_BYTES or _encode(decoded) != value:
        raise InvalidSubscriptionTokenError()
    return decoded


class SubscriptionTokenCodec:
    """Issue and authenticate bounded, canonical ADR 0012 token envelopes.

    Verification proves the envelope signature and purpose only. Expiry,
    consent generation, revocation and single-use checks remain persistence
    responsibilities and must happen before a route changes state.
    """

    def __init__(
        self,
        *,
        keys: Mapping[SubscriptionTokenPurpose, Mapping[str, bytes]],
        active_key_ids: Mapping[SubscriptionTokenPurpose, str],
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        copied: dict[SubscriptionTokenPurpose, Mapping[str, bytes]] = {}
        seen_material: set[bytes] = set()
        for purpose in SubscriptionTokenPurpose:
            purpose_keys = dict(keys.get(purpose, {}))
            active = active_key_ids.get(purpose)
            if active is None or active not in purpose_keys:
                raise ValueError(f"active key is not configured for {purpose.value}")
            for key_id, material in purpose_keys.items():
                if _KEY_ID.fullmatch(key_id) is None:
                    raise ValueError("subscription token key IDs must use safe lowercase syntax")
                if len(material) < MIN_SIGNING_KEY_BYTES:
                    raise ValueError("subscription token signing keys must be at least 32 bytes")
                if material in seen_material:
                    raise ValueError("subscription token key material cannot be reused")
                seen_material.add(material)
            copied[purpose] = MappingProxyType(purpose_keys)
        self._keys = MappingProxyType(copied)
        self._active_key_ids = MappingProxyType(dict(active_key_ids))
        self._random_bytes = random_bytes

    def issue(self, purpose: SubscriptionTokenPurpose) -> IssuedSubscriptionToken:
        """Create one authenticated, URL-safe 256-bit bearer capability."""
        key_id = self._active_key_ids[purpose]
        random_segment = _encode(self._random_bytes(TOKEN_RANDOM_BYTES))
        if len(random_segment) != TOKEN_SEGMENT_LENGTH:
            raise RuntimeError("random source did not return exactly 32 bytes")
        unsigned = f"{ENVELOPE_VERSION}.{key_id}.{purpose.value}.{random_segment}"
        signature = _encode(
            hmac.digest(self._keys[purpose][key_id], unsigned.encode("ascii"), "sha256")
        )
        token = f"{unsigned}.{signature}"
        if len(token) > MAX_TOKEN_LENGTH:
            raise RuntimeError("configured key ID makes the token exceed its length bound")
        return IssuedSubscriptionToken(
            token=token,
            token_digest=hashlib.sha256(token.encode("ascii")).hexdigest(),
            key_id=key_id,
            purpose=purpose,
        )

    def verify(
        self,
        token: str,
        *,
        expected_purpose: SubscriptionTokenPurpose,
    ) -> VerifiedSubscriptionToken:
        """Authenticate one envelope, failing every invalid case identically."""
        if not token.isascii() or len(token) > MAX_TOKEN_LENGTH:
            raise InvalidSubscriptionTokenError()
        parts = token.split(".")
        if len(parts) != 5:
            raise InvalidSubscriptionTokenError()
        version, key_id, purpose_value, random_segment, signature_segment = parts
        if version != ENVELOPE_VERSION or purpose_value != expected_purpose.value:
            raise InvalidSubscriptionTokenError()
        if _KEY_ID.fullmatch(key_id) is None:
            raise InvalidSubscriptionTokenError()
        key = self._keys[expected_purpose].get(key_id)
        if key is None:
            raise InvalidSubscriptionTokenError()
        _decode_canonical(random_segment)
        supplied_signature = _decode_canonical(signature_segment)
        unsigned = ".".join(parts[:4]).encode("ascii")
        expected_signature = hmac.digest(key, unsigned, "sha256")
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise InvalidSubscriptionTokenError()
        return VerifiedSubscriptionToken(
            token_digest=hashlib.sha256(token.encode("ascii")).hexdigest(),
            key_id=key_id,
            purpose=expected_purpose,
        )
