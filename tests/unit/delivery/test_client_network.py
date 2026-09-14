"""Platform-aware client-network resolution tests -- see ai_daily_digest.delivery.api.client_network."""

from __future__ import annotations

import pytest
from starlette.requests import Request

from ai_daily_digest.delivery.api.client_network import (
    ClientNetworkResolutionError,
    resolve_client_network_identity,
)

_RENDER_WEB_ENV = {"RENDER": "true", "RENDER_SERVICE_TYPE": "web"}


def _make_request(
    *,
    client_host: str | None = "203.0.113.9",
    headers: list[tuple[str, str]] | None = None,
) -> Request:
    raw_headers = [
        (name.lower().encode("ascii"), value.encode("ascii")) for name, value in headers or []
    ]
    scope = {
        "type": "http",
        "client": (client_host, 50000) if client_host is not None else None,
        "headers": raw_headers,
    }
    return Request(scope)


def test_render_runtime_uses_cf_connecting_ip_for_ipv4() -> None:
    request = _make_request(headers=[("cf-connecting-ip", "198.51.100.7")])

    assert resolve_client_network_identity(request, env=_RENDER_WEB_ENV) == "198.51.100.7"


def test_render_runtime_uses_cf_connecting_ip_for_ipv6_and_normalizes_it() -> None:
    request = _make_request(
        headers=[("cf-connecting-ip", "2001:0db8:0000:0000:0000:0000:0000:0001")]
    )

    assert resolve_client_network_identity(request, env=_RENDER_WEB_ENV) == "2001:db8::1"


def test_render_runtime_ignores_spoofed_x_forwarded_for() -> None:
    request = _make_request(
        headers=[
            ("cf-connecting-ip", "198.51.100.7"),
            ("x-forwarded-for", "1.2.3.4, 5.6.7.8"),
        ],
    )

    assert resolve_client_network_identity(request, env=_RENDER_WEB_ENV) == "198.51.100.7"


def test_render_runtime_fails_closed_when_cf_connecting_ip_is_missing() -> None:
    request = _make_request(headers=[("x-forwarded-for", "198.51.100.7")])

    with pytest.raises(ClientNetworkResolutionError):
        resolve_client_network_identity(request, env=_RENDER_WEB_ENV)


@pytest.mark.parametrize(
    "raw_value",
    [
        "",
        "   ",
        "not-an-ip",
        "198.51.100.7, 198.51.100.8",
        "198.51.100.7,198.51.100.8",
        "999.999.999.999",
    ],
)
def test_render_runtime_fails_closed_on_malformed_or_comma_joined_values(raw_value: str) -> None:
    request = _make_request(headers=[("cf-connecting-ip", raw_value)])

    with pytest.raises(ClientNetworkResolutionError):
        resolve_client_network_identity(request, env=_RENDER_WEB_ENV)


def test_render_runtime_fails_closed_on_repeated_header_lines() -> None:
    request = _make_request(
        headers=[
            ("cf-connecting-ip", "198.51.100.7"),
            ("cf-connecting-ip", "198.51.100.8"),
        ],
    )

    with pytest.raises(ClientNetworkResolutionError):
        resolve_client_network_identity(request, env=_RENDER_WEB_ENV)


def test_non_render_platform_uses_the_direct_peer_and_ignores_cf_connecting_ip() -> None:
    request = _make_request(
        client_host="192.0.2.55",
        headers=[("cf-connecting-ip", "198.51.100.7")],
    )

    assert resolve_client_network_identity(request, env={}) == "192.0.2.55"


def test_render_env_without_web_service_type_uses_the_direct_peer() -> None:
    """RENDER=true alone (e.g. the existing cron/worker) must not switch to header-based trust."""
    request = _make_request(
        client_host="192.0.2.55",
        headers=[("cf-connecting-ip", "198.51.100.7")],
    )

    assert resolve_client_network_identity(request, env={"RENDER": "true"}) == "192.0.2.55"


def test_missing_direct_peer_falls_back_to_loopback_outside_render() -> None:
    request = _make_request(client_host=None)

    assert resolve_client_network_identity(request, env={}) == "127.0.0.1"


def test_resolution_error_message_carries_no_request_data() -> None:
    request = _make_request(headers=[("cf-connecting-ip", "not-an-ip")])

    with pytest.raises(ClientNetworkResolutionError) as excinfo:
        resolve_client_network_identity(request, env=_RENDER_WEB_ENV)

    assert "not-an-ip" not in str(excinfo.value)
