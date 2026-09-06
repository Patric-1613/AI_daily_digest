# OpenAI News RSS fixtures

Hand-authored RSS 2.0 documents modelled on the structure of the official
OpenAI News feed (`https://openai.com/news/rss.xml`) — element layout,
namespaces (`content:`, `dc:`, `atom:`), CDATA usage, and RFC 822
`pubDate` formatting. They contain **no copied OpenAI article text**;
titles and descriptions are invented placeholders, so the feed content is
safe to version and redistribute (`AGENTS.md`: respect source copyrights).

Regenerate or extend by editing these files directly. The collector's
unit and integration tests parse only these saved files — never the live
feed (`docs/ENGINEERING_STANDARDS.md`: "use saved fixtures instead of
live sites"; live-source smoke tests are a separate opt-in suite).

| File | What it exercises |
|---|---|
| `openai_news_sample.xml` | Four well-formed entries; tracking params, `+0000` offset dates, multiple `<category>` elements, CDATA. The happy path. |
| `openai_news_changed.xml` | Same four entries as the sample with entry 0's body edited — a genuine content revision (new snapshot, same item). |
| `openai_news_with_duplicate.xml` | Three entries where two canonicalize to the same URL with identical content — in-run deduplication. |
| `openai_news_malformed_entry.xml` | Valid entries around ones with an empty `<link>` and an unparseable `<pubDate>` — per-entry failure isolation. |
| `openai_news_bad_url_entry.xml` | Valid entries around ones with an invalid port and with `user:password@` credentials in the link — malformed-URL rejection and credential redaction. |
| `openai_news_malformed_xml.xml` | Truncated, never-closed tags — a source-level parse failure. |
| `openai_news_with_entity.xml` | A `<!DOCTYPE>` with a declared/referenced entity — `defusedxml` must refuse it. |
| `openai_news_empty.xml` | A syntactically valid feed with zero `<item>` elements — the zero-item source anomaly. |
