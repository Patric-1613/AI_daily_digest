# LLM agent specs — intelligence (Person B)

Not to be confused with the repo-root `AGENTS.md` (instructions *for*
coding agents like Codex/Claude). This document specs the product's own
LLM call sites — each one is effectively a small agent, and this is its
card. Keep each card in sync with its prompt file in
`intelligence/prompts/` and its model choice in `intelligence/CLAUDE.md`.

Maps onto `docs/ARCHITECTURE.md`'s intelligence workflow diagram:
`Classify relevance/entities → Retrieve history → Extract facts → Compare
→ Draft claims → Validate → Publish/Review`. Deterministic steps
(Compare, Draft for single-field changes, Validate) are plain code, not
LLM calls — per that same doc: "Prefer deterministic code ... use an LLM
only where deterministic rules are insufficient."

## Quickstart — running the whole pipeline

`daily_run.py::run_daily` is the actual entry point; everything else on
this page is a piece it calls. `store` and `known_snapshot_ids` are yours
to keep alive across calls (a global, a singleton, whatever fits once
this is wired into a real scheduler) — passing fresh ones each call means
starting history over from nothing every day.

```python
from ai_daily_digest.intelligence.daily_run import BatchItem, run_daily
from ai_daily_digest.intelligence.facts import FactStore

store = FactStore()  # keep this alive across daily runs
known_snapshot_ids: set[str] = set()  # same here

batch = [
    BatchItem(item=source_item, snapshot=document_snapshot),
    # ... one BatchItem per SourceItem ingestion produced today
]

result = run_daily(store, known_snapshot_ids, batch, digest_date="2026-08-26")

result.digest  # the Digest -- delivery renders/sends this
result.resolved_subjects  # Subjects touched today
result.unresolved_item_ids  # items nobody could resolve -- check these
```

No `ANTHROPIC_API_KEY` needed for testing: every LLM call site
(`resolve_via_llm`, `extract_facts`, `compare_subjects`) takes an
injectable `call_fn`, and `build_graph`/`run_daily` forward `*_call_fn`
kwargs straight through — see `tests/unit/test_daily_run.py` for a full
worked example with fakes standing in for all three.

## resolve_llm — Classify

- **File**: `intelligence/resolve_llm.py`, prompt `intelligence/prompts/resolve.txt`
- **Runs**: only on items deterministic alias matching (`intelligence/resolve.py`)
  left unmatched or ambiguous.
- **Input**: item title + snapshot text excerpt, list of candidate
  `Subject`s (company, product).
- **Output**: `{company: str|null, product: str|null, new_subject_proposal: str|null, confidence: float}`
- **Model**: Haiku 4.5. No `temperature` control — removed on current-gen
  models (400 if sent); see `intelligence/llm.py::call_structured`'s
  docstring.
- **Guardrail**: constrained JSON only, no free text. `confidence < 0.6` →
  logged for manual review, never auto-merged. A high-confidence proposal
  naming a company/product that isn't actually one of the candidates it
  was given is also never auto-merged (`method="llm_subject_not_in_candidates"`)
  — treated the same as a new-subject proposal.
- **Failure mode**: on parse/validation failure, `intelligence/llm.py`
  retries once with the error appended; on a second failure the item stays
  unresolved (never force-matched) and is logged.

## extract_facts — Extract

- **File**: `intelligence/extract_facts.py`, prompt `intelligence/prompts/extract_facts.txt`
- **Input**: one `Subject`, one `DocumentSnapshot`'s text, the closed field
  list (`shared/attributes.py`).
- **Output**: zero or more `ExtractedFact` (see `shared/schemas.py`) —
  one per field actually found. A field not mentioned is simply absent,
  never a null placeholder entry.
- **Model**: Sonnet 5. No `temperature` control — see resolve_llm's note
  above.
