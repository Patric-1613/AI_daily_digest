"""LangGraph wiring for the per-item intelligence pipeline: Classify →
Extract → Compare → Draft → Validate, per docs/ARCHITECTURE.md's
intelligence workflow diagram. Every node below is a thin wrapper around
an already-built, already-tested function (resolve.py, resolve_llm.py,
facts.py, extract_facts.py, draft_claims.py, validate.py) — this file
only owns how state passes between them, never business logic of its
own. That ordering (nodes built and tested standalone first, graph
wiring last) is deliberate — see intelligence/CLAUDE.md's Orchestration
section.

Note on "Retrieve related history": the workflow diagram shows it as a
separate step, but here it's folded into classify (reads
`store.known_subjects()`) and compare (`FactStore.update_fact` reads the
stored prior value) rather than getting its own node — `FactStore` *is*
the retrieval mechanism, so a standalone "retrieve" node would just be
reading the same store a second time for nothing.

Scope: this graph processes ONE new SourceItem + its DocumentSnapshot at
a time, producing zero or more validated DigestClaims. Assembling a full
day's Digest from many items' claims, and the cross-subject
compare_subjects() step, are not wired into this graph yet — see
docs/LLM_AGENT_SPECS.md's "Not yet built" section.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from ai_daily_digest.intelligence.change_sets import get_or_create_change_set_id
from ai_daily_digest.intelligence.draft_claims import draft_change_claim
from ai_daily_digest.intelligence.extract_facts import FactExtractionResponse, extract_facts
from ai_daily_digest.intelligence.facts import FactStore, change_snapshot_ids
from ai_daily_digest.intelligence.resolve import (
    ResolutionResult,
    SubjectAlias,
    load_alias_table,
    resolve_deterministic,
)
from ai_daily_digest.intelligence.resolve_llm import ResolveLLMResponse, resolve_via_llm
from ai_daily_digest.intelligence.validate import validate_claim
from ai_daily_digest.shared.schemas import (
    Change,
    DigestClaim,
    DocumentSnapshot,
    ExtractedFact,
    SourceItem,
    Subject,
)


class PipelineState(TypedDict, total=False):
    item: SourceItem
    snapshot: DocumentSnapshot
    resolution: ResolutionResult
    subject: Subject | None
    facts: list[ExtractedFact]
    changes: list[Change]
    claims: list[DigestClaim]


def build_graph(  # pylint: disable=too-many-arguments,too-many-locals
    store: FactStore,
    change_set_ids: dict[Subject, uuid.UUID],
    *,
    batch_detected_at: datetime,
    alias_table: list[SubjectAlias] | None = None,
    resolve_llm_call_fn: Callable[[str, str], ResolveLLMResponse] | None = None,
    extract_call_fn: Callable[[str, str], FactExtractionResponse] | None = None,
) -> CompiledStateGraph[PipelineState, Any, PipelineState, PipelineState]:
    """batch_detected_at: the one caller-injected instant every `Change` this
    graph produces is stamped with as its `detected_at` (the `/v1/changes`
    sort key, ADR 0008 section 5.A). Required, keyword-only, no default — no
    graph node ever reads the wall clock; `run_daily()` normalizes it once and
    threads it here, and a direct `build_graph()` caller (a test) must supply
    it too. The `compare` node closes over it exactly the way it already
    closes over `store` and `change_set_ids`.

    store: the FactStore this pipeline reads from and writes to —
    shared across invocations so history accumulates run over run, the
    same way the real system will use one long-lived store (or, later,
    the real database via StoreLoader). change_set_ids: the batch-scoped
    ChangeSet-id allocator (ADR 0007's "Batch-scoped ChangeSet ID
    allocation") -- unlike `store`, this must be a FRESH dict per
    run_daily() call (daily_run.py's `_BatchAccumulator.change_set_ids`),
    never reused across runs, since a change_set_id is only valid for one
    batch. Closed over by the `compare` node below, the same way `store`
    already is. alias_table/*_call_fn are injectable, primarily for tests
    — see tests/unit/test_graph.py for running this without hitting the
    real Anthropic API.
    """
    resolved_alias_table = alias_table if alias_table is not None else load_alias_table()

    def classify_deterministic(state: PipelineState) -> PipelineState:
        """Entry node. Tries the cheap, auditable path first — candidates
        come from store.known_subjects(), i.e. what this store has
        already seen, plus the checked-in alias table."""
        item = state["item"]
        text = state["snapshot"].content_text or ""
        result = resolve_deterministic(
            item, store.known_subjects(), resolved_alias_table, item_text=text
        )
        if result.subject is not None:
            # Register as soon as a subject is resolved, not only
            # implicitly later via store.update_fact() -- an item that
            # resolves successfully but ends up with zero accepted facts
            # (everything the model reported failed a grounding check)
            # must still make this subject show up in known_subjects();
            # otherwise a real, seen subject silently disappears from
            # future resolution candidates. Idempotent, see
            # register_subject()'s own docstring.
            store.register_subject(result.subject)
        return {"resolution": result, "subject": result.subject}

    def classify_llm(state: PipelineState) -> PipelineState:
        """Only reached when classify_deterministic found true residue
        (method="no_match", see route_after_classify) — resolves against
        whichever candidates the deterministic pass narrowed it to."""
        prior = state["resolution"]
        result = resolve_via_llm(
            state["item"],
            prior.candidate_subjects,
            item_text=state["snapshot"].content_text or "",
            alias_table=resolved_alias_table,
            call_fn=resolve_llm_call_fn,
        )
        if result.subject is not None:
            store.register_subject(result.subject)  # see classify_deterministic's comment
        return {"resolution": result, "subject": result.subject}

    def route_after_classify(state: PipelineState) -> str:
        """The branch point before the LLM fallback: a clean "alias_match"
        skips straight to extraction; "no_match" needs the LLM to
        adjudicate; "ambiguous_multi_subject" (two or more tracked
        subjects both phrase-matched, e.g. Codex and ChatGPT) ends the run
        for this item right here instead — the LLM is never asked to pick
        one, since the schema has no way to represent "both" and picking
        either would be a false merge (see resolve.py's module
        docstring)."""
        method = state["resolution"].method
        if method == "alias_match":
            return "extract"
        if method == "ambiguous_multi_subject":
            return "end"
        return "classify_llm"

    def route_after_llm(state: PipelineState) -> str:
        """If even the LLM fallback couldn't resolve a subject, there is
        nothing further this item can contribute — end the run for it
        here rather than letting extract/compare/draft run on subject=None."""
        return "extract" if state["subject"] is not None else "end"

    def extract(state: PipelineState) -> PipelineState:
        """Runs the LLM fact extractor against this item's snapshot text.
        The RuntimeError below should be unreachable — route_after_llm
        guarantees a resolved subject before this node runs — but fails
        loudly instead of silently extracting nothing if that guarantee
        is ever broken by a future edit."""
        subject = state["subject"]
        if subject is None:
            raise RuntimeError("extract reached with no resolved subject — routing bug")
        facts = extract_facts(subject, state["snapshot"], call_fn=extract_call_fn)
        return {"facts": facts}

    def compare(state: PipelineState) -> PipelineState:
        """Deterministic — no LLM call. Feeds each extracted fact through
        FactStore.update_fact(), which is where "did this actually
        change" gets decided; only real changes are collected here."""
        subject = state["subject"]
        if subject is None:
            raise RuntimeError("compare reached with no resolved subject — routing bug")
        snapshot = state["snapshot"]
        changes: list[Change] = []
        for fact in state.get("facts", []):
            change = store.update_fact(
                subject,
                fact,
                source_url=str(state["item"].canonical_url),
                observed_at=snapshot.fetched_at,
                # The run's one injected detection time -- every Change from
                # this batch is stamped with exactly this instant. Never the
                # wall clock (ADR 0008 section 5.A).
                detected_at=batch_detected_at,
                # Lazy -- only actually invoked by update_fact() if this
                # fact turns into a real Change (ADR 0007). Late-binds
                # `subject` via the closure, which is correct here since
                # `subject` doesn't change again before this call resolves.
                change_set_id_factory=lambda: get_or_create_change_set_id(change_set_ids, subject),
                # Without this, update_fact()'s confidence=1.0 default
                # silently applied to every real Change regardless of
                # the actual extraction confidence -- fact.confidence is
                # None only for deterministic facts (see ExtractedFact's
                # docstring), which never reach this LLM-extraction node.
                confidence=fact.confidence if fact.confidence is not None else 1.0,
            )
            if change is not None:
                changes.append(change)
        return {"changes": changes}

    def draft(state: PipelineState) -> PipelineState:
        """Deterministic — no LLM call. One DigestClaim per Change; a run
        with no changes produces no claims, which is correct, not empty
        output to worry about."""
        claims = [draft_change_claim(change) for change in state.get("changes", [])]
        return {"claims": claims}

    def validate(state: PipelineState) -> PipelineState:
        """Deterministic — no LLM call. Be clear about what this step
        actually proves at the per-item level: draft() only ever builds
        a claim's citations FROM the same Change objects this function
        derives known_snapshot_ids from, so by construction every claim
        here always validates -- this node cannot currently catch a
        genuinely bad citation. It exists as (a) a defensive check that
        starts pulling its weight the moment draft_claims.py or this
        node's wiring changes, and (b) so every claim leaving this graph
        already carries a validation_status, matching the shape
        assemble_digest()/publish_digest() expect. The validation that
        actually matters runs there, in daily_run.py, against the real
        accumulated known_snapshot_ids spanning every run — not here."""
        known_snapshot_ids = {state["snapshot"].id}
        for change in state.get("changes", []):
            for snapshot_id in change_snapshot_ids(change):
                if snapshot_id:
                    known_snapshot_ids.add(snapshot_id)
        validated = [validate_claim(c, known_snapshot_ids) for c in state.get("claims", [])]
        return {"claims": validated}

    graph = StateGraph(PipelineState)
    graph.add_node("classify_deterministic", classify_deterministic)
    graph.add_node("classify_llm", classify_llm)
    graph.add_node("extract", extract)
    graph.add_node("compare", compare)
    graph.add_node("draft", draft)
    graph.add_node("validate", validate)

    graph.set_entry_point("classify_deterministic")
    graph.add_conditional_edges(
        "classify_deterministic",
        route_after_classify,
        {"extract": "extract", "classify_llm": "classify_llm", "end": END},
    )
    graph.add_conditional_edges("classify_llm", route_after_llm, {"extract": "extract", "end": END})
    graph.add_edge("extract", "compare")
    graph.add_edge("compare", "draft")
    graph.add_edge("draft", "validate")
    graph.add_edge("validate", END)

    return graph.compile()
