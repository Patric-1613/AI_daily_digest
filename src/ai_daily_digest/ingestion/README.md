# Ingestion module

Owned primarily by Person A. Contains source adapters, collection orchestration, normalization, immutable snapshots, provenance, and duplicate detection.

Public contracts belong in `shared`; test fixtures belong under `tests/fixtures`.

## RSS adapter (`ingestion/rss/`)

One reusable RSS 2.0 collector serves every `sources.yaml` entry whose
`type` is `rss`. `collect_rss_source()` takes a validated
`SourceDefinition` and applies the same tested pipeline to all of them:
per-source HTTPS + host-allowlist enforcement, manual one-hop redirect
validation, `defusedxml` parsing (DTD / entity / external-reference
rejection), a streamed response-size cap, a bounded timeout with jittered
retry, per-entry failure isolation, deterministic URL canonicalization
and SHA-256 dedupe/content hashes, ADR 0002 §10 author/tag normalization,
persistence through the existing `IngestionWriteRepository` protocol, and
a structured, secret-safe `CollectionReport`.

`collect_openai_rss` is a thin backwards-compatible **wrapper** (not a
second implementation) that keeps the exact keyword-only signature PR #71
exported — which already required `source=` — and forwards every argument
to `collect_rss_source`. The one visible change is the default
`collector_version` label (`openai-rss/0.1.0` → `rss/0.1.0`); that label
is snapshot metadata only and is not an input to the canonical URL,
`dedupe_key`, normalized content, or `content_hash`, so relabelling never
causes a re-snapshot and existing OpenAI rows stay `unchanged`.

### Verified source IDs

`collect-rss --source-id <id>` accepts **any** registered source whose
`type` is `rss`. The adapter is deliberately generic and fail-safe: an
unrecognised feed shape produces a `failed`/`partial` `CollectionReport`,
never a crash and never a false success. There is intentionally **no
hardcoded source-ID allowlist**.

Only these three sources have been structurally preflighted against the
live feed and covered by source-specific unit and PostgreSQL integration
tests:

| `source_id` | Publisher | Feed |
|---|---|---|
| `openai_news` | OpenAI | `https://openai.com/news/rss.xml` |
| `langchain_pypi` | Python Package Index (subject: LangChain) | `https://pypi.org/rss/project/langchain/releases.xml` |
| `langgraph_pypi` | Python Package Index (subject: LangGraph) | `https://pypi.org/rss/project/langgraph/releases.xml` |

Every other `type: rss` entry in `sources.yaml` (`google_ai_blog`,
`google_deepmind`, `deepagents_pypi`, `huggingface_blog`) is **not yet
verified**. `deepagents_pypi` carries an explicit
`allowed_hosts: [pypi.org]` for host-guard consistency with the two
verified PyPI sources, but that does **not** make it verified or
production-supported. `collect-rss` will still attempt any of them, and if
the format is incompatible with the RSS 2.0 parser the run fails safely
with a structured report — but those sources are **not
production-supported** by this slice and must be preflighted and given
their own tests before being relied on.

### Registry subject and intelligence resolution — deferred

`sources.yaml` gives `langchain_pypi` / `langgraph_pypi` a `subject`
(`LangChain` / `LangGraph`) that identifies the package at the **registry**
level. This ingestion slice does **not** wire that through to the
intelligence layer:

- `SourceItem` (`shared/schemas.py`) has no `subject` column, so the
  registry `subject` is not persisted with the collected item.
- The current intelligence resolver (`intelligence/resolve.py`) matches
  on the item's title/text, not on a registry-supplied subject.

This PR therefore guarantees **collection and persistence** for the three
sources; it does **not** claim that LangChain/LangGraph subject
resolution is complete. Designing or wiring that integration (persist the
subject, or have the resolver consume the registry) belongs to Person B's
PostgreSQL pipeline-wiring / audit work and is out of scope here — no
intelligence code, shared schema, or ADR 0002 change is made in this PR.

### Running one source manually

```bash
# any registered source whose type is rss (the three verified ids, or another):
DATABASE_URL=postgresql+psycopg://USER:PW@HOST:5432/DB \
  uv run collect-rss --source-id langchain_pypi

# compatibility command, equivalent to --source-id openai_news:
uv run collect-openai-rss
```

The command loads `sources.yaml`, requires `--source-id` to name a
`type: rss` source (an unknown id or a non-RSS type exits `2` with a
one-line JSON error, before any database connection), reads `DATABASE_URL`
through `shared.config.DatabaseConfig`, uses the shared engine / session
factory, runs one collection pass, prints the `CollectionReport` as one
line of JSON, and exits `0` ok / `1` partial / `2` failed.

