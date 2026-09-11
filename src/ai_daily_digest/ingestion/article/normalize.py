"""Normalize a parsed first-party HTML article into a persistable document."""

from __future__ import annotations

from datetime import datetime

from ai_daily_digest.ingestion.article.parser import ParsedArticle
from ai_daily_digest.ingestion.persistence import SourceItemMetadata
from ai_daily_digest.ingestion.rss.normalize import (
    EntryNormalizationError,
    canonicalize_url,
    content_hash,
    dedupe_key,
    normalize_authors,
    normalize_tags,
    normalized_content,
)
from ai_daily_digest.ingestion.rss.url_policy import UnsafeUrlError, require_safe_url
from ai_daily_digest.ingestion.service import FetchedDocument
from ai_daily_digest.ingestion.sources import SourceDefinition


def normalize_article(  # pylint: disable=too-many-arguments
    article: ParsedArticle,
    *,
    requested_url: str,
    source: SourceDefinition,
    fetched_at: datetime,
    collector_version: str,
    etag: str | None,
    last_modified: str | None,
) -> FetchedDocument:
    """Validate provenance and create the existing ingestion write-model."""
    raw_canonical = article.canonical_url or requested_url
    try:
        require_safe_url(raw_canonical, allowed_hosts=source.host_allowlist())
    except UnsafeUrlError as exc:
        raise EntryNormalizationError(f"unsafe canonical URL: {exc}") from exc
    canonical = canonicalize_url(raw_canonical)
    content = normalized_content(article.title, article.body)
    metadata: SourceItemMetadata = {
        "publisher": source.publisher,
        "title": article.title,
        "published_at": article.published_at,
        "updated_at": None,
        "authors": normalize_authors(article.authors),
        "tags": normalize_tags(article.tags),
        "language": "en",
        "event_id": None,
    }
    return FetchedDocument(
        dedupe_key=dedupe_key(canonical),
        source_id=source.id,
        canonical_url=canonical,
        first_fetched_at=fetched_at,
        fetched_at=fetched_at,
        content_hash=content_hash(content),
        content_text=content,
        metadata=metadata,
        raw_location=None,
        etag=etag,
        last_modified=last_modified,
        collector_version=collector_version,
    )
