"""Offline tests for deterministic HTML article parsing."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_daily_digest.ingestion.article.parser import ArticleParseError, parse_article

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "article"


def _fixture(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


def test_parse_article_prefers_article_prose_and_reads_metadata() -> None:
    article = parse_article(_fixture("anthropic_context_100k.html"))

    assert article.title == "Introducing 100K Context Windows"
    assert article.published_at == datetime(2023, 5, 11, 9, tzinfo=UTC)
    assert article.canonical_url == "https://www.anthropic.com/news/100k-context-windows"
    assert article.authors == ("Anthropic",)
    assert "100,000 token context window" in article.body
    assert "navigation" not in article.body
    assert "footer" not in article.body


def test_parse_article_uses_json_ld_metadata() -> None:
    payload = b"""<html><head><script type="application/ld+json">
    {"headline":"JSON headline","datePublished":"2024-02-15T12:00:00Z",
     "author":{"name":"Example Author"}}
    </script></head><body><main>
    This is a sufficiently long main article body used to prove JSON-LD metadata is accepted.
    </main></body></html>"""

    article = parse_article(payload)

    assert article.title == "JSON headline"
    assert article.published_at == datetime(2024, 2, 15, 12, tzinfo=UTC)
    assert article.authors == ("Example Author",)


def test_parse_article_uses_visible_lead_date_when_metadata_omits_it() -> None:
    payload = b"""<html><head><meta property="og:title" content="Article title"></head>
    <body><header><h1>Article title</h1><div>Nov 21, 2023</div></header><article>
    This is a sufficiently long article body used to prove the visible publication date fallback.
    </article></body></html>"""

    article = parse_article(payload)

    assert article.published_at == datetime(2023, 11, 21, tzinfo=UTC)


def test_parse_article_survives_unclosed_void_elements_in_nav_and_article() -> None:
    """Real-world HTML routinely writes void elements ("<img>", "<br>",
    "<meta>", ...) without a self-closing slash. Python's html.parser
    calls handle_starttag() for one but never a matching handle_endtag()
    unless the markup itself self-closes it, so the depth counters that
    decide "am I inside <nav>/<article>" must never treat a void element
    as opening a level that needs a later close -- otherwise a bare
    "<img>" inside <nav> permanently inflates the ignored-region depth
    and silently discards every real article that follows it in the
    document (previously: ArticleParseError("article has no substantive
    body") even though the article below is perfectly well-formed)."""
    payload = b"""<html><head>
    <meta property="og:title" content="Void Element Test">
    <meta property="article:published_time" content="2023-05-11T09:00:00Z">
    </head><body>
    <nav>Navigation with an unclosed void element <img src="logo.png"> more nav text</nav>
    <article><h1>Void Element Test</h1>
    <p>This is the real article body that must be extracted correctly even
    though the navigation above contains an unclosed void element.</p>
    </article>
    <footer>This footer text must never appear as evidence.</footer>
    </body></html>"""

    article = parse_article(payload)

    assert "real article body" in article.body
    assert "Navigation" not in article.body
    assert "footer text" not in article.body


def test_parse_article_survives_self_closing_void_element_in_article() -> None:
    payload = b"""<html><head>
    <meta property="og:title" content="Self-closing Void Element Test">
    <meta property="article:published_time" content="2023-05-11T09:00:00Z">
    </head><body><article><h1>Self-closing Void Element Test</h1>
    <img src="diagram.png" />
    <p>This substantive paragraph appears after the self-closing image and must
    remain inside the extracted article body rather than being discarded.</p>
    </article><footer>Footer text must not appear.</footer></body></html>"""

    article = parse_article(payload)

    assert "substantive paragraph" in article.body
    assert "Footer text" not in article.body


def test_parse_article_rejects_missing_publication_timestamp() -> None:
    with pytest.raises(ArticleParseError, match="publication timestamp"):
        parse_article(_fixture("no_date.html"))


def test_parse_article_rejects_empty_document() -> None:
    with pytest.raises(ArticleParseError, match="title"):
        parse_article(b"")
