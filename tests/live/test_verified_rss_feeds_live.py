"""Opt-in / manual live-source smoke test for the verified RSS sources.

`tests/README.md`: "opt-in source smoke tests; never part of the normal
local or PR suite." `docs/ENGINEERING_STANDARDS.md` calls for
"live-source smoke tests: verify official feeds still parse without
importing their content into normal test results" -- this file provides
that check as an **opt-in, manually run** suite. There is no scheduled
GitHub Actions workflow that runs it yet; adding one is a separate
future issue.

Marked `@pytest.mark.live`, so `make check` (`-m "not integration and not
e2e and not live"`) and `make ci` (`-m "not live"`) both exclude it. Run
it deliberately:

    uv run pytest -m live tests/live/ -q

This guards the exact regression class GitHub issue #91 raised: an
upstream feed change that moves the entry links off a source's
`allowed_hosts`, changes the feed URL / redirect target, or breaks the
RSS 2.0 shape. It fetches each verified feed through the *same* transport
the collector uses (project `HttpxFetcher` + `fetch_with_retry`: HTTPS
only, per-source host allowlist, manually validated redirects, approved
content type), parses it with the project parser, and re-runs the exact
per-entry `require_safe_url` / `normalize_entry` gates the collector
applies -- **without** touching a database and **without** fetching any
article page. Feed content is read only for validation and is not
persisted or logged; assertion messages carry only counts, host names,
and the source's own allowlist -- never a full URL, feed text, or
secret.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlsplit

import pytest

from ai_daily_digest.ingestion.rss.batch import _VERIFIED_RSS_SOURCE_IDS
from ai_daily_digest.ingestion.rss.collector import RSS_COLLECTOR_VERSION
from ai_daily_digest.ingestion.rss.normalize import EntryNormalizationError, normalize_entry
from ai_daily_digest.ingestion.rss.parser import parse_rss
from ai_daily_digest.ingestion.rss.transport import HttpxFetcher, fetch_with_retry
from ai_daily_digest.ingestion.rss.url_policy import UnsafeUrlError, require_safe_url
from ai_daily_digest.ingestion.sources import load_source_registry

pytestmark = pytest.mark.live

_FIXED_TIME = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.asyncio
@pytest.mark.parametrize("source_id", _VERIFIED_RSS_SOURCE_IDS)
async def test_verified_feed_still_parses_and_passes_the_source_policy(source_id: str) -> None:
    registry = load_source_registry()
    source = registry.get(source_id)
    allowed_hosts = source.host_allowlist()

    response = await fetch_with_retry(
        HttpxFetcher(),
        str(source.url),
        policy=registry.collection_policy,
        allowed_hosts=allowed_hosts,
    )

    feed = parse_rss(response.body)
    assert feed.entries, f"{source_id}: live feed parsed but has zero <item> elements"

    rejected_hosts: set[str] = set()
    normalization_failures = 0
    for entry in feed.entries:
        link = (entry.link or "").strip()
        assert link, f"{source_id}: a live entry has no <link>"
        try:
            require_safe_url(link, allowed_hosts=allowed_hosts)
        except UnsafeUrlError:
            rejected_hosts.add((urlsplit(link).hostname or "<none>").lower())
            continue
        try:
            normalize_entry(
                entry,
                source=source,
                first_fetched_at=_FIXED_TIME,
                fetched_at=_FIXED_TIME,
                collector_version=RSS_COLLECTOR_VERSION,
                feed_etag=None,
                feed_last_modified=None,
            )
        except EntryNormalizationError:
            normalization_failures += 1

    assert not rejected_hosts, (
        f"{source_id}: live entry links use host(s) not on allowed_hosts="
        f"{sorted(allowed_hosts)}: {sorted(rejected_hosts)} -- update sources.yaml "
        f"(see issue #91) after confirming they are official destinations"
    )
    assert normalization_failures == 0, (
        f"{source_id}: {normalization_failures}/{len(feed.entries)} live entries "
        f"failed normalization -- the feed shape may have changed"
    )
