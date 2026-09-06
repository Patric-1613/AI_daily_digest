"""Deterministic normalization of a parsed `RssEntry` into the shared
`FetchedDocument` the ingestion service persists.

Every rule here is deterministic (no clock read except the injected
`fetched_at`/`first_fetched_at`, no randomness) so the same feed always
produces the same `dedupe_key` and `content_hash` -- which is what makes
a rerun idempotent and a genuine content change detectable
(`docs/ARCHITECTURE.md` "Collection flow" steps 6-8).
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ai_daily_digest.ingestion.persistence import SourceItemMetadata
from ai_daily_digest.ingestion.rss.parser import RssEntry
from ai_daily_digest.ingestion.rss.url_policy import sanitize_url
from ai_daily_digest.ingestion.service import FetchedDocument
from ai_daily_digest.ingestion.sources import SourceDefinition

# Query parameters that carry campaign/analytics tracking, not content
# identity -- stripped before the dedupe key is computed so the same
# article shared with different tracking still deduplicates. Anything
# starting `utm_` is dropped by prefix; these are the common non-`utm_`
# ones.
_TRACKING_PARAM_PREFIXES = ("utm_",)
_TRACKING_PARAM_NAMES = frozenset(
    {
        "gclid",
        "dclid",
        "fbclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "igshid",
        "ref",
        "ref_src",
        "ref_url",
        "referrer",
        "source",
        "cmpid",
        "campaign",
        "spm",
        "yclid",
        "_hsenc",
        "_hsmi",
    }
)

_WHITESPACE_RUN = re.compile(r"\s+")


class EntryNormalizationError(Exception):
    """The entry cannot be turned into a persistable document -- e.g. no
    link, no title, or an unparseable publication date. Recorded as a
    per-item failure; sibling entries continue."""


def _is_tracking_param(name: str) -> bool:
    lowered = name.lower()
    return lowered in _TRACKING_PARAM_NAMES or lowered.startswith(_TRACKING_PARAM_PREFIXES)


def canonicalize_url(raw: str) -> str:
    """One canonical form per article, so identity comparison and the
    dedupe key are stable:

    - scheme and host lower-cased; default ports removed;
    - fragment (`#...`) dropped;
    - tracking query parameters removed, the rest sorted;
    - a trailing slash removed from a non-root path.

    Raises `EntryNormalizationError` for anything that is not an absolute
    http(s) URL: a value that will not parse, an invalid port, a
    malformed host, or one carrying `user:password@` user-info. Error
    messages carry only a `sanitize_url`-redacted form of the input, so a
    credential in a feed link cannot leak into a failure report.
    """
    raw = raw.strip()
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise EntryNormalizationError(f"malformed URL: {sanitize_url(raw)}") from exc
    if parts.username or parts.password:
        raise EntryNormalizationError(f"URL must not contain user-info: {sanitize_url(raw)}")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise EntryNormalizationError(f"not an absolute http(s) URL: {sanitize_url(raw)}")

    host = parts.hostname.lower()
    if port is not None and not (
        (parts.scheme == "http" and port == 80) or (parts.scheme == "https" and port == 443)
    ):
        host = f"{host}:{port}"

    kept = sorted(
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking_param(key)
    )
    query = urlencode(kept)

    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    return urlunsplit((parts.scheme.lower(), host, path, query, ""))


def dedupe_key(canonical_url: str) -> str:
    """`sha256:` + hex digest of the canonical URL bytes -- the
    deterministic identity key `docs/ARCHITECTURE.md` step 7 and
    `SourceItem.dedupe_key` require."""
    digest = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def normalized_content(title: str, body: str | None) -> str:
    """The canonical text of an entry for hashing *and* for
    `DocumentSnapshot.content_text`. Deterministic: strip both parts,
    collapse internal whitespace runs to a single space, join with a
    blank line. Using the same string for the hash and the stored text
    means the stored snapshot is exactly what was hashed."""
    parts = [_WHITESPACE_RUN.sub(" ", segment).strip() for segment in (title, body or "")]
    return "\n\n".join(part for part in parts if part)


def content_hash(content: str) -> str:
    """`sha256:` + hex digest of the normalized content bytes."""
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def parse_published_at(raw: str | None) -> datetime | None:
    """RFC 822 `pubDate` -> timezone-aware UTC `datetime`. Absent ->
    `None` (publisher publication time is nullable). Present but
    unparseable -> `EntryNormalizationError` (malformed data)."""
    if raw is None:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError) as exc:
        raise EntryNormalizationError(f"unparseable pubDate {raw!r}") from exc
    if parsed.tzinfo is None:
        # RFC 822 without a zone -- treat as UTC rather than guess a local zone.
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def normalize_entry(  # pylint: disable=too-many-arguments
    entry: RssEntry,
    *,
    source: SourceDefinition,
    first_fetched_at: datetime,
    fetched_at: datetime,
    collector_version: str,
    feed_etag: str | None,
    feed_last_modified: str | None,
) -> FetchedDocument:
    """Turn one parsed entry into a `FetchedDocument`. Raises
    `EntryNormalizationError` for any entry the collector should record
    as a failure and skip."""
    if not entry.link:
        raise EntryNormalizationError("entry has no <link>")
    if not entry.title:
        raise EntryNormalizationError("entry has no <title>")

    canonical = canonicalize_url(entry.link)
    content = normalized_content(entry.title, entry.description)
    published_at = parse_published_at(entry.pub_date_raw)

    metadata: SourceItemMetadata = {
        "publisher": source.publisher,
        "title": entry.title,
        "published_at": published_at,
        "updated_at": None,
        "authors": [],  # OpenAI's RSS carries no <author>/<dc:creator>.
        "tags": list(entry.categories),
        "language": "en",
        "event_id": None,
    }

    return FetchedDocument(
        dedupe_key=dedupe_key(canonical),
        source_id=source.id,
        canonical_url=canonical,
        first_fetched_at=first_fetched_at,
        fetched_at=fetched_at,
        content_hash=content_hash(content),
        content_text=content,
        metadata=metadata,
        # `raw_location` stays None: it is meant for a pointer into
        # immutable raw-object storage (the stored HTTP response), which
        # this slice does not build. The public provenance URL is
        # `canonical_url`; putting the article URL here too would falsely
        # imply a retained raw artifact. Populate it when raw-object
        # storage exists.
        raw_location=None,
        etag=feed_etag,
        last_modified=feed_last_modified,
        collector_version=collector_version,
    )
