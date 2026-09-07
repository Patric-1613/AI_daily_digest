# 0009 — Project-wide Enum policy for lifecycle statuses and controlled vocabularies

Status: Accepted by Persons A, B, and C
Date: 2026-09-02
Amended: 2026-09-06 — the first ingestion vertical slice (OpenAI RSS collector,
PR #71) activates the deferred source-adapter-type enum. Section 5's
"source adapter types" deferral is superseded by the new section 5.1
(`SourceType`, ingestion-local); no other decision in this ADR changes and its
accepted status stands.

## Context

Several shared data models in `src/ai_daily_digest/shared/schemas.py` and API responses use string
literals or `Literal[...]` types to represent discrete lifecycle states and controlled vocabularies
(e.g. `disclosure_status`, `extraction_method`, `validation_status`, and `status`).

Currently, these strings are validated ad-hoc or via Pydantic type annotations. However:

1. Pure string annotations allow typos and case mismatches if not strictly guarded by runtime
   validation.
2. Inconsistent string values can leak across module boundaries (e.g. Ingestion -> Intelligence ->
   Delivery).
3. Delivery will soon expose OpenAPI documentation via FastAPI; explicit Enum definitions provide
   structured enum schemas in generated OpenAPI contracts without manual schema overrides.

At the same time, over-constraining open fields (such as model IDs, company names, product slugs, or
extensible change types) into rigid Enums would break forward compatibility and create unnecessary
merge friction.

This ADR establishes a project-wide policy for when, where, and how Enums must be introduced and
maintained.

---

## Decision

### 1. Governing Rule for Enum Usage

The project adopts a single governing rule across all modules:

> **Use an Enum only when the application owns a complete, deliberately closed set of values and
> application behavior directly depends on the value.**
> Values that are externally controlled, unbounded, or naturally expanding must remain plain strings.

---

### 2. Technology: Standard Library `enum.StrEnum`

We select Python 3.12 standard-library `enum.StrEnum` (`from enum import StrEnum`).

- **Zero Dependencies**: Requires no third-party package.
- **String & JSON Compatibility**: `StrEnum` instances inherit from `str`, ensuring
  `isinstance(val, str)` is true, string serialization is native, and wire JSON output is identical.
- **Pydantic & FastAPI Support**: Pydantic v2 and FastAPI natively generate OpenAPI `enum` schemas for
  `StrEnum` fields.

---

### 3. Phase 1 Shared Enums (`shared/schemas.py`)

Phase 1 introduces four shared `StrEnum` definitions, placed directly in
`src/ai_daily_digest/shared/schemas.py` beside their owning Pydantic models:

1. **`DisclosureStatus(StrEnum)`** (owning model: `ExtractedFact`):
   - `DISCLOSED = "disclosed"`
   - `NOT_DISCLOSED = "not_disclosed"`
2. **`ExtractionMethod(StrEnum)`** (owning model: `ExtractedFact`):
   - `DETERMINISTIC = "deterministic"`
   - `LLM_STRUCTURED_OUTPUT = "llm_structured_output"`
3. **`ClaimValidationStatus(StrEnum)`** (owning model: `DigestClaim`):
   - `PENDING = "pending"`
   - `SUPPORTED = "supported"`
   - `UNSUPPORTED = "unsupported"`
4. **`DigestStatus(StrEnum)`** (owning model: `Digest`):
   - `DRAFT = "draft"`
   - `REVIEW = "review"`
   - `PUBLISHED = "published"`

---

### 4. ChangeType Decision: Keep `Change.change_type` Open (`str`)

`Change.change_type` in `shared/schemas.py` remains typed as **`str`** (open set).

- **Rationale**: `validate_change_shape()` in `shared/schemas.py` and its regression tests
  deliberately support future unknown change types, enforcing generic evidence and observation-shape
  invariants regardless of the specific change string.
- **Intelligence-Local Enum**: Intelligence will define a module-local `InferredChangeType(StrEnum)`
  in `src/ai_daily_digest/intelligence/facts.py` covering the five values produced by
  `_infer_change_type()`:
  - `INCREASED = "increased"`
  - `DECREASED = "decreased"`
  - `CHANGED = "changed"`
  - `DISCLOSED = "disclosed"`
  - `NOT_DISCLOSED = "not_disclosed"`
- Arbitrary caller overrides remain possible on `Change` and will continue to pass generic shape
  validation. Closing `Change.change_type` globally in `shared` is explicitly deferred and would
  require a future ADR amendment.

---

### 5. Explicit Deferrals (Values that Remain Strings for Now)

The following fields remain `str` and are NOT converted to Enums in Phase 1:

- **`Change.review_status` / `ChangeSet.review_status`**: The human-review lifecycle and approval
  state machine transitions are not yet fully specified.
- **`FactRow.disclosure_status` (in `compare_subjects.py`)**: Remains an intelligence-local
  `Literal["unknown", "disclosed", "not_disclosed"]`.
  - *Critical Distinction*: Persisted `DisclosureStatus` only contains `disclosed` and
    `not_disclosed`. `"unknown"` represents the absence of an `ExtractedFact` row and MUST NEVER
    become a member of the persisted `DisclosureStatus` Enum.
- **Resolution methods, comparison relations, email delivery statuses**: Deferred until their
  respective vertical slices are implemented.
- **Source adapter types (`SourceType`)**: **Activated 2026-09-06** by the first ingestion
  vertical slice — see section 5.1. (Previously listed here as deferred.)

---

### 5.1 Source adapter types — `SourceType` (ingestion-local, activated 2026-09-06)

The OpenAI RSS collector (PR #71) is the first ingestion vertical slice, so the source-adapter-type
enum this ADR deferred in section 5 is now defined. It follows the same rules as every other
module-local enum:

- **Placement — ingestion module, not `shared/`.** `SourceType(StrEnum)` lives in
  `src/ai_daily_digest/ingestion/sources.py`, the module that owns the `sources.yaml` registry
  (section 7: "Module-local Enums live in their respective owning domain module"). No other module
  needs it, so it does not go in `shared/schemas.py`.
- **Meaning — the closed set of adapter *categories* the source registry accepts.** Members:
  `rss`, `html`, `changelog`, `github_releases_api`. Application behaviour depends directly on the
  value (which collector runs, if any), and the set is deliberately closed, so the section 1
  governing rule applies.
- **Enum membership is not collector implementation.** Only `rss` has an implemented collector in
  PR #71. The `html`, `changelog`, and `github_releases_api` members correspond to categories
  **already present in `sources.yaml`** (for example `openai_release_notes` is `changelog`,
  `anthropic_news` is `html`, `langchain_github_releases` is `github_releases_api`); their
  collectors remain **deferred** to their own vertical slices. A member records that the registry
  may legally declare that category — it does **not** assert that a working adapter exists. These
  unimplemented collectors are not marked complete anywhere.
- **Runtime dispatch must fail clearly for a category with no adapter.** A collection run that
  reaches a registered source whose `SourceType` has no implemented collector must raise an
  explicit error, never silently skip or mishandle it. The RSS adapter guards this
  (`collect_rss_source` raises `ValueError` for a non-RSS `SourceDefinition`; the `collect-rss`
  command rejects a non-RSS `--source-id` before any database connection). Any later
  HTML/changelog/GitHub-API adapter carries the same obligation.
- **Adding a new adapter category is a reviewed change** to both the `SourceType` enum and
  `sources.yaml`, exactly as sections 1 and 7 require for any closed application-owned set.

---

### 6. Values Remaining Open in Phase 1

The following fields remain plain strings:

- `publisher`, `company`, `product`
- `source_id` (slug in `sources.yaml`)
- `event_id`
- `dedupe_key`, `content_hash`
- fact/change field names (`field`)
- `tags`, `url`, `language`, external model identifiers (`extraction_model`)

Any future proposal to convert one of these open taxonomies into a closed Enum requires an ADR
amendment and backwards-compatibility review.

---

### 7. Code Placement Policy

- **No Miscellaneous Dumping Grounds**: Shared Enums must live directly in
  `src/ai_daily_digest/shared/schemas.py` next to the Pydantic models that own them.
- Do NOT create generic bucket modules such as `shared/enums.py`, `shared/statuses.py`, or
  `shared/vocabularies.py`.
- Do NOT place lifecycle statuses in `src/ai_daily_digest/shared/attributes.py` (which is dedicated
  exclusively to comparison attributes and rules).
- Module-local Enums (such as `InferredChangeType`) live in their respective owning domain module
  (e.g. `intelligence/facts.py`).

---

### 8. Compatibility & Wire Format Guarantees

Implementation of this policy must preserve:

1. **Wire Compatibility**: JSON serialization (`model_dump(mode="json")`, `model_dump_json()`) emits
   exact lowercase string values (e.g. `"disclosed"`, `"published"`).
2. **Fixture Compatibility**: Existing test fixtures in `tests/fixtures/contracts/*.json` continue to
   load and validate without modification.
3. **Strict Boundary Validation**: Pydantic models reject misspelled, wrong-case, or invalid strings
   during validation.
4. **`model_copy` Invariant**: `model_copy(update=...)` does not re-validate inputs; callers must pass
   Enum members rather than raw unvalidated strings.
5. **OpenAPI & Generated Clients**: FastAPI will generate `enum` schemas in the OpenAPI specification
   from these shared types. Delivery must reuse these shared Enums and never duplicate equivalent Enum
   definitions. Note that adding a future member to a response Enum can break exhaustive pattern
   matches in generated clients (e.g. TypeScript / Rust), and therefore requires a compatibility
   review.

---

## Consequences

- **Stricter Boundary Rejection**: Pydantic models will immediately reject malformed, wrong-case, or
  unrecognized strings at input boundaries across all modules.
- **Type Safety in Python**: Python attributes on instantiated models become `StrEnum` members,
  providing IDE autocompletion, type-checker verification, and elimination of typo-prone string
  literals.
- **Unchanged Wire JSON**: Wire serialization remains 100% backwards-compatible lowercase JSON
  strings; existing clients and fixtures require zero migration.
- **`model_copy` Invariant**: Because Pydantic `model_copy(update=...)` bypasses validation,
  developers must explicitly pass `StrEnum` members rather than raw strings when updating model
  fields.
- **Zero Production Dependencies**: Uses standard library `enum.StrEnum` in Python 3.12 without adding
  third-party packages.
- **Client Evolution Governance**: Any future addition of response enum members requires deliberate
  compatibility review to avoid breaking exhaustive generated clients.

---

## Future Implementation Scope (Separate PR)

Once this ADR is accepted by Persons A, B, and C, a follow-up implementation PR will:

1. Add the four `StrEnum` definitions to `src/ai_daily_digest/shared/schemas.py`.
2. Add `InferredChangeType` to `src/ai_daily_digest/intelligence/facts.py`.
3. Update Pydantic model field annotations and defaults in `shared/schemas.py`.
4. Re-use `DisclosureStatus` for `FactCandidate.disclosure_status` in `extract_facts.py` (or document
   why a local Literal stays separate from `FactRow`'s three-state type).
5. Update intelligence call sites, comparisons, and `model_copy` invocations.
6. Coordinate updates to `docs/API_CONTRACT.md` schema references and examples (preserving exact wire
   strings).
7. Add unit and contract tests in `tests/unit/test_schemas.py` and `tests/contract/` verifying:
   - Valid member acceptance and rejection of invalid/misspelled/wrong-case strings.
   - JSON serialization and round-trip integrity.
   - Exact `model_json_schema()` enum members.
   - Fixture deserialization.
   - Behavior tests confirming disclosure invariants, LLM extraction quotes, and publication gate
     rules remain intact.
