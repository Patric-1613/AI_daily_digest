"""The RSS HTTP transport: `HttpxFetcher` (SSRF hardening, redirect
validation, size cap, status + Content-Type handling, validator capture)
and `fetch_with_retry` (bounded retry, no permanent-error retry). No test
opens a socket -- `HttpxFetcher` runs against an `httpx.MockTransport`,
`fetch_with_retry` against a scripted `FakeFetcher`
(`docs/ENGINEERING_STANDARDS.md`)."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from ai_daily_digest.ingestion.rss.transport import (
    HttpResponse,
    HttpxFetcher,
    PermanentTransportError,
    ResponseTooLargeError,
    RetryOutcome,
    TransientTransportError,
    fetch_with_retry,
)
from tests.unit.ingestion.rss_helpers import (
    TEST_ALLOWED_HOSTS,
    FakeFetcher,
    RecordingSleep,
    build_policy,
)

_URL = "https://openai.com/news/rss.xml"
_XML = {"content-type": "application/rss+xml"}


def _mock_fetcher(
    handler: Callable[[httpx.Request], httpx.Response],
) -> HttpxFetcher:
    return HttpxFetcher(transport=httpx.MockTransport(handler))


async def _fetch(
    fetcher: HttpxFetcher,
    url: str = _URL,
    *,
    allowed_hosts: frozenset[str] = TEST_ALLOWED_HOSTS,
) -> HttpResponse:
    return await fetcher.fetch(
        url,
        headers={"User-Agent": "ai-daily-digest-test/0.0"},
        timeout_seconds=5.0,
        max_response_bytes=1_000,
        allowed_hosts=allowed_hosts,
    )


# -- terminal-response handling ----------------------------------------


@pytest.mark.asyncio
async def test_successful_fetch_captures_etag_and_last_modified() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<rss></rss>",
            headers={
                **_XML,
                "ETag": '"abc123"',
                "Last-Modified": "Wed, 03 Sep 2026 13:15:00 GMT",
            },
        )

    response = await _fetch(_mock_fetcher(handler))

    assert response.status_code == 200
    assert response.body == b"<rss></rss>"
    assert response.etag == '"abc123"'
    assert response.last_modified == "Wed, 03 Sep 2026 13:15:00 GMT"


@pytest.mark.asyncio
async def test_missing_validators_are_none() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<rss></rss>", headers=_XML)

    response = await _fetch(_mock_fetcher(handler))

    assert response.etag is None
    assert response.last_modified is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type",
    ["application/rss+xml", "application/atom+xml", "application/xml", "text/xml; charset=utf-8"],
)
async def test_approved_content_types_are_accepted(content_type: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<rss></rss>", headers={"content-type": content_type})

    response = await _fetch(_mock_fetcher(handler))
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_configured_html_media_type_is_accepted_for_article_fetcher() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html></html>", headers={"content-type": "text/html"})

    fetcher = HttpxFetcher(
        transport=httpx.MockTransport(handler),
        approved_media_types=frozenset({"text/html"}),
    )

    response = await _fetch(fetcher)

    assert response.body == b"<html></html>"


@pytest.mark.asyncio
@pytest.mark.parametrize("content_type", ["text/html; charset=utf-8", "application/json", ""])
async def test_unapproved_content_type_is_permanent(content_type: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        headers = {"content-type": content_type} if content_type else {}
        return httpx.Response(200, content=b"<html></html>", headers=headers)

    with pytest.raises(PermanentTransportError, match="Content-Type"):
        await _fetch(_mock_fetcher(handler))


@pytest.mark.asyncio
async def test_oversized_response_is_rejected_while_reading() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 5_000, headers=_XML)

    with pytest.raises(ResponseTooLargeError):
        await _fetch(_mock_fetcher(handler))


@pytest.mark.asyncio
async def test_client_error_status_is_permanent() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"not found")

    with pytest.raises(PermanentTransportError):
        await _fetch(_mock_fetcher(handler))


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_retryable_status_is_transient(status: int) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=b"")

    with pytest.raises(TransientTransportError):
        await _fetch(_mock_fetcher(handler))


@pytest.mark.asyncio
async def test_timeout_is_translated_to_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    with pytest.raises(TransientTransportError):
        await _fetch(_mock_fetcher(handler))


@pytest.mark.asyncio
async def test_connection_error_is_translated_to_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(TransientTransportError, match="transport error"):
        await _fetch(_mock_fetcher(handler))


@pytest.mark.asyncio
async def test_request_carries_the_configured_user_agent() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers.get("user-agent", "")
        return httpx.Response(200, content=b"<rss></rss>", headers=_XML)

    await _fetch(_mock_fetcher(handler))

    assert seen["ua"] == "ai-daily-digest-test/0.0"


# -- SSRF / URL + redirect hardening ----------------------------------


@pytest.mark.asyncio
async def test_userinfo_in_the_url_is_rejected_before_any_request() -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, content=b"<rss></rss>", headers=_XML)

    with pytest.raises(PermanentTransportError, match="user-info"):
        await _fetch(_mock_fetcher(handler), "https://user:pw@openai.com/news/rss.xml")
    assert called is False


@pytest.mark.asyncio
async def test_host_off_the_allowlist_is_rejected() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<rss></rss>", headers=_XML)

    with pytest.raises(PermanentTransportError, match="allowlist"):
        await _fetch(_mock_fetcher(handler), "https://evil.example/news/rss.xml")


@pytest.mark.asyncio
async def test_non_https_url_is_rejected() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<rss></rss>", headers=_XML)

    with pytest.raises(PermanentTransportError, match="https"):
        await _fetch(_mock_fetcher(handler), "http://openai.com/news/rss.xml")


@pytest.mark.asyncio
async def test_allowlisted_same_domain_redirect_is_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/news/rss.xml":
            return httpx.Response(301, headers={"location": "https://www.openai.com/feed.xml"})
        return httpx.Response(200, content=b"<rss>ok</rss>", headers=_XML)

    response = await _fetch(_mock_fetcher(handler))
    assert response.body == b"<rss>ok</rss>"


@pytest.mark.asyncio
async def test_cross_domain_redirect_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "openai.com" in request.url.host:
            return httpx.Response(302, headers={"location": "https://evil.example/x"})
        return httpx.Response(200, content=b"<rss></rss>", headers=_XML)

    with pytest.raises(PermanentTransportError):
        await _fetch(
            _mock_fetcher(handler),
            allowed_hosts=TEST_ALLOWED_HOSTS | {"evil.example"},
        )


@pytest.mark.asyncio
async def test_https_to_http_downgrade_redirect_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.scheme == "https":
            return httpx.Response(302, headers={"location": "http://openai.com/news/rss.xml"})
        return httpx.Response(200, content=b"<rss></rss>", headers=_XML)

    with pytest.raises(PermanentTransportError):
        await _fetch(_mock_fetcher(handler))


@pytest.mark.asyncio
async def test_redirect_without_a_location_header_is_permanent() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302)

    with pytest.raises(PermanentTransportError, match="Location"):
        await _fetch(_mock_fetcher(handler))


@pytest.mark.asyncio
async def test_redirect_loop_is_bounded() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://openai.com/loop"})

    with pytest.raises(PermanentTransportError, match="redirects"):
        await _fetch(_mock_fetcher(handler))


# -- fetch_with_retry ------------------------------------------------


async def _retry(
    fetcher: FakeFetcher,
    *,
    policy_max: int,
    sleep: RecordingSleep | None = None,
    outcome: RetryOutcome | None = None,
    extra_headers: dict[str, str] | None = None,
) -> HttpResponse:
    return await fetch_with_retry(
        fetcher,
        _URL,
        policy=build_policy(max_attempts=policy_max),
        allowed_hosts=TEST_ALLOWED_HOSTS,
        extra_headers=extra_headers,
        sleep=sleep or RecordingSleep(),
        outcome=outcome,
    )


@pytest.mark.asyncio
async def test_extra_headers_are_merged_over_the_defaults() -> None:
    ok = HttpResponse(status_code=200, body=b"<rss></rss>")
    fetcher = FakeFetcher(ok)

    await _retry(fetcher, policy_max=1, extra_headers={"If-None-Match": '"abc"'})

    sent = fetcher.received_headers[0]
    assert sent["If-None-Match"] == '"abc"'
    assert sent["User-Agent"] == "ai-daily-digest-test/0.0"


@pytest.mark.asyncio
async def test_retry_recovers_after_transient_failures() -> None:
    ok = HttpResponse(status_code=200, body=b"<rss></rss>")
    fetcher = FakeFetcher(TransientTransportError("boom"), TransientTransportError("boom"), ok)
    sleep = RecordingSleep()
    outcome = RetryOutcome()

    response = await _retry(fetcher, policy_max=3, sleep=sleep, outcome=outcome)

    assert response is ok
    assert fetcher.call_count == 3
    assert outcome.attempts == 3
    assert len(sleep.delays) == 2
    assert 0.0 <= sleep.delays[0] <= 0.01
    assert 0.0 <= sleep.delays[1] <= 0.02
    assert fetcher.received_allowed_hosts[0] == TEST_ALLOWED_HOSTS


@pytest.mark.asyncio
async def test_retry_gives_up_after_max_attempts() -> None:
    fetcher = FakeFetcher(TransientTransportError("still down"))
    outcome = RetryOutcome()

    with pytest.raises(TransientTransportError):
        await _retry(fetcher, policy_max=3, outcome=outcome)

    assert fetcher.call_count == 3
    assert outcome.attempts == 3
    assert len(outcome.transient_errors) == 3


@pytest.mark.asyncio
async def test_permanent_failure_is_not_retried() -> None:
    fetcher = FakeFetcher(PermanentTransportError("HTTP 404"))
    sleep = RecordingSleep()

    with pytest.raises(PermanentTransportError):
        await _retry(fetcher, policy_max=5, sleep=sleep)

    assert fetcher.call_count == 1
    assert sleep.delays == []


@pytest.mark.asyncio
async def test_oversized_response_is_not_retried() -> None:
    fetcher = FakeFetcher(ResponseTooLargeError("too big"))

    with pytest.raises(ResponseTooLargeError):
        await _retry(fetcher, policy_max=5)

    assert fetcher.call_count == 1
