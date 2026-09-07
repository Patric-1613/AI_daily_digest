"""URL-safety policy shared by the RSS transport and normalizer.

Two concerns, kept in one place so the transport (initial request and
every redirect hop) and the normalizer (each feed entry link) enforce the
same rules:

- **What is safe to fetch** -- `require_safe_url` / `require_safe_redirect`:
  HTTPS only, no `user:password@` user-info, a parseable host and port,
  and a host on the source's explicit allowlist. A redirect must also
  stay on the same registrable domain and must never downgrade the
  scheme.
- **What is safe to record** -- `sanitize_url`: a rendering of a URL with
  user-info, query string, and fragment stripped, because any of the
  three can carry a credential or token. Used for every URL that reaches
  a log line or a `CollectionReport` failure (AGENTS.md: "Never place
  ... credentials ... in logs").

This module performs no I/O.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

_ALLOWED_SCHEMES = frozenset({"https"})
_UNPARSABLE = "<unparsable-url>"


class UnsafeUrlError(Exception):
    """A URL (a configured source URL, a feed entry link, or a redirect
    target) violates the collector's fetch-safety policy. The transport
    translates this into a non-retryable `PermanentTransportError`; the
    normalizer into an `EntryNormalizationError`."""


def _try_split(raw: str) -> tuple[str, str | None, int | None, str, str] | None:
    """`(scheme, host, port, path, userinfo)` for `raw`, or `None` if it
    cannot be parsed as a URL at all. Accessing `.port` here surfaces an
    invalid-port `ValueError` at parse time rather than at some later
    attribute access."""
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return None
    userinfo = ""
    if parts.username:
        userinfo += parts.username
    if parts.password:
        userinfo += f":{parts.password}"
    return parts.scheme.lower(), (parts.hostname or None), port, parts.path, userinfo


def sanitize_url(raw: str) -> str:
    """`raw` with user-info, query, and fragment removed -- scheme, host,
    optional port, and path only. Never raises: an unparseable value
    becomes the literal ``"<unparsable-url>"`` so a caller can drop it
    straight into an error string. A value with no scheme and no host
    (e.g. a URN-style guid) is returned unchanged -- there is nothing
    structured to redact."""
    split = _try_split(raw)
    if split is None:
        return _UNPARSABLE
    scheme, host, port, path, _userinfo = split
    if not scheme and not host:
        return raw
    netloc = host or ""
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((scheme, netloc, path, "", ""))


def _registrable_domain(host: str) -> str:
    """The last two labels of `host` -- a deliberately simple
    approximation (`www.openai.com` and `openai.com` -> `openai.com`).
    It is only ever used to *tighten* an already-enforced explicit host
    allowlist, so a multi-part public suffix like `co.uk` collapsing to
    `co.uk` here can only reject an otherwise-allowed redirect, never
    admit a disallowed one."""
    labels = host.split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def require_safe_url(raw: str, *, allowed_hosts: frozenset[str]) -> str:
    """Return `raw` unchanged if it is safe to fetch, else raise
    `UnsafeUrlError`. Safe means: parseable, `https`, no user-info, a
    real host, a valid port, and a host in `allowed_hosts` (compared
    case-insensitively)."""
    split = _try_split(raw)
    if split is None:
        raise UnsafeUrlError(f"unparseable URL: {sanitize_url(raw)}")
    scheme, host, _port, _path, userinfo = split
    if scheme not in _ALLOWED_SCHEMES:
        raise UnsafeUrlError(f"URL scheme must be https, got {scheme!r}: {sanitize_url(raw)}")
    if userinfo:
        raise UnsafeUrlError(f"URL must not contain user-info: {sanitize_url(raw)}")
    if not host:
        raise UnsafeUrlError(f"URL has no host: {sanitize_url(raw)}")
    if host.lower() not in {allowed.lower() for allowed in allowed_hosts}:
        raise UnsafeUrlError(f"host {host!r} is not on the source allowlist: {sanitize_url(raw)}")
    return raw


def require_safe_redirect(*, from_url: str, to_url: str, allowed_hosts: frozenset[str]) -> str:
    """Validate a redirect target: everything `require_safe_url` checks,
    plus no HTTPS->HTTP downgrade and no move to a different registrable
    domain than `from_url`."""
    from_split = _try_split(from_url)
    to_split = _try_split(to_url)
    if to_split is None:
        raise UnsafeUrlError(f"redirect to an unparseable URL: {sanitize_url(to_url)}")
    to_scheme, to_host, _to_port, _to_path, _to_userinfo = to_split

    if (
        from_split is not None
        and from_split[0] in _ALLOWED_SCHEMES
        and to_scheme not in _ALLOWED_SCHEMES
    ):
        raise UnsafeUrlError(
            f"redirect downgrades scheme to {to_scheme!r}: "
            f"{sanitize_url(from_url)} -> {sanitize_url(to_url)}"
        )

    require_safe_url(to_url, allowed_hosts=allowed_hosts)

    from_host = from_split[1] if from_split is not None else None
    if (
        from_host
        and to_host
        and _registrable_domain(to_host.lower()) != _registrable_domain(from_host.lower())
    ):
        raise UnsafeUrlError(
            f"cross-domain redirect: {sanitize_url(from_url)} -> {sanitize_url(to_url)}"
        )
    return to_url
