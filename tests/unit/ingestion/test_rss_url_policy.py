"""URL-safety policy (`ingestion/rss/url_policy.py`): `sanitize_url` for
reports/logs, `require_safe_url` for the initial fetch, and
`require_safe_redirect` for every redirect hop."""

from __future__ import annotations

import pytest

from ai_daily_digest.ingestion.rss.url_policy import (
    UnsafeUrlError,
    require_safe_redirect,
    require_safe_url,
    sanitize_url,
)

_ALLOW = frozenset({"openai.com", "www.openai.com"})


# -- sanitize_url ----------------------------------------------------


def test_sanitize_strips_userinfo_query_and_fragment() -> None:
    dirty = "https://alice:s3cr3t@openai.com/index/x?api_key=leak&page=2#section"
    assert sanitize_url(dirty) == "https://openai.com/index/x"


def test_sanitize_keeps_scheme_host_port_and_path() -> None:
    assert sanitize_url("https://openai.com:8443/a/b") == "https://openai.com:8443/a/b"


def test_sanitize_never_raises_on_garbage() -> None:
    assert sanitize_url("https://openai.com:notaport/x") == "<unparsable-url>"
    assert sanitize_url("::::") == "::::"  # no scheme, no host -> nothing to redact


def test_sanitize_leaves_a_urn_style_guid_intact() -> None:
    assert sanitize_url("urn:openai:some-entry") == "urn:openai:some-entry"


# -- require_safe_url ----------------------------------------------


def test_require_safe_url_accepts_an_allowlisted_https_url() -> None:
    url = "https://openai.com/news/rss.xml"
    assert require_safe_url(url, allowed_hosts=_ALLOW) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://openai.com/news/rss.xml",  # not https
        "https://alice:pw@openai.com/rss",  # user-info
        "https://evil.example/rss",  # host off the allowlist
        "https://openai.com:notaport/rss",  # invalid port
        "https:///rss",  # no host
    ],
)
def test_require_safe_url_rejects_unsafe_values(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        require_safe_url(url, allowed_hosts=_ALLOW)


def test_require_safe_url_error_does_not_echo_the_credential() -> None:
    with pytest.raises(UnsafeUrlError) as excinfo:
        require_safe_url("https://alice:s3cr3t@openai.com/rss", allowed_hosts=_ALLOW)
    assert "s3cr3t" not in str(excinfo.value)


# -- require_safe_redirect --------------------------------------


def test_require_safe_redirect_allows_same_registrable_domain() -> None:
    target = require_safe_redirect(
        from_url="https://openai.com/news/rss.xml",
        to_url="https://www.openai.com/feed.xml",
        allowed_hosts=_ALLOW,
    )
    assert target == "https://www.openai.com/feed.xml"


def test_require_safe_redirect_rejects_cross_domain() -> None:
    with pytest.raises(UnsafeUrlError, match="cross-domain"):
        require_safe_redirect(
            from_url="https://openai.com/news/rss.xml",
            to_url="https://evil.example/x",
            allowed_hosts=_ALLOW | {"evil.example"},
        )


def test_require_safe_redirect_rejects_scheme_downgrade() -> None:
    with pytest.raises(UnsafeUrlError, match="downgrade"):
        require_safe_redirect(
            from_url="https://openai.com/news/rss.xml",
            to_url="http://openai.com/news/rss.xml",
            allowed_hosts=_ALLOW,
        )


def test_require_safe_redirect_still_enforces_the_allowlist() -> None:
    with pytest.raises(UnsafeUrlError, match="allowlist"):
        require_safe_redirect(
            from_url="https://openai.com/news/rss.xml",
            to_url="https://api.openai.com/internal",
            allowed_hosts=_ALLOW,
        )
