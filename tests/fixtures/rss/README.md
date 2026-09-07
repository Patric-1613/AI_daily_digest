# RSS collector fixtures

Hand-authored RSS 2.0 documents modelled on the *structure* of the real
feeds the collector supports — element layout, namespaces (`content:`,
`dc:`, `atom:`), CDATA usage, and RFC 822 `pubDate` formatting. They
contain **no copied source text**: every title and description is an
invented, clearly-synthetic placeholder, so the fixtures are safe to
version and redistribute (`AGENTS.md`: respect source copyrights).

Regenerate or extend by editing these files directly. The collector's
unit and integration tests parse only these saved files — never a live
feed (`docs/ENGINEERING_STANDARDS.md`: "use saved fixtures instead of
live sites"; live-source smoke tests are a separate opt-in suite).

## OpenAI News feed shape (`openai.com`)

| File | What it exercises |
|---|---|
| `openai_news_sample.xml` | Four well-formed entries; tracking params, `+0000` offset dates, multiple `<category>` elements, CDATA. The happy path. |
| `openai_news_changed.xml` | Same four entries as the sample with entry 0's body edited — a genuine content revision (new snapshot, same item). |
| `openai_news_with_duplicate.xml` | Three entries where two canonicalize to the same URL with identical content — in-run deduplication. |
| `openai_news_malformed_entry.xml` | Valid entries around ones with an empty `<link>` and an unparseable `<pubDate>` — per-entry failure isolation. |
| `openai_news_bad_url_entry.xml` | Valid entries around ones with an invalid port and with `user:password@` credentials in the link — malformed-URL rejection and credential redaction. |
| `openai_news_offsite_entry.xml` | Valid entries around ones whose link is plain `http` or an off-allowlist host — the entry link is held to the source's fetch-safety policy before it is stored. |
| `openai_news_malformed_xml.xml` | Truncated, never-closed tags — a source-level parse failure. |
| `openai_news_with_entity.xml` | A `<!DOCTYPE>` with a declared/referenced entity — `defusedxml` must refuse it. |
| `openai_news_plain_dtd.xml` | A bare `<!DOCTYPE rss>` with no entity declaration or reference, otherwise valid RSS 2.0 — `forbid_dtd=True` must still refuse it. |
| `openai_news_empty.xml` | A syntactically valid feed with zero `<item>` elements — the zero-item source anomaly. |

## PyPI release feed shape (`pypi.org`)

Structural facts verified in the Day-4 preflight of
`https://pypi.org/rss/project/{langchain,langgraph}/releases.xml`: RSS
2.0, `Content-Type: text/xml`, ~10 KB, ~40 `<item>`s, each with
`<title>` (a version string), `<link>` (`https://pypi.org/project/<pkg>/<version>/`),
`<description>` (the project one-liner), and `<pubDate>` (RFC 822 GMT).
No `<guid>`, `<category>`, `<author>`, or namespaces. Every entry link is
on `pypi.org`.

| File | What it exercises |
|---|---|
| `pypi_langchain_sample.xml` | Three well-formed LangChain-release entries — the generic adapter's happy path for a non-OpenAI source. |
| `pypi_langchain_changed.xml` | Same three entries with entry 0's description revised — a content revision on a PyPI source. |
| `pypi_langgraph_sample.xml` | Three well-formed LangGraph-release entries — a second configured PyPI source through the same adapter. |
| `pypi_langchain_offsite_entry.xml` | Valid entries around one whose link host is off the `pypi.org` allowlist (and carries a `?token=` query) — per-source allowlist enforcement and credential redaction. |
