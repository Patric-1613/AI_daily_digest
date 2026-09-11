"""Asynchronous HTTP transport for the RSS collector.

The collector depends only on the narrow `HttpFetcher` protocol below --
never on `httpx` directly -- so a test injects a canned-response fake and
never touches the network (`docs/ENGINEERING_STANDARDS.md`: "Never make
normal unit tests depend on live ... services"; "use saved fixtures
instead of live sites").

Transport rules (`docs/ARCHITECTURE.md` "Security model" /
"Reliability"): explicit per-request timeout, a hard response-size cap
enforced *while reading* (not after), a clear User-Agent, bounded
retries for transient faults only, and no permanent-error retry. No
response body, secret, or `Authorization` header is ever logged.

SSRF hardening (`url_policy.py`): the initial URL and every redirect hop
are validated against the source's explicit host allowlist *before* the
request is made -- redirects are followed manually, one hop at a time,
never by httpx's automatic follower. A redirect that changes registrable
domain or downgrades the scheme is refused. The terminal response's
`Content-Type` must be an approved RSS/XML media type.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from ai_daily_digest.ingestion.rss.url_policy import (
    UnsafeUrlError,
    require_safe_redirect,
    require_safe_url,
    sanitize_url,
)
from ai_daily_digest.ingestion.sources import CollectionPolicy

LOGGER = logging.getLogger(__name__)

# Status codes worth retrying: a transient upstream problem or an explicit
# "slow down". Every other non-2xx is permanent for this run.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# The default Content-Type media types an RSS/Atom fetch may legitimately
# carry. Other guarded ingestion adapters can provide their own reviewed set.
RSS_MEDIA_TYPES = frozenset(
    {
        "application/rss+xml",
        "application/atom+xml",
        "application/rdf+xml",
        "application/xml",
        "text/xml",
    }
)

# A hard ceiling on redirect hops -- each one is validated, but a loop or
# a long chain is refused rather than followed indefinitely.
_MAX_REDIRECTS = 5

# Jitter source. `SystemRandom` (os.urandom-backed) rather than the
# default Mersenne Twister -- not because retry jitter needs
# cryptographic strength, but because it sidesteps a static-analysis
# false positive without a suppression comment; the cost is negligible
# for one draw per retry.
_JITTER = random.SystemRandom()


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """What a successful fetch returns. `body` is the fully-read response
    bytes (already proven within the size cap). `etag` / `last_modified`
    are captured verbatim when the server sent them, `None` otherwise --
    they are conditional-request validators, never secrets."""

    status_code: int
    body: bytes
    etag: str | None = None
    last_modified: str | None = None


class TransportError(Exception):
    """Base class for every fetch failure."""


class TransientTransportError(TransportError):
    """A fault that may succeed on retry -- a timeout, a connection
    error, or a retryable status (``429``/``5xx``)."""


class PermanentTransportError(TransportError):
    """A fault retrying will not help -- a non-retryable non-2xx status
    (most ``4xx``), or a malformed response."""


class ResponseTooLargeError(PermanentTransportError):
    """The response body exceeded ``max_response_bytes`` while being
    read. Raised before the whole body is buffered."""


class HttpFetcher(Protocol):
    """The one operation the collector needs from an HTTP client. The
    production implementation is `HttpxFetcher`; tests pass a fake that
    satisfies this protocol structurally, the same pattern
    `shared/snapshot_resolver.py` and `ingestion/persistence.py` use."""

    async def fetch(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int,
        allowed_hosts: frozenset[str],
    ) -> HttpResponse:
        """GET `url`, following only redirects whose target passes
        `url_policy.require_safe_redirect` for `allowed_hosts`. Raise
        `TransientTransportError` for a timeout/connection error/retryable
        status, `PermanentTransportError` (or `ResponseTooLargeError`) for
        anything else non-2xx, a disallowed host/redirect, an unapproved
        `Content-Type`, or a body past `max_response_bytes`. Return an
        `HttpResponse` on an approved 2xx."""


class HttpxFetcher:
    """`HttpFetcher` backed by `httpx.AsyncClient`.

    Responses are streamed and bounded. RSS/XML media types are accepted by
    default; another guarded adapter must explicitly provide its reviewed
    media-type set. A custom `httpx.AsyncBaseTransport` can be injected for
    offline tests.
    """

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        approved_media_types: frozenset[str] = RSS_MEDIA_TYPES,
    ) -> None:
        self._transport = transport
        self._approved_media_types = approved_media_types

    async def fetch(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int,
        allowed_hosts: frozenset[str],
    ) -> HttpResponse:
        self._require_safe(url, allowed_hosts)
        client_kwargs: dict[str, object] = {
            "timeout": httpx.Timeout(timeout_seconds),
            # Redirects are followed manually below so every hop is
            # validated first -- never delegated to httpx.
            "follow_redirects": False,
            "headers": dict(headers),
        }
        if self._transport is not None:
            client_kwargs["transport"] = self._transport
        try:
            async with httpx.AsyncClient(**client_kwargs) as client:  # type: ignore[arg-type]
                return await self._fetch_following_redirects(
                    client, url, allowed_hosts, max_response_bytes
                )
        except httpx.TimeoutException as exc:
            raise TransientTransportError(f"timeout fetching {sanitize_url(url)}") from exc
        except httpx.TransportError as exc:
            # Connection reset, DNS failure, TLS error, etc. -- transient.
            raise TransientTransportError(
                f"transport error fetching {sanitize_url(url)}: {type(exc).__name__}"
            ) from exc

    async def _fetch_following_redirects(
        self,
        client: httpx.AsyncClient,
        url: str,
        allowed_hosts: frozenset[str],
        max_response_bytes: int,
    ) -> HttpResponse:
        current = url
        for _hop in range(_MAX_REDIRECTS + 1):
            async with client.stream("GET", current) as response:
                if response.is_redirect:
                    location = response.headers.get("location", "")
                    if not location:
                        raise PermanentTransportError(
                            f"redirect with no Location header from {sanitize_url(current)}"
                        )
                    target = str(httpx.URL(current).join(location))
                    self._require_safe_redirect(current, target, allowed_hosts)
                    current = target
                    continue
                if response.status_code in _RETRYABLE_STATUS:
                    raise TransientTransportError(
                        f"retryable HTTP {response.status_code} from {sanitize_url(current)}"
                    )
                if response.status_code >= 400:
                    raise PermanentTransportError(
                        f"HTTP {response.status_code} from {sanitize_url(current)}"
                    )
                _require_approved_media_type(response, current, self._approved_media_types)
                body = await _read_capped(response, max_response_bytes)
                return HttpResponse(
                    status_code=response.status_code,
                    body=body,
                    etag=response.headers.get("etag"),
                    last_modified=response.headers.get("last-modified"),
                )
        raise PermanentTransportError(
            f"exceeded {_MAX_REDIRECTS} redirects starting from {sanitize_url(url)}"
        )

    @staticmethod
    def _require_safe(url: str, allowed_hosts: frozenset[str]) -> None:
        try:
            require_safe_url(url, allowed_hosts=allowed_hosts)
        except UnsafeUrlError as exc:
            raise PermanentTransportError(str(exc)) from exc

    @staticmethod
    def _require_safe_redirect(from_url: str, to_url: str, allowed_hosts: frozenset[str]) -> None:
        try:
            require_safe_redirect(from_url=from_url, to_url=to_url, allowed_hosts=allowed_hosts)
        except UnsafeUrlError as exc:
            raise PermanentTransportError(str(exc)) from exc


def _require_approved_media_type(
    response: httpx.Response, url: str, approved_media_types: frozenset[str]
) -> None:
    raw = response.headers.get("content-type", "")
    media_type = raw.split(";", 1)[0].strip().lower()
    if media_type not in approved_media_types:
        raise PermanentTransportError(
            f"unapproved Content-Type {media_type or '(none)'!r} from {sanitize_url(url)}"
        )


async def _read_capped(response: httpx.Response, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise ResponseTooLargeError(f"response exceeded {max_bytes} bytes while reading")
        chunks.append(chunk)
    return b"".join(chunks)


@dataclass(slots=True)
class RetryOutcome:
    """Bookkeeping the collector's report needs: how many attempts a
    fetch actually took and what transient errors were seen, so an
    operator can spot a source that is flaky but eventually succeeding."""

    attempts: int = 0
    transient_errors: list[str] = field(default_factory=list)


async def fetch_with_retry(  # pylint: disable=too-many-arguments
    fetcher: HttpFetcher,
    url: str,
    *,
    policy: CollectionPolicy,
    allowed_hosts: frozenset[str],
    extra_headers: Mapping[str, str] | None = None,
    accept: str = "application/rss+xml, application/xml",
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    outcome: RetryOutcome | None = None,
) -> HttpResponse:
    """Fetch `url` (host + every redirect hop constrained to
    `allowed_hosts`), retrying only `TransientTransportError` up to
    `policy.max_attempts` with exponential backoff plus full jitter. A
    `PermanentTransportError` (including `ResponseTooLargeError` and a
    disallowed host/redirect) is re-raised immediately -- never retried.
    `sleep` is injectable so tests assert the retry schedule without real
    delay."""
    headers = {"User-Agent": policy.user_agent, "Accept": accept}
    if extra_headers:
        headers.update(extra_headers)
    record = outcome if outcome is not None else RetryOutcome()

    attempt = 0
    while True:
        attempt += 1
        record.attempts = attempt
        try:
            return await fetcher.fetch(
                url,
                headers=headers,
                timeout_seconds=policy.timeout_seconds,
                max_response_bytes=policy.max_response_bytes,
                allowed_hosts=allowed_hosts,
            )
        except TransientTransportError as exc:
            # A PermanentTransportError (including ResponseTooLargeError) is
            # not a TransientTransportError, so it is never caught here --
            # it propagates out of fetch_with_retry on the first attempt,
            # exactly the "never retried" guarantee, with no handler needed.
            record.transient_errors.append(str(exc))
            LOGGER.warning(
                "rss fetch transient failure",
                extra={"attempt": attempt, "max_attempts": policy.max_attempts},
            )
            if attempt >= policy.max_attempts:
                raise
            backoff = policy.backoff_base_seconds * (2 ** (attempt - 1))
            await sleep(_JITTER.uniform(0, backoff))
