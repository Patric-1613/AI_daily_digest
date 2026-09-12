# intelligence — Person B

Owns: fact retrieval/comparison state (`facts.py`), subject resolution,
fact extraction, deterministic claim drafting, citation validation, and the
evaluation harness (`evaluate.py` — computes citation validity,
unsupported claim count, duplicate rate, and change recall over frozen
evaluation packs and digest runs).
See `README.md` in this directory for the one-line upstream
description, and `docs/API_CONTRACT.md` for the real shared contract —
there is no "Entity" model; this module resolves to `Subject`
(company + product) and produces `Change`/`ExtractedFact`/`DigestClaim`.

Read the root `AGENTS.md` and `CLAUDE.md` first for repo-wide rules. This
file is the local rules for everything under
`src/ai_daily_digest/intelligence/` and `tests/{unit,contract}/` paths
that cover it.

## Non-negotiable rules

- **Never let a model infer a fact value it can't see in context.** An
  absent field is simply omitted — never a null placeholder guessed from
  general knowledge.
- **Every `DigestClaim` needs a citation resolvable to a real snapshot
  id.** Enforce this in code (`validate.py`), never rely on the prompt
  alone to behave. Existence alone is not enough — when the caller has a
  `SnapshotResolver` (`shared/snapshot_resolver.py`, ADR 0005),
  `validate.py` also checks that every number the claim asserts is
  actually grounded in the content of the snapshot(s) it cites
  (`intelligence/grounding.py`). The final publish gate
  (`validate_digest`/`publish_digest`) requires a real resolver — no
  `None` fallback to existence-only checking; `graph.py`'s per-item node
  is the one exception, since it never authorizes publication itself.
- **`quoted_span` must actually appear in the source text, AND the
  extracted `value` must actually be supported by that quote** — both
  checked in code (`extract_facts.py`); a fabricated span or a fabricated
  value hiding behind a real quote are both silently dropped, not
  silently stored. Accepted facts keep their `quoted_span`/`confidence`
  (ADR 0004) rather than discarding them.
- **A false subject merge is worse than a miss.** Assert on it explicitly
  in `tests/unit/test_resolve.py`; low-confidence LLM resolutions are
  never auto-merged (`resolve_llm.py`'s 0.6 threshold) — and neither is a
  high-confidence resolution naming a company/product that isn't
  actually one of the candidates it was given.
- Change detection compares against **stored history**, never a
  recomputed value — `facts.py::FactStore` builds `Change.previous` from
  what was actually recorded, and `field_history()` is append-only.
- A `Digest` can only reach `status="published"` via `validate.py::publish_digest`,
  and only when every claim is supported — never set that status by hand
  elsewhere.

## Model choices

No call site sets `temperature` — sampling controls (`temperature`/`top_p`/
`top_k`) are removed on the current-gen models below (400 if sent); see
`intelligence/llm.py::call_structured`'s docstring. Reproducibility comes
from the schema-validation retry loop and each call site's own grounding
checks, not from a sampling knob.

| Call site | Model | Notes |
|---|---|---|
| `resolve_llm.py` | Haiku 4.5 | Only runs on the residue after deterministic alias matching fails/is ambiguous. Constrained JSON. Confidence < 0.6, or a proposed subject not in the candidate list, → logged for review, never auto-merged. |
| `extract_facts.py` | Sonnet 5 | Must quote an exact source span, not paraphrase — Haiku tends to paraphrase, which breaks the grounding check. |
| `compare_subjects.py` | Sonnet 5 | Reads the fact table only, never raw article text, and proposes structured `(subject_a, subject_b, field)` triples only — never prose or numbers (ADR 0005). Code alone looks up values and renders the claim; same-subject/unknown-subject/non-comparable-field/undisclosed-value/malformed-value candidates are all dropped in code, never guessed at. |

`facts.py`'s change detection and `draft_claims.py`'s single-field claim
drafting are deliberately **not** LLM calls — see
`docs/ARCHITECTURE.md`: "Prefer deterministic code ... use an LLM only
where deterministic rules are insufficient."

All LLM call sites go through `intelligence/llm.py::call_structured` —
one wrapper, so retries, logging, and model swaps happen in one place.
Don't call the Anthropic SDK directly from elsewhere in `intelligence/`.

## Orchestration

Per-item flow (classify → extract → compare → draft → validate) is a
**LangGraph** `StateGraph` in `intelligence/graph.py::build_graph`, per
`docs/ARCHITECTURE.md`'s technology baseline (still status: Proposed) —
each stage a node, state passed explicitly, every node that calls the
model still routing through `call_structured`. The individual functions
(`resolve_deterministic`, `resolve_via_llm`, `FactStore.update_fact`,
`extract_facts`, `draft_change_claim`, `compare_subjects`,
`validate_digest`/`publish_digest`) were built and tested standalone
first — nodes before the graph shell, so graph wiring only combined
already-proven logic instead of debugging both at once. See
`docs/LLM_AGENT_SPECS.md`'s Orchestration section for the exact node/edge
layout. `compare_subjects` is not a node in this graph — it's a
cross-item, cross-subject step, a different shape from the current
one-item-at-a-time graph — but it is wired into the batch-level
orchestrator, `daily_run.py::run_daily`, which invokes this graph once
per item and then runs one `compare_subjects` pass over the batch. See
`docs/LLM_AGENT_SPECS.md`'s `daily_run` section.

## Structured output discipline

Request JSON conforming to the relevant response model → validate → on
failure, retry once with the validation error appended to the prompt →
on second failure, fail loudly (`StructuredCallFailedError`). No silent
fallback to prose, ever.

## Prompts

Live in `intelligence/prompts/*.txt`, referenced by filename (not inlined
as strings) so prompt changes show up cleanly in diffs and in eval logs.
Agent-level specs (input/output contract, guardrails, failure mode) for
each call site are in `docs/LLM_AGENT_SPECS.md` — update both together.

## Testing

- `tests/fixtures/contracts/` is the real fixture pack location (per
  `docs/TEAM_WORKFLOW.md`/`docs/ARCHITECTURE.md` Milestone 0) — currently
  a small solo draft, not the team's ratified 20+/malformed/prompt-
  injection pack; see its own README before treating it as canonical.
  Never hand-edit it to make a test pass once it's the real thing — fix
  the code or prompt instead.
- Contract tests in `tests/contract/` (marked `pytest.mark.contract`)
  protect the shared models — anything that changes `shared/schemas.py`
  must keep them passing.
- LLM call sites (`resolve_llm.py`, `extract_facts.py`) take an
  injectable `call_fn` specifically so tests don't need a real API key or
  network call — use that pattern for new call sites too.
- `intelligence/llm.py` logs model, attempt number, prompt length, and a
  content fingerprint (hash) for every call — **never the raw prompt
  itself**, per repo-root AGENTS.md's "never log raw prompts" rule
  (collected page content flows into prompts, so logging them verbatim
  would leak scraped content into log retention). If you need to inspect
  an actual prompt while debugging, reproduce the call locally with the
  same inputs rather than reading it back out of logs.