### Batch collection (`collect-rss-batch`)

`collect-rss-batch` (`ingestion/rss/batch.py`) is the separately invoked
worker command that collects several sources in one pass. It is the "one
separately invoked job command" from `docs/ARCHITECTURE.md` ("Scheduling")
and is safe for a cloud-cron worker. It does **not** run inside FastAPI
and adds **no** scheduling endpoint or cron configuration.

What it does, in order:

1. loads `sources.yaml`;
2. selects sources: with no flag, the **verified production RSS source
   ids** — `openai_news`, `langchain_pypi`, `langgraph_pypi` (the
   "Verified source IDs" table above); or exactly the `--source-id`
   values given (repeatable) for a controlled or manual run;
3. resolves every selected id to a validated `type: rss`
   `SourceDefinition` **before any network or database work** — an empty,
   unknown, or non-RSS selection exits `2` with a one-line JSON error;
4. reads `DATABASE_URL`, builds the one shared engine + session factory,
   preflights it;
5. runs each source through the **same** `collect_rss_source` adapter
   under a bounded `asyncio.Semaphore` (`--concurrency`, default 3,
   range 1–8). Per-source timeout, bounded retry with jittered backoff,
   canonical-URL and content-hash deduplication, immutable snapshots and
   PostgreSQL persistence are all the adapter's, unchanged. One source
   failing never cancels the others;
6. prints one structured, secret-safe JSON batch report:
   `job_run_id` (UUID v7), `started_at` / `finished_at`,
   `requested_source_ids`, `concurrency`, overall `status`, a `totals`
   block (`requested`, `succeeded`, `partial`, `failed`,
   `items_processed`, `new_snapshots`, `unchanged`, `failed_items`), and a
   per-source summary (`status`, the collection counts, `fetch_attempts`,
   `error_category`). It carries **no** `failures` list, entry link,
   query string, feed content, or raw exception string — only counts and
   a fixed-vocabulary `error_category`.

Exit codes: `0` = every selected source finished `ok`; `1` = partial
(some useful work, some not); `2` = configuration / selection failure, or
every selected source failed. This extends the single-source `collect-rss`
convention (`ok`→0 / `partial`→1 / `failed`→2) to a whole batch.

```bash
# the verified set, default concurrency:
DATABASE_URL=postgresql+psycopg://USER:PW@HOST:5432/DB uv run collect-rss-batch

# a controlled subset:
uv run collect-rss-batch --source-id openai_news --source-id langchain_pypi --concurrency 2
```

#### Scheduling this command (Person C)

For the zero-cost MVP an authorised operator runs it manually from a
checkout (`docs/DEPLOYMENT.md`: "A manually invoked worker command is
therefore the MVP baseline"). When a Render Cron Job is later approved and
budgeted, configure it with **exactly**:

```
uv sync --locked --no-dev --no-editable && uv run collect-rss-batch
```

with `DATABASE_URL` set to the Render Postgres reference. The command is
idempotent (re-running re-collects nothing unchanged), needs no
interactive input, emits one JSON line to stdout, and its exit code is
cron-actionable (`0` ok, `1` partial, `2` investigate). No deployment file
is changed by this slice.

#### Deferred: durable run history and `--due` cadence

`collect-rss-batch` always runs the full selected set. Cadence-aware
`--due` selection needs durable, reliable per-source last-attempt state,
and `collection_runs` has no accepted schema yet — ADR 0002 §7 defers it
to "the feature that needs it, in its own migration, owned by the module
that owns it". A `collection_runs` table + `--due` filtering is a
follow-up that starts with that schema decision; this slice deliberately
adds neither.

### Limitations (this slice)

- **RSS 2.0 only.** Atom and other feed formats are out of scope; the
  parser targets the RSS 2.0 shape (`<rss><channel><item>`).
- **`collect-rss` runs one source, once.** `collect-rss-batch` runs
  several (the verified set, or an explicit `--source-id` selection) in
  one bounded-concurrency pass. Neither reads `cadence_minutes`;
  cadence-aware `--due` selection is deferred (see "Batch collection").
- **No article-body fetching.** `content_text` is the feed entry summary
  (`<description>` / `<content:encoded>`), never the fetched article page.
- **No raw-object storage.** `DocumentSnapshot.raw_location` stays `NULL`
  until immutable raw storage exists.
- **HTML / changelog / GitHub-API sources are not implemented.** Their
  `SourceType` members exist (ADR 0009 §5.1) but no adapter does.
- **Only three RSS sources are verified.** Other `type: rss` entries are
  accepted by `collect-rss` but unverified — see "Verified source IDs".
- **Registry `subject` is not resolved.** See "Registry subject and
  intelligence resolution — deferred".
