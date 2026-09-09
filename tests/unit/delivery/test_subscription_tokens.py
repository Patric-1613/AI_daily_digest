"""Unit tests for ADR 0012's subscription bearer-token boundary."""

from __future__ import annotations

import re
from collections.abc import Callable

import pytest

from ai_daily_digest.delivery.subscriptions.tokens import (
    MAX_TOKEN_LENGTH,
    InvalidSubscriptionTokenError,
    SubscriptionTokenCodec,
    SubscriptionTokenPurpose,
)

CONFIRM_KEY = b"c" * 32
UNSUBSCRIBE_KEY = b"u" * 32


def _codec() -> SubscriptionTokenCodec:
    return SubscriptionTokenCodec(
        keys={
            SubscriptionTokenPurpose.CONFIRM: {"test-confirm-1": CONFIRM_KEY},
            SubscriptionTokenPurpose.UNSUBSCRIBE: {"test-unsubscribe-1": UNSUBSCRIBE_KEY},
        },
        active_key_ids={
            SubscriptionTokenPurpose.CONFIRM: "test-confirm-1",
            SubscriptionTokenPurpose.UNSUBSCRIBE: "test-unsubscribe-1",
        },
        random_bytes=lambda size: b"r" * size,
    )


@pytest.mark.parametrize("purpose", list(SubscriptionTokenPurpose))
def test_round_trip_is_canonical_bounded_and_purpose_bound(
    purpose: SubscriptionTokenPurpose,
) -> None:
    codec = _codec()
    issued = codec.issue(purpose)

    assert len(issued.token) <= MAX_TOKEN_LENGTH
    assert re.fullmatch(r"[A-Za-z0-9_.-]+", issued.token)
    assert len(issued.token.split(".")[3]) == 43
    assert len(issued.token.split(".")[4]) == 43
    assert "token=" not in repr(issued)

    verified = codec.verify(issued.token, expected_purpose=purpose)
    assert verified.token_digest == issued.token_digest
    assert verified.key_id == issued.key_id
    assert verified.purpose is purpose


def test_wrong_purpose_fails_closed() -> None:
    issued = _codec().issue(SubscriptionTokenPurpose.CONFIRM)
    with pytest.raises(InvalidSubscriptionTokenError):
        _codec().verify(issued.token, expected_purpose=SubscriptionTokenPurpose.UNSUBSCRIBE)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda token: token + "x",
        lambda token: token.replace("v1.", "v2.", 1),
        lambda token: token.replace("test-confirm-1", "unknown-key", 1),
        lambda token: token.rsplit(".", 1)[0] + "." + ("A" * 43),
        lambda token: token.replace(".", "", 1),
    ],
)
def test_tampered_or_malformed_tokens_fail_identically(mutate: Callable[[str], str]) -> None:
    codec = _codec()
    issued = codec.issue(SubscriptionTokenPurpose.CONFIRM)
    changed = mutate(issued.token)
    with pytest.raises(InvalidSubscriptionTokenError, match="invalid or no longer usable"):
        codec.verify(changed, expected_purpose=SubscriptionTokenPurpose.CONFIRM)


def test_oversized_token_fails_before_parsing() -> None:
    with pytest.raises(InvalidSubscriptionTokenError):
        _codec().verify(
            "x" * (MAX_TOKEN_LENGTH + 1),
            expected_purpose=SubscriptionTokenPurpose.CONFIRM,
        )


@pytest.mark.parametrize("weak_key", [b"", b"short"])
def test_weak_keys_are_rejected_at_startup(weak_key: bytes) -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        SubscriptionTokenCodec(
            keys={
                SubscriptionTokenPurpose.CONFIRM: {"test-confirm-1": weak_key},
                SubscriptionTokenPurpose.UNSUBSCRIBE: {"test-unsubscribe-1": UNSUBSCRIBE_KEY},
            },
            active_key_ids={
                SubscriptionTokenPurpose.CONFIRM: "test-confirm-1",
                SubscriptionTokenPurpose.UNSUBSCRIBE: "test-unsubscribe-1",
            },
        )


def test_key_material_cannot_be_reused_across_purposes() -> None:
    with pytest.raises(ValueError, match="cannot be reused"):
        SubscriptionTokenCodec(
            keys={
                SubscriptionTokenPurpose.CONFIRM: {"test-confirm-1": CONFIRM_KEY},
                SubscriptionTokenPurpose.UNSUBSCRIBE: {"test-unsubscribe-1": CONFIRM_KEY},
            },
            active_key_ids={
                SubscriptionTokenPurpose.CONFIRM: "test-confirm-1",
                SubscriptionTokenPurpose.UNSUBSCRIBE: "test-unsubscribe-1",
            },
        )
