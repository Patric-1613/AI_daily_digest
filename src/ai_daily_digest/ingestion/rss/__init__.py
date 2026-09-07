"""OpenAI News RSS collector -- the first ingestion source adapter
(`docs/ARCHITECTURE.md`, "Collection flow"). Fetch (transport), parse
(parser), normalize into the shared `SourceItem`/`DocumentSnapshot`
contract (normalize), and persist one transaction per item through the
existing ingestion service (collector).
"""
