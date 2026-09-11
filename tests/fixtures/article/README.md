# Official-article test fixtures

The HTML files in this directory are short synthetic fixtures. They model the
metadata and comparable statements needed by the deterministic parser tests;
they are not archived copies of publisher pages and are not demo evidence.

`tests/live/test_official_article_backfill_live.py` is the opt-in smoke test
that fetches the real, public Anthropic announcement URLs. Production and
staging evidence is always collected from those allowlisted first-party pages,
never from these fixtures.
