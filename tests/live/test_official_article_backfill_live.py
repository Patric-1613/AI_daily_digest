"""Opt-in smoke test for the two first-party Anthropic rehearsal pages."""

from __future__ import annotations

import pytest

from ai_daily_digest.ingestion.article.collector import ARTICLE_ACCEPT, HTML_MEDIA_TYPES
from ai_daily_digest.ingestion.article.parser import parse_article
from ai_daily_digest.ingestion.rss.transport import HttpxFetcher, fetch_with_retry
from ai_daily_digest.ingestion.sources import load_source_registry

pytestmark = pytest.mark.live

_CASES = (
    ("https://www.anthropic.com/news/100k-context-windows", "100,000"),
    # The live page's own wording reads "200K", not "200,000" -- verified
    # by direct fetch during review. This asserts on the page's actual
    # current phrasing, not a guess.
    ("https://www.anthropic.com/news/claude-2-1", "200K"),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("url", "expected_text"), _CASES)
async def test_anthropic_rehearsal_article_still_parses(url: str, expected_text: str) -> None:
    registry = load_source_registry()
    source = registry.get("anthropic_news")
    response = await fetch_with_retry(
        HttpxFetcher(approved_media_types=HTML_MEDIA_TYPES),
        url,
        policy=registry.collection_policy,
        allowed_hosts=source.host_allowlist(),
        accept=ARTICLE_ACCEPT,
    )

    article = parse_article(response.body)

    assert article.published_at.year == 2023
    assert "claude" in f"{article.title} {article.body}".casefold()
    assert expected_text in article.body
