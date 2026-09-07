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


def _item(*author_elements: str) -> bytes:
    """One `<item>` wrapped in a minimal feed that declares the `dc:` and
    a decoy `x:` namespace, so author-ordering tests only vary the
    author-bearing elements."""
    inner = "".join(author_elements)
    return (
        "<?xml version='1.0'?>"
        "<rss version='2.0' xmlns:dc='http://purl.org/dc/elements/1.1/'"
        " xmlns:x='http://example.invalid/ns'>"
        "<channel><title>t</title><item>"
        "<title>Entry</title><link>https://openai.com/index/x</link>"
        f"{inner}"
        "</item></channel></rss>"
    ).encode()


def test_author_then_dc_creator_keeps_document_order() -> None:
    feed = parse_rss(_item("<author>Ada Lovelace</author>", "<dc:creator>Alan Turing</dc:creator>"))

    assert feed.entries[0].authors == ("Ada Lovelace", "Alan Turing")


def test_dc_creator_before_author_keeps_document_order() -> None:
    feed = parse_rss(_item("<dc:creator>Alan Turing</dc:creator>", "<author>Ada Lovelace</author>"))

    assert feed.entries[0].authors == ("Alan Turing", "Ada Lovelace")


def test_interleaved_author_and_creator_keep_document_order() -> None:
    feed = parse_rss(
        _item(
            "<author>Ada Lovelace</author>",
            "<dc:creator>Alan Turing</dc:creator>",
            "<author>Grace Hopper</author>",
        )
    )

    assert feed.entries[0].authors == ("Ada Lovelace", "Alan Turing", "Grace Hopper")


def test_empty_author_elements_are_ignored() -> None:
    feed = parse_rss(
        _item(
            "<author></author>",
            "<dc:creator>   </dc:creator>",
            "<author>Grace Hopper</author>",
        )
    )

    assert feed.entries[0].authors == ("Grace Hopper",)


def test_duplicate_and_mixed_case_authors_are_preserved_at_the_parser_level() -> None:
    feed = parse_rss(
        _item(
            "<author>Ada Lovelace</author>",
            "<dc:creator>ada lovelace</dc:creator>",
            "<author>Ada Lovelace</author>",
        )
    )

    # The parser does not dedupe or case-fold -- that is `normalize.py`'s job.
    assert feed.entries[0].authors == ("Ada Lovelace", "ada lovelace", "Ada Lovelace")


def test_creator_in_an_unrelated_namespace_is_not_treated_as_an_author() -> None:
    feed = parse_rss(_item("<x:creator>Not An Author</x:creator>", "<author>Ada Lovelace</author>"))

    assert feed.entries[0].authors == ("Ada Lovelace",)


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


def test_a_bare_doctype_with_no_entities_is_refused() -> None:
    """`forbid_dtd=True`: a `<!DOCTYPE>` declaration is rejected outright,
    even one that declares no entity and references none. The feed is
    otherwise valid RSS 2.0."""
    with pytest.raises(RssParseError):
        parse_rss(load_fixture("openai_news_plain_dtd.xml"))


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
