"""Deterministic normalization of a parsed `RssEntry` into a
`FetchedDocument` (`ingestion/rss/normalize.py`)."""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime

import pytest

from ai_daily_digest.ingestion.rss.normalize import (
    EntryNormalizationError,
    canonicalize_url,
    content_hash,
    dedupe_key,
    normalize_authors,
    normalize_entry,
    normalize_tags,
    normalized_content,
    parse_published_at,
)
from ai_daily_digest.ingestion.rss.parser import RssEntry
from ai_daily_digest.ingestion.service import FetchedDocument
from ai_daily_digest.ingestion.sources import load_source_registry

_SOURCE = load_source_registry().get("openai_news")
_FIRST_FETCHED = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
_FETCHED = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def _entry(**overrides: object) -> RssEntry:
    base: dict[str, object] = {
        "raw_index": 0,
        "title": "GPT-4o context window increased to 256k tokens",
        "link": "https://openai.com/index/gpt-4o-256k-context",
        "description": "GPT-4o now supports a 256,000 token context window.",
        "guid": "https://openai.com/index/gpt-4o-256k-context",
        "categories": ("Product", "API"),
        "authors": (),
        "pub_date_raw": "Thu, 03 Sep 2026 13:15:00 GMT",
    }
    base.update(overrides)
    return RssEntry(**base)  # type: ignore[arg-type]


def _normalize(entry: RssEntry) -> FetchedDocument:
    return normalize_entry(
        entry,
        source=_SOURCE,
        first_fetched_at=_FIRST_FETCHED,
        fetched_at=_FETCHED,
        collector_version="openai-rss/test",
        feed_etag='"etag-1"',
        feed_last_modified="Fri, 05 Sep 2026 12:00:00 GMT",
    )


# -- URL canonicalization ------------------------------------------------


def test_canonicalize_strips_fragment_tracking_params_and_trailing_slash() -> None:
    raw = "https://OpenAI.com/index/gpt-4o-256k-context/?utm_source=rss&utm_medium=feed&ref=homepage#summary"
    assert canonicalize_url(raw) == "https://openai.com/index/gpt-4o-256k-context"


def test_canonicalize_keeps_and_sorts_meaningful_query_params() -> None:
    raw = "https://openai.com/search?q=gpt&page=2&utm_source=rss"
    assert canonicalize_url(raw) == "https://openai.com/search?page=2&q=gpt"


def test_canonicalize_drops_default_port() -> None:
    assert canonicalize_url("https://openai.com:443/index/x") == "https://openai.com/index/x"


def test_canonicalize_rejects_non_http_url() -> None:
    with pytest.raises(EntryNormalizationError):
        canonicalize_url("ftp://openai.com/feed")


def test_canonicalize_rejects_malformed_url() -> None:
    with pytest.raises(EntryNormalizationError, match="malformed URL"):
        canonicalize_url("https://openai.com:notaport/x")


def test_canonicalize_rejects_out_of_range_port() -> None:
    with pytest.raises(EntryNormalizationError, match="malformed URL"):
        canonicalize_url("https://openai.com:99999/x")


def test_canonicalize_rejects_userinfo_and_does_not_echo_the_credential() -> None:
    with pytest.raises(EntryNormalizationError) as excinfo:
        canonicalize_url("https://alice:s3cr3t@openai.com/index/x")
    message = str(excinfo.value)
    assert "user-info" in message
    assert "s3cr3t" not in message
    assert "alice" not in message


def test_two_urls_differing_only_by_tracking_share_a_dedupe_key() -> None:
    a = canonicalize_url("https://openai.com/index/gpt-4o-256k-context/?utm_source=rss#summary")
    b = canonicalize_url("https://openai.com/index/gpt-4o-256k-context/?ref=digest#top")
    assert a == b
    assert dedupe_key(a) == dedupe_key(b)


def test_dedupe_key_is_a_sha256_prefixed_digest() -> None:
    key = dedupe_key("https://openai.com/index/x")
    assert key.startswith("sha256:")
    assert len(key) == len("sha256:") + 64
    assert key == dedupe_key("https://openai.com/index/x")


# -- content hashing ---------------------------------------------------


def test_normalized_content_collapses_whitespace_deterministically() -> None:
    a = normalized_content("Title   here", "Body\n\twith   runs")
    b = normalized_content("Title here", "Body with runs")
    assert a == b == "Title here\n\nBody with runs"


def test_content_hash_is_stable_and_prefixed() -> None:
    content = normalized_content("Title", "Body")
    assert content_hash(content) == content_hash(content)
    assert content_hash(content).startswith("sha256:")


def test_changed_body_changes_the_content_hash() -> None:
    original = content_hash(normalized_content("Title", "Body"))
    corrected = content_hash(normalized_content("Title", "Body. Correction: revised."))
    assert original != corrected


# -- published_at ----------------------------------------------------


def test_pub_date_is_normalized_to_aware_utc() -> None:
    parsed = parse_published_at("Tue, 01 Sep 2026 17:00:00 +0200")
    assert parsed == datetime(2026, 9, 1, 15, 0, tzinfo=UTC)
    assert parsed.tzinfo is UTC


def test_absent_pub_date_is_none() -> None:
    assert parse_published_at(None) is None


def test_unparseable_pub_date_is_an_entry_failure() -> None:
    with pytest.raises(EntryNormalizationError):
        parse_published_at("not a real date")


# -- normalize_entry field mapping ----------------------------------------


