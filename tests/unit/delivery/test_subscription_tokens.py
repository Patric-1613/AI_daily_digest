"""Unit tests for ADR 0012's subscription bearer-token boundary."""

from __future__ import annotations

import re
from collections.abc import Callable

import pytest

from ai_daily_digest.delivery.subscriptions.tokens import (
    MAX_TOKEN_LENGTH,
    InvalidSubscriptionTokenError,
    SubscriptionTokenCodec,
    SubscriptionTokenEnvironment,
    SubscriptionTokenPurpose,
)

CONFIRM_KEY = b"c" * 32
UNSUBSCRIBE_KEY = b"u" * 32


def _codec() -> SubscriptionTokenCodec:
    return SubscriptionTokenCodec(
        environment=SubscriptionTokenEnvironment.TEST,
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
            environment=SubscriptionTokenEnvironment.TEST,
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
            environment=SubscriptionTokenEnvironment.TEST,
            keys={
                SubscriptionTokenPurpose.CONFIRM: {"test-confirm-1": CONFIRM_KEY},
                SubscriptionTokenPurpose.UNSUBSCRIBE: {"test-unsubscribe-1": CONFIRM_KEY},
            },
            active_key_ids={
                SubscriptionTokenPurpose.CONFIRM: "test-confirm-1",
                SubscriptionTokenPurpose.UNSUBSCRIBE: "test-unsubscribe-1",
            },
        )


@pytest.mark.parametrize(
    "active_key_ids",
    [
        {SubscriptionTokenPurpose.UNSUBSCRIBE: "test-unsubscribe-1"},
        {
            SubscriptionTokenPurpose.CONFIRM: "test-confirm-missing",
            SubscriptionTokenPurpose.UNSUBSCRIBE: "test-unsubscribe-1",
        },
    ],
)
def test_missing_or_unknown_active_key_is_rejected(
    active_key_ids: dict[SubscriptionTokenPurpose, str],
) -> None:
    with pytest.raises(ValueError, match="active key is not configured"):
        SubscriptionTokenCodec(
            environment=SubscriptionTokenEnvironment.TEST,
            keys={
                SubscriptionTokenPurpose.CONFIRM: {"test-confirm-1": CONFIRM_KEY},
                SubscriptionTokenPurpose.UNSUBSCRIBE: {"test-unsubscribe-1": UNSUBSCRIBE_KEY},
            },
            active_key_ids=active_key_ids,
        )


def test_malformed_key_id_is_rejected_during_verification() -> None:
    codec = _codec()
    issued = codec.issue(SubscriptionTokenPurpose.CONFIRM)
    malformed = issued.token.replace("test-confirm-1", "TEST-confirm-1", 1)

    with pytest.raises(InvalidSubscriptionTokenError):
        codec.verify(malformed, expected_purpose=SubscriptionTokenPurpose.CONFIRM)


def test_retired_key_remains_verification_only() -> None:
    retired_key = b"r" * 32
    old_codec = SubscriptionTokenCodec(
        environment=SubscriptionTokenEnvironment.TEST,
        keys={
            SubscriptionTokenPurpose.CONFIRM: {"test-confirm-retired": retired_key},
            SubscriptionTokenPurpose.UNSUBSCRIBE: {"test-unsubscribe-1": UNSUBSCRIBE_KEY},
        },
        active_key_ids={
            SubscriptionTokenPurpose.CONFIRM: "test-confirm-retired",
            SubscriptionTokenPurpose.UNSUBSCRIBE: "test-unsubscribe-1",
        },
        random_bytes=lambda size: b"o" * size,
    )
    old_token = old_codec.issue(SubscriptionTokenPurpose.CONFIRM)
    current_codec = SubscriptionTokenCodec(
        environment=SubscriptionTokenEnvironment.TEST,
        keys={
            SubscriptionTokenPurpose.CONFIRM: {
                "test-confirm-1": CONFIRM_KEY,
                "test-confirm-retired": retired_key,
            },
            SubscriptionTokenPurpose.UNSUBSCRIBE: {"test-unsubscribe-1": UNSUBSCRIBE_KEY},
        },
        active_key_ids={
            SubscriptionTokenPurpose.CONFIRM: "test-confirm-1",
            SubscriptionTokenPurpose.UNSUBSCRIBE: "test-unsubscribe-1",
        },
    )

    assert current_codec.issue(SubscriptionTokenPurpose.CONFIRM).key_id == "test-confirm-1"
    assert (
        current_codec.verify(
            old_token.token,
            expected_purpose=SubscriptionTokenPurpose.CONFIRM,
        ).key_id
        == "test-confirm-retired"
    )


def test_removed_retired_key_fails_closed() -> None:
    retired_key = b"r" * 32
    old_codec = SubscriptionTokenCodec(
        environment=SubscriptionTokenEnvironment.TEST,
        keys={
            SubscriptionTokenPurpose.CONFIRM: {"test-confirm-retired": retired_key},
            SubscriptionTokenPurpose.UNSUBSCRIBE: {"test-unsubscribe-1": UNSUBSCRIBE_KEY},
        },
        active_key_ids={
            SubscriptionTokenPurpose.CONFIRM: "test-confirm-retired",
            SubscriptionTokenPurpose.UNSUBSCRIBE: "test-unsubscribe-1",
        },
    )
    issued = old_codec.issue(SubscriptionTokenPurpose.CONFIRM)

    with pytest.raises(InvalidSubscriptionTokenError):
        _codec().verify(issued.token, expected_purpose=SubscriptionTokenPurpose.CONFIRM)


def test_key_ids_cannot_be_reused_across_purposes() -> None:
    with pytest.raises(ValueError, match="unique across purposes"):
        SubscriptionTokenCodec(
            environment=SubscriptionTokenEnvironment.TEST,
            keys={
                SubscriptionTokenPurpose.CONFIRM: {"test-shared-1": CONFIRM_KEY},
                SubscriptionTokenPurpose.UNSUBSCRIBE: {"test-shared-1": UNSUBSCRIBE_KEY},
            },
            active_key_ids={
                SubscriptionTokenPurpose.CONFIRM: "test-shared-1",
                SubscriptionTokenPurpose.UNSUBSCRIBE: "test-shared-1",
            },
        )


def test_key_id_must_match_environment_namespace() -> None:
    with pytest.raises(ValueError, match="does not match the environment"):
        SubscriptionTokenCodec(
            environment=SubscriptionTokenEnvironment.PRODUCTION,
            keys={
                SubscriptionTokenPurpose.CONFIRM: {"test-confirm-1": CONFIRM_KEY},
                SubscriptionTokenPurpose.UNSUBSCRIBE: {"test-unsubscribe-1": UNSUBSCRIBE_KEY},
            },
            active_key_ids={
                SubscriptionTokenPurpose.CONFIRM: "test-confirm-1",
                SubscriptionTokenPurpose.UNSUBSCRIBE: "test-unsubscribe-1",
            },
        )
