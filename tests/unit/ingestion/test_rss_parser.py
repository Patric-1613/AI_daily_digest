"""Safe RSS parsing (`ingestion/rss/parser.py`). Every case parses a
saved fixture from `tests/fixtures/rss/`, never a live feed."""

from __future__ import annotations

import pytest

from ai_daily_digest.ingestion.rss.parser import RssParseError, parse_rss
from tests.unit.ingestion.rss_helpers import load_fixture


def test_parses_a_well_formed_feed() -> None:
    feed = parse_rss(load_fixture("openai_news_sample.xml"))

    assert feed.channel_title == "OpenAI News"
    assert len(feed.entries) == 4
    first = feed.entries[0]
    assert first.raw_index == 0
    assert first.title == "GPT-4o context window increased to 256k tokens"
    assert first.link is not None
    assert first.link.startswith("https://openai.com/index/gpt-4o-256k-context/")
    assert first.guid == "https://openai.com/index/gpt-4o-256k-context"
    assert first.pub_date_raw == "Thu, 03 Sep 2026 13:15:00 GMT"


def test_cdata_and_entity_escapes_are_resolved() -> None:
    feed = parse_rss(load_fixture("openai_news_sample.xml"))

    entry = feed.entries[0]
    assert entry.link is not None
    assert entry.description is not None
    # `&amp;` in the link text is resolved to a literal `&`.
    assert "&utm_medium=feed" in entry.link
    # CDATA body is returned as plain text.
    assert entry.description.startswith("GPT-4o now supports")


def test_multiple_categories_are_collected_in_order() -> None:
    feed = parse_rss(load_fixture("openai_news_sample.xml"))

    assert feed.entries[0].categories == ("Product", "API")
    assert feed.entries[1].categories == ("Product",)


def test_openai_sample_entries_carry_no_authors() -> None:
    feed = parse_rss(load_fixture("openai_news_sample.xml"))

    assert all(entry.authors == () for entry in feed.entries)


def test_author_and_dc_creator_elements_are_collected_in_document_order() -> None:
    body = (
        b"<?xml version='1.0'?>"
        b"<rss version='2.0' xmlns:dc='http://purl.org/dc/elements/1.1/'>"
        b"<channel><title>t</title><item>"
        b"<title>Entry</title>"
        b"<link>https://openai.com/index/x</link>"
        b"<author>Ada Lovelace</author>"
        b"<dc:creator>Alan Turing</dc:creator>"
        b"</item></channel></rss>"
    )

    feed = parse_rss(body)

    assert feed.entries[0].authors == ("Ada Lovelace", "Alan Turing")


def test_entries_keep_document_order_indices() -> None:
    feed = parse_rss(load_fixture("openai_news_sample.xml"))

    assert [entry.raw_index for entry in feed.entries] == [0, 1, 2, 3]
    assert feed.entries[3].title == "Whisper large-v3-turbo released"


def test_missing_item_fields_become_none_not_errors() -> None:
    feed = parse_rss(load_fixture("openai_news_malformed_entry.xml"))

    # The entry with an empty <link></link> parses; the empty element
    # yields None rather than raising -- normalization decides usability.
    no_link = feed.entries[1]
    assert no_link.link is None
    assert no_link.title == "Entry with no link"


def test_malformed_xml_raises_a_source_level_error() -> None:
    with pytest.raises(RssParseError):
        parse_rss(load_fixture("openai_news_malformed_xml.xml"))


def test_declared_entity_is_refused() -> None:
    """defusedxml must reject a feed that declares and references an XML
    entity -- entity-expansion is a denial-of-service vector on untrusted
    content (AGENTS.md)."""
    with pytest.raises(RssParseError):
        parse_rss(load_fixture("openai_news_with_entity.xml"))


def test_non_rss_root_is_rejected() -> None:
    with pytest.raises(RssParseError, match="expected <rss>"):
        parse_rss(b"<?xml version='1.0'?><feed><entry/></feed>")


def test_feed_without_channel_is_rejected() -> None:
    with pytest.raises(RssParseError, match="no <channel>"):
        parse_rss(b"<?xml version='1.0'?><rss version='2.0'></rss>")


def test_content_encoded_is_preferred_over_description() -> None:
    body = (
        b"<?xml version='1.0'?>"
        b"<rss version='2.0' xmlns:content='http://purl.org/rss/1.0/modules/content/'>"
        b"<channel><title>t</title><item>"
        b"<title>Entry</title>"
        b"<link>https://openai.com/index/x</link>"
        b"<description>short summary</description>"
        b"<content:encoded>the full rich body</content:encoded>"
        b"</item></channel></rss>"
    )

    feed = parse_rss(body)

    assert feed.entries[0].description == "the full rich body"
