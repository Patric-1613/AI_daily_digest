"""Platform-aware client-network resolution, separated from subscription business logic.

Render does not publish a stable reverse-proxy IP/CIDR for its web-service load balancer, and
automatically injects ``FORWARDED_ALLOW_IPS=*`` into every Python service's environment. That
makes Uvicorn's ``ProxyHeadersMiddleware`` (which requires an explicit, bounded trusted-proxy
allowlist to rewrite ``request.client``) unusable as the security boundary for the real client
address on Render -- there is no operator-supplied CIDR that could ever satisfy it.

Render's own proxy guidance instead identifies ``CF-Connecting-IP`` -- set by Cloudflare's edge,
which fronts every Render service, and always overwritten by Cloudflare regardless of what a
client sends -- as authoritative for the real client address. This module resolves the network
identity used for subscription rate limiting from that header when running on a verified Render
public web-service runtime, and never trusts ``X-Forwarded-For`` for this purpose (Render's own
documentation notes its leftmost value can be caller-controlled). Outside Render, it preserves the
existing direct-peer (``request.client``) behavior unchanged.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping

from fastapi import Request

_RENDER_TRUE_VALUES = frozenset({"true", "1"})
_CF_CONNECTING_IP_HEADER = "cf-connecting-ip"
_DIRECT_PEER_FALLBACK = "127.0.0.1"


class ClientNetworkResolutionError(RuntimeError):
    """The real client network identity could not be safely established.

    Carries no request data in its message -- callers must never log or return this exception's
    details, only translate it to the standard, generic error envelope.
    """


def _is_render_public_web_service(env: Mapping[str, str]) -> bool:
    """True only on a verified Render public web-service runtime.

    Both signals are Render-managed platform facts, not operator-supplied configuration: Render
    sets ``RENDER=true`` for every service it runs, and ``RENDER_SERVICE_TYPE=web`` specifically
    for a public web service (as opposed to a background worker or cron job, which are not
    fronted by the same public load balancer/Cloudflare path).
    """
    return (
        env.get("RENDER", "").strip().lower() in _RENDER_TRUE_VALUES
        and env.get("RENDER_SERVICE_TYPE", "").strip().lower() == "web"
    )


def _render_client_address(request: Request) -> str:
    """Resolve the real client address from Cloudflare's ``CF-Connecting-IP``.

    Fails closed on anything other than exactly one syntactically valid IPv4 or IPv6 address:
    missing, empty, malformed, comma-joined, or repeated-header values are all rejected rather
    than guessed at. ``X-Forwarded-For`` is never consulted here.
    """
    values = request.headers.getlist(_CF_CONNECTING_IP_HEADER)
    if len(values) != 1:
        raise ClientNetworkResolutionError()
    candidate = values[0].strip()
    if not candidate or "," in candidate:
        raise ClientNetworkResolutionError()
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        raise ClientNetworkResolutionError() from None
    return str(address)


def _direct_peer_address(request: Request) -> str:
    """The existing, unchanged behavior for local development, tests, and non-Render platforms."""
    return request.client.host if request.client is not None else _DIRECT_PEER_FALLBACK


def resolve_client_network_identity(
    request: Request,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """Resolve the address used to derive the subscription rate-limit network identity.

    On a verified Render public web-service runtime, this is Cloudflare's ``CF-Connecting-IP``.
    Everywhere else -- local development, tests, and any other supported deployment target -- it
    is the existing direct ASGI connection peer, exactly as before this module existed.
    """
    values = env if env is not None else os.environ
    if _is_render_public_web_service(values):
        return _render_client_address(request)
    return _direct_peer_address(request)
