# 0005 — Structured cross-subject comparisons and typed snapshot content resolution

Status: Accepted. Authored by Person B; accepted by Person A on 2026-08-28 and confirmed by
Person C on 2026-09-04.

Implementation was intentionally excluded from the original PR #5 scope and later completed under
[issue #6](https://github.com/Patric-1613/Virtusa_CaPStone/issues/6), beginning with commit
`c64e282`. The typed `SnapshotResolver`, structured comparison assertions, deterministic rendering,
and fail-closed publication behavior described below are present on `main`.
Date: 2026-08-27

## Context

The intelligence pipeline's cross-subject comparison step (`compare_subjects.py`) lets an LLM write
free-form prose for a comparison claim, with code checking only that its citations are real and
that any numbers *mentioned in the prose* match real stored values. Two independent adversarial
reviews found this insufficient in ways a numeric-only check structurally cannot catch:

- A claim with no numeric assertion at all ("OpenAI is cheaper than Anthropic") has nothing for the
  numeric check to verify, true or false.
- A claim can state two real numbers attributed to the wrong subject (swapped), and the check —
  which only confirms both numbers are *somewhere* among the real values — can't detect the swap.
- Separately, `validate.py`'s content-grounding check and `extract_facts.py`'s value extraction had
  related gaps: unverifiable historical citations were being trusted rather than treated as
  unproven, and a value could be extracted from a quote shared with a different field's value with
  no attribution check.

This affects more than `intelligence/`: the fix touches the boundary intelligence needs into
snapshot content (today served by an ad hoc dict; the real version eventually backed by ingestion's
storage), and the shape of `DigestClaim` that delivery and any future API consumers read.

**Scope note**: the "unknown" vs. "not disclosed" value-semantics question originally included here
has been split into [ADR 0006](0006-disclosure-status-semantics.md) — not required for Phase 1
(`context_window_tokens`-only comparison), and kept out to avoid expanding this ADR's surface
unnecessarily.

## Decision

Adopt the design in
[`docs/DESIGN_PROPOSAL_comparison_and_grounding.md`](../DESIGN_PROPOSAL_comparison_and_grounding.md),
with the following clarifications required before this ADR can be marked Accepted:

1. **Comparisons stop being LLM-authored prose.** The model proposes structured
   `(subject_a, subject_b, field)` triples to compare; code looks up real values and renders the
   claim text deterministically — the same pattern `draft_claims.py` already uses for single-subject
   changes.
   - Same-subject comparisons (`subject_a == subject_b`) are rejected.
   - Reversed-pair duplicates are deduplicated, keyed by
     **`(sorted(subject_a, subject_b) by (company, product), field)`** — the field is part of the
     key. `(A, B, context_window_tokens)` and `(A, B, input_price_usd)` are two distinct
     comparisons and must never collapse into one just because the subject pair matches.
2. **Comparison rules are field-specific, not a numeric/text split.** Each comparable field needs
   its own defined representation (currency/unit/basis for prices, benchmark name/conditions for
   scores, set semantics for regions/modalities). **Phase 1 of this decision scopes comparison to
   `context_window_tokens` only** — the one field already unambiguous as a bare string. Every other
   field is excluded from comparison until its own representation is designed, each via a follow-up
   ADR.
   - **Malformed stored values fail per-candidate, not per-batch**: if a stored
     `context_window_tokens` value can't be parsed as an integer, the comparison rule drops and
     logs only that one candidate — it must never abort the rest of the comparison pass (mirrors
     the existing per-item/per-comparison broad-exception handling already in `daily_run.py`).
3. **Snapshot content is resolved through a typed `SnapshotResolver` interface**, not a raw,
   caller-owned, ever-growing dict.
   - **Lives in `shared/`, not `intelligence/`.** A protocol that ingestion is ever expected to
     implement or provide an instance of cannot live inside an intelligence-private module —
     ingestion must not import an intelligence-owned type. Proposed location:
     `shared/snapshot_resolver.py`, alongside `shared/schemas.py`. Mirrors the existing
     `Loader`/`FixtureLoader`/`StoreLoader` split in `intelligence/loaders.py`, but the interface
     itself is a shared contract, not an intelligence-internal abstraction.
   - **Synchronous, Phase 1 only.** This ADR defines a synchronous `get_content(snapshot_id) ->
     DocumentSnapshot | None` interface. A future database-backed implementation may need to be
     asynchronous; that is explicitly a separate design decision, not assumed or precluded here.
   - **The final publish gate must require a real resolver — no `None` fallback.** The version of
     this proposal reviewed previously allowed `snapshot_resolver: SnapshotResolver | None = None`
     everywhere, defaulting to existence-only checking. That is corrected: at the actual final gate
     (`publish_digest`/`validate_digest`'s call from `daily_run.py`), the resolver parameter becomes
     **required**, not optional — passing nothing must not silently degrade to trusting citation
     existence. The one narrow exception: `graph.py`'s per-item `validate` node, which — per its own
     existing docstring — never actually authorizes publication (that happens only at the batch-level
     final gate) may still run without a resolver, because it cannot grant "supported" status that
     matters regardless. A resolver-less per-item check is safe specifically because it isn't the
     thing that decides whether something ships.
4. **A citation whose content can't be resolved is never treated as proof of support.** Existence of
   a snapshot id is not evidence its content backs a claim; an unresolvable citation routes the
   claim to review, not "supported." (Already implemented, independent of this ADR's approval.)
5. **Multi-number attribution ambiguity in extraction is deferred, conditionally — and its limits
   are documented, not assumed away.** A full fix (character-offset citations) is out of scope for
   this ADR. Deferring it is only acceptable because a fact whose evidence quote is shared with a
   different field's value is dropped outright rather than guessed at (already implemented,
   independent of this ADR). That guard's real limits, now documented in
   `extract_facts.py::_cross_contaminated_indices`'s own docstring:
   - It only catches the mix-up when **both** the correct and the confused candidate appear in the
     **same** extraction response. If the model returns a wrong value while omitting the sibling
     fact that would have exposed it, nothing catches that.
   - It can false-positive on **coincidentally** shared digit sequences between unrelated fields —
     a safe direction to be wrong in (a valid fact gets dropped, not a false one published), but a
     real, non-zero cost, not free caution.
   - This is **containment**, not a complete attribution fix, and must be described that way
     wherever referenced, not implied to be a solved problem.
6. **First-ever disclosures remain a separate, undecided product question.** Not built here. If
   adopted later, scoped via an explicit allowlist, with neutral wording ("first recorded
   observation") that doesn't imply a prior state that never existed.

## Consequences

- Cross-subject comparison claims become verifiable by construction for the field they cover,
  closing the "cheaper than" and swapped-value fabrication classes at the root rather than trying to
  detect them after the fact in prose.
- Initial comparison coverage is deliberately narrow (`context_window_tokens` only) — prices,
  benchmarks, regions, modalities, and licence terms are NOT compared until their own representation
  is designed, each via its own follow-up ADR. This is a real capability reduction from what a
  free-text `compare_subjects` could attempt (however unreliably) — accepted as the cost of
  correctness.
- `SnapshotResolver` lives in `shared/`, making it a real cross-module contract from day one, not an
  intelligence-internal type that later has to be relocated when ingestion needs to implement it.
- The final publish gate's dependency on a real resolver (not optional) means `daily_run.py`'s
  signature changes to require one — every caller and test double needs an actual (even if
  in-memory) `SnapshotResolver` instance, not `None`.
- Multi-day/historical claims will need human review more often until a real `SnapshotResolver` can
  retrieve content beyond the current batch — an accepted, deliberate cost of the fail-closed
  default in item 4, not treated as a defect to route around.
- The character-offset citation fix for extraction attribution (item 5's real fix) is explicitly out
  of scope here and needs its own follow-up ADR before implementation.
- The "unknown" vs. "not disclosed" value semantics (ADR 0006) and "new disclosure claims" (item 6)
  both stay open, decided separately from this ADR.

## MVP scoping decision (2026-08-28)

Person A accepted this ADR's design direction and, to avoid an open-ended design-review loop,
explicitly separated **design acceptance** from **implementation scope for PR #5**:

- This ADR is accepted as the agreed design for cross-subject comparison and snapshot resolution —
  pending Person C's confirmation, same as ADR 0004.
- **Its implementation is out of scope for PR #5.** Tracked in a separate follow-up issue/PR
  (linked here once created) — building it is a deliberate next step, not something PR #5 is
  claiming to have done.
- PR #5 ships as an intentionally scoped MVP: the interim safety nets this ADR's Context section
  describes stay exactly as they are — `daily_run.py::_never_auto_publish_comparisons()` unchanged,
  content-grounding still fails closed on unresolvable citations. Reduced comparison automation
  (every cross-company comparison still requires human review) is an **accepted, disclosed,
  temporary limitation** of this PR, not an undisclosed defect.
- Further work on this design happens through the follow-up issue/ADR process, not by continuing to
  expand PR #5.

This ADR still requires Person C's review before being considered fully accepted by the team, per
the team's contract-change process (`docs/API_CONTRACT.md`).

## Phase 2: price comparison rules (2026-09-02)

Point 2's Phase 1 scope (`context_window_tokens` only) has been extended to
`input_price_usd`/`output_price_usd`, per direct review instruction — implemented ahead of a
dedicated follow-up ADR for price representation, which point (f) originally anticipated each
comparable field would get. Recorded here as the same kind of disclosed, deliberate scope
decision this ADR's other rounds have been, not a silent expansion:

- `shared/attributes.py::PriceComparisonRule` — a plain USD numeric string (optional leading `$`,
  thousands separators tolerated, mirroring the formatting tolerance `grounding.py`'s own
  number-matching already extends to a disclosed value elsewhere in this codebase). Registered for
  both `input_price_usd` and `output_price_usd` in `COMPARISON_RULES`.
- **Explicitly still NOT handled**: currency conversion, and reconciling a basis mismatch (e.g. one
  side priced per-token, the other per-million-tokens) — this rule trusts the stored value's basis
  is already consistent for a given field across every subject being compared, the same trust
  `IntegerComparisonRule` already places in `context_window_tokens` always meaning the same unit.
  Getting this wrong would silently render a numerically-correct-looking but semantically-wrong
  comparison; not addressed here, and worth its own explicit design pass before it's relied on for
  real production content, not assumed safe by omission.
- `benchmark_scores`, `availability_regions`, `licence_terms`, `modalities` remain excluded from
  comparison, unchanged by this round.