- **Guardrail** (all enforced in code, not just the prompt):
  1. `field` must be in the closed list — an invented field is dropped.
  2. `quoted_span` must actually appear in the snapshot text — a model
     that paraphrases instead of quoting produces a fact that's silently
     dropped, not silently stored. `confidence` below 0.6 is also dropped.
  3. `value` must itself be supported by `quoted_span` (see
     `intelligence/grounding.py::value_supported_by_quote`) — check #2
     alone doesn't catch a model quoting a real sentence but attaching a
     different, invented value to it.
  Accepted facts keep their `quoted_span`/`confidence` (ADR 0004) instead
  of discarding them, so grounding can be audited later.
- **Failure mode**: retry once on validation failure, then fail loudly —
  never persist a fact from an unvalidated response.

## FactStore.update_fact — Compare (deterministic, no LLM)

- **File**: `intelligence/facts.py`
- **Input**: a `Subject`, a new `ExtractedFact`, its snapshot id/source
  URL/observed_at.
- **Output**: a `Change` if the new value differs from what was
  previously known for (subject, field); `None` for a first-time
  observation or an identical value (both a true no-op here). **Known
  gap** (flagged in the second review, not yet resolved): a first
  observation is genuinely new information, but nothing downstream
  currently turns it into a `DigestClaim` either — there is no "new
  disclosure" claim path today, despite an earlier version of this
  comment implying one existed. Whether to build one is one of the open
  design questions in the second-review response, not assumed here.
- **Guardrail**: `previous` is always built from what was actually
  stored, never recomputed after the fact — history is append-only.

## draft_change_claim — Draft (deterministic, single-field changes only)

- **File**: `intelligence/draft_claims.py`
- **Input**: one `Change`.
- **Output**: one `DigestClaim`, `validation_status="pending"`, citing
  the change's current snapshot id (and previous, when it exists).
- **ADR 0006, disclosure transitions**: `change_type="disclosed"` (a
  field previously withheld now has a real value) reuses the existing
  first-disclosure wording ("X's Y is now disclosed as ..."); `"not_disclosed"`
  (a field previously disclosed is now explicitly withheld) gets its own
  wording ("X's Y is no longer disclosed (previously ...)."), citing both
  the snapshot proving the new state and the one that recorded the
  previous value.

## compare_subjects — Compare (cross-subject comparisons, ADR 0005)

- **File**: `intelligence/compare_subjects.py`, prompt
  `intelligence/prompts/compare_subjects.txt`
- **Input**: a `FactRow` table (`build_fact_table()`) — current values +
  snapshot ids for a set of subjects/fields, read from `FactStore`.
  Never raw article text — the architectural decision that stops
  fabricated competitive claims, carried over from the original design.
- **Output**: zero or more `DigestClaim`, one per accepted comparison.
- **Model**: Sonnet 5. No `temperature` control — see resolve_llm's note
  above.
- **ADR 0005: the model no longer writes claim prose or numbers at all.**
  It proposes structured `ComparisonAssertion(subject_a, subject_b,
  field)` triples only — which two subjects and which field are worth
  comparing. Code alone looks up both subjects' real stored values,
  applies the field's `ComparisonRule` (`shared/attributes.py`), and
  renders the claim text deterministically — the same pattern
  `draft_claims.py` already uses for single-subject changes. This closes
  the two fabrication classes a free-text-plus-numeric-check design could
  not: a claim asserting no number at all (nothing for a numeric check to
  verify), and a claim stating two real numbers attributed to the wrong
  subject (swapped) — a check confirming both numbers appear *somewhere*
  among real values can't detect a swap, but code deciding which number
  goes with which subject makes the swap structurally impossible.