def test_normalize_entry_maps_every_contract_field() -> None:
    document = _normalize(_entry())

    assert document.source_id == "openai_news"
    assert document.canonical_url == "https://openai.com/index/gpt-4o-256k-context"
    assert document.dedupe_key == dedupe_key(document.canonical_url)
    assert document.content_hash == content_hash(document.content_text)
    assert document.first_fetched_at == _FIRST_FETCHED
    assert document.fetched_at == _FETCHED
    # raw_location stays None until immutable raw-object storage exists;
    # canonical_url is the public provenance URL.
    assert document.raw_location is None
    assert document.etag == '"etag-1"'
    assert document.last_modified == "Fri, 05 Sep 2026 12:00:00 GMT"
    assert document.collector_version == "openai-rss/test"
    assert document.metadata["publisher"] == "OpenAI"
    assert document.metadata["title"] == _entry().title
    # tags are case-folded (ADR 0002 §10); OpenAI's feed carries no authors.
    assert document.metadata["tags"] == ["product", "api"]
    assert document.metadata["authors"] == []
    assert document.metadata["published_at"] == datetime(2026, 9, 3, 13, 15, tzinfo=UTC)


def test_normalize_entry_requires_a_link() -> None:
    with pytest.raises(EntryNormalizationError, match="link"):
        _normalize(_entry(link=None))


def test_normalize_entry_requires_a_title() -> None:
    with pytest.raises(EntryNormalizationError, match="title"):
        _normalize(_entry(title=None))


def test_normalize_entry_propagates_a_bad_pub_date() -> None:
    with pytest.raises(EntryNormalizationError):
        _normalize(_entry(pub_date_raw="yesterday-ish"))


# -- Finding 2: entry link validated against the source policy ----------


def test_normalize_entry_accepts_an_allowlisted_openai_link() -> None:
    document = _normalize(_entry(link="https://openai.com/index/some-post"))
    assert document.canonical_url == "https://openai.com/index/some-post"


def test_normalize_entry_accepts_the_www_alias_on_the_allowlist() -> None:
    document = _normalize(_entry(link="https://www.openai.com/index/some-post"))
    assert document.canonical_url == "https://www.openai.com/index/some-post"


def test_normalize_entry_rejects_an_http_openai_link() -> None:
    with pytest.raises(EntryNormalizationError, match="https"):
        _normalize(_entry(link="http://openai.com/index/x"))


def test_normalize_entry_rejects_an_off_domain_https_link() -> None:
    with pytest.raises(EntryNormalizationError, match="allowlist"):
        _normalize(_entry(link="https://evil.example/index/x"))


def test_normalize_entry_rejects_a_link_with_userinfo_without_echoing_it() -> None:
    with pytest.raises(EntryNormalizationError) as excinfo:
        _normalize(_entry(link="https://alice:s3cr3t@openai.com/index/x?api_key=leak"))
    message = str(excinfo.value)
    assert "user-info" in message
    assert "s3cr3t" not in message
    assert "alice" not in message
    assert "api_key=leak" not in message


def test_normalize_entry_rejects_a_malformed_link() -> None:
    with pytest.raises(EntryNormalizationError):
        _normalize(_entry(link="https://openai.com:notaport/x"))


# -- Finding 1: ADR 0002 §10 list normalization ------------------------


def test_normalize_tags_nfc_normalizes_composed_and_decomposed_equivalents() -> None:
    composed = unicodedata.normalize("NFC", "caf\u00e9")
    decomposed = unicodedata.normalize("NFD", composed)
    assert composed != decomposed
    assert normalize_tags(["  " + decomposed + "  ", composed]) == [composed]


def test_normalize_tags_drops_empty_and_whitespace_only_values() -> None:
    assert normalize_tags(["Product", "", "   ", "\t\n"]) == ["product"]


def test_normalize_tags_casefolds_and_dedupes_after_casefolding() -> None:
    assert normalize_tags(["Product", "product", "PRODUCT", "API", "api"]) == ["product", "api"]


def test_normalize_tags_preserves_first_seen_order() -> None:
    assert normalize_tags(["Zeta", "Alpha", "zeta", "Beta"]) == ["zeta", "alpha", "beta"]


def test_normalize_tags_keeps_clean_openai_categories_working() -> None:
    assert normalize_tags(("Product", "API", "Research")) == ["product", "api", "research"]


def test_normalize_authors_preserves_case_and_first_seen_order() -> None:
    assert normalize_authors(["Ada Lovelace", "  Alan Turing  ", "Ada Lovelace"]) == [
        "Ada Lovelace",
        "Alan Turing",
    ]


def test_normalize_authors_is_case_sensitive_unlike_tags() -> None:
    # Same person written two ways is NOT deduped -- authors preserve case.
    assert normalize_authors(["Ada Lovelace", "ada lovelace"]) == ["Ada Lovelace", "ada lovelace"]


def test_normalize_authors_nfc_normalizes_drops_empty_and_dedupes() -> None:
    composed = unicodedata.normalize("NFC", "Ren\u00e9e")
    decomposed = unicodedata.normalize("NFD", composed)
    assert composed != decomposed
    assert normalize_authors(["  " + decomposed + "  ", composed, "", "  "]) == [composed]


def test_normalize_entry_casefolds_and_dedupes_entry_categories() -> None:
    document = _normalize(_entry(categories=("Product", "product", "Research")))
    assert document.metadata["tags"] == ["product", "research"]