- **Scope**: only fields with a registered `ComparisonRule`
  (`shared/attributes.py::COMPARISON_RULES`) are eligible for comparison
  — Phase 1 added `context_window_tokens`, Phase 2 added
  `input_price_usd`/`output_price_usd` (`PriceComparisonRule`: a plain
  USD numeric string, optional leading `$`/thousands separators
  tolerated; no currency conversion or per-token-vs-per-million-token
  basis reconciliation). Every other field (benchmark_scores,
  availability_regions, licence_terms, modalities) is still excluded
  from comparison until its own representation is designed via its own
  follow-up. Not a bug — a deliberate, disclosed scope limit.
- **Guardrail** (all enforced in code, not the prompt): an assertion
  naming the same subject twice, an unknown subject, a field with no
  registered `ComparisonRule`, a subject/field with no disclosed value,
  or a stored value the rule can't parse are all dropped and logged —
  never guessed at, and a malformed value drops only that one candidate,
  never the rest of the batch. Reversed-pair duplicates (same two
  subjects, same field, either order) are deduplicated, keyed by
  `(sorted((subject_a, subject_b)), field)` — the field is part of the
  key, so a different field for the same pair is never treated as a
  duplicate. A sparse table should yield an empty assertions list
  (abstention) — verified by an adversarial test
  (`tests/unit/test_compare_subjects.py`).
- **Failure mode**: no retry loop of its own (rides `call_structured`'s
  validate→retry-once→fail-loudly); an assertion that fails a guardrail
  is dropped and logged, not retried or "corrected".

## validate_digest / publish_digest — Validate (deterministic, no LLM)

- **File**: `intelligence/validate.py`
- **Input**: a `Digest`, the set of known/real snapshot ids, and a
  `snapshot_resolver: SnapshotResolver` (`shared/snapshot_resolver.py`,
  ADR 0005) — typed access to real snapshot content, not a raw dict.
  **Required** (no default) on `validate_digest`/`publish_digest` — the
  actual final gate before a digest can auto-publish, reached from
  `daily_run.py`, must never silently degrade to existence-only checking
  just because it forgot to pass one. `validate_claim()` keeps it
  optional (default `None`) since not every caller has one — see below.
- **Output**: `validate_digest` sets each claim's `validation_status`
  and forces `status="review"` if anything is unsupported — it never
  upgrades to `"published"` itself. `publish_digest` is the only place a
  `Digest.status` becomes `"published"`, and only when every claim is
  supported.
- **Guardrail**: a claim with zero citations is always unsupported,
  regardless of how the text reads. Citation *existence* alone was
  flagged in review as insufficient — a claim can cite a real snapshot
  id that has nothing to do with what it says. When `snapshot_resolver`
  is supplied, a second check also runs: every number the claim's text
  asserts must actually appear in the content of the snapshot(s) it
  cites (see `intelligence/grounding.py`) — and a citation whose content
  can't be resolved is never treated as proof of support (routes to
  "review", not silently trusted). `daily_run.py` supplies an
  `InMemorySnapshotResolver` built from the current batch's own
  snapshots; `graph.py`'s per-item `validate` node does not (it only has
  the current item's own snapshot, not every historical one a Change
  might cite, and it never authorizes publication by itself anyway) and
  keeps the existence-only check — documented as a known, deliberate
  limitation in that node's own docstring, not a regression.

## Orchestration — Classify → Extract → Compare → Draft → Validate

- **File**: `intelligence/graph.py::build_graph`
- A LangGraph `StateGraph` wiring the nodes above for **one `SourceItem`
  + its `DocumentSnapshot` at a time**: `classify_deterministic` →
  (conditionally) `classify_llm` → `extract` → `compare` → `draft` →
  `validate` → `END`. Every node is a thin wrapper around an
  already-tested function — the graph owns state passing and routing
  only, never business logic.
- "Retrieve related history" isn't a separate node: `classify` reads
  `FactStore.known_subjects()` and `compare` reads
  `FactStore.get_current_fact()` internally — `FactStore` *is* the
  retrieval mechanism.
- The `classify_llm` → `extract` edge is conditional: an item that
  resolves to no subject even after the LLM fallback ends the graph
  early (`route_after_llm`) rather than proceeding with `subject=None`.
- `resolve_llm_call_fn` and `extract_call_fn` are injectable on
  `build_graph()`, same pattern as the individual node functions — see
  `tests/unit/test_graph.py` for running the whole graph without a real
  API key.
- **A genuinely new subject (never seen before) has nothing to
  deterministically match against and always routes through
  `classify_llm`** — this surfaced from a real test bug during
  development, worth remembering when writing more tests here.

## assemble_digest — final step before delivery

- **File**: `intelligence/assemble_digest.py`
- **Input**: `digest_date`, the collected `DigestClaim`s (from running
  `graph.py::build_graph` over the day's items, plus
  `compare_subjects`), the set of known/real snapshot ids.
- **Output**: a `Digest`, run through `validate.py::publish_digest`.
- **Guardrail**: a digest with zero claims stays `"draft"` — it never
  auto-publishes an empty digest just because there was nothing to mark
  unsupported. Unsupported claims are kept in the digest (not dropped)
  so a reviewer can see exactly what failed.

## daily_run — the actual orchestrator

- **File**: `intelligence/daily_run.py::run_daily`
- **Input**: a shared `FactStore`, a shared `known_snapshot_ids` set (both
  owned by the caller and threaded across daily runs — the same objects
  come back in, mutated, so tomorrow's run sees today's history), a
  `list[BatchItem]` (item + snapshot pairs), and the digest date.
- **What it does**: builds one compiled graph and invokes it once per
  batch item (collecting `draft_change_claim` claims and which subjects
  got resolved), then — if ≥2 distinct subjects were touched — runs a
  single `compare_subjects` pass over them, then hands everything to
  `assemble_digest`.
- **Output**: a `DailyRunResult` — the `Digest`, the list of resolved
  `Subject`s, `unresolved_item_ids` (items that failed to resolve even
  after the LLM fallback), `failed_item_ids` (items whose processing
  *raised* — a transient failure, logged, the rest of the batch still
  runs), and `change_sets` — the batch's Changes grouped into `ChangeSet`
  aggregates (`intelligence/change_sets.py`) with `change_set_id`
  backfilled onto each Change (`FactStore.update_fact()` deliberately
  leaves this blank; `run_daily` is the caller that fills it in).
- Also builds an `InMemorySnapshotResolver` from the batch's real
  `DocumentSnapshot`s and threads it to `assemble_digest`/`validate.py`
  (a required parameter there, per ADR 0005) for the content-grounding
  check described above.
- This is the piece that turns "here are today's items" into an actual
  `Digest` — see `tests/unit/test_daily_run.py` for a full 3-item batch
  (a first observation, a change, and a cross-subject comparison) run
  end-to-end with injected fake `call_fn`s, no API key needed.

## evaluate — the four scored metrics

- **File**: `intelligence/evaluate.py`, `make eval`
- **Metrics**: citation validity, unsupported-claim count, duplicate
  rate, change recall — the four from the original project design.
- **Current state**: the metric functions are built and unit-tested with
  synthetic examples proving they actually detect problems (partial
  citation validity, detected duplicates, partial change recall), not
  just pass trivially. `main()` runs a **self-check** — it scores the
  current draft `tests/fixtures/contracts/` pack against itself, which
  proves the plumbing works but is not real evaluation signal (a
  self-check trivially scores ~100%).
- **What real signal needs**: the team's actual Milestone-0 fixture pack
  (held-out gold reference) plus a live run of `graph.py::build_graph`
  over it — which needs a real `ANTHROPIC_API_KEY` and a deliberate
  decision to spend on it, not something to do without asking.
- Results append to `docs/eval_results.md`, one row per run, never
  edited or deleted — only appended.

## Not yet built

- **Email/web rendering** of a published `Digest` — delivery's job per
  `docs/ARCHITECTURE.md`'s module boundaries, not intelligence's.
