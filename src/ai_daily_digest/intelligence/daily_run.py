"""Daily-run orchestrator — the piece that threads the per-item graph
(`graph.py`), cross-subject comparison (`compare_subjects.py`), and
digest assembly (`assemble_digest.py`) together into one real run over a
batch of new items. Not itself a LangGraph node or graph: it's the loop
that invokes the (already-compiled) per-item graph once per item, then a
single cross-subject comparison pass, then assembles everything into one
Digest.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from langgraph.graph.state import CompiledStateGraph

from ai_daily_digest.intelligence.assemble_digest import assemble_digest
from ai_daily_digest.intelligence.change_sets import build_change_sets
from ai_daily_digest.intelligence.compare_subjects import (
    ComparisonResponse,
    build_fact_table,
    compare_subjects,
)
from ai_daily_digest.intelligence.extract_facts import FactExtractionResponse
from ai_daily_digest.intelligence.facts import FactStore
from ai_daily_digest.intelligence.graph import PipelineState, build_graph
from ai_daily_digest.intelligence.resolve import SubjectAlias
from ai_daily_digest.intelligence.resolve_llm import ResolveLLMResponse
from ai_daily_digest.shared.attributes import COMPARISON_RULES
from ai_daily_digest.shared.schemas import (
    Change,
    ChangeSet,
    Digest,
    DigestClaim,
    DigestStatus,
    DocumentSnapshot,
    SourceItem,
    Subject,
    normalize_ordering_timestamp,
)
from ai_daily_digest.shared.snapshot_resolver import SnapshotResolver

logger = logging.getLogger("intelligence.daily_run")


@dataclass
class BatchItem:
    item: SourceItem
    snapshot: DocumentSnapshot


@dataclass
class DailyRunResult:
    digest: Digest
    resolved_subjects: list[Subject] = field(default_factory=list)
    unresolved_item_ids: list[uuid.UUID] = field(default_factory=list)
    failed_item_ids: list[uuid.UUID] = field(default_factory=list)
    # ChangeSet aggregates built from this run's Changes (change_sets.py)
    # -- ready for whatever persistence layer picks them up next; nothing
    # downstream of run_daily() currently persists them, the same way
    # FixtureLoader/StoreLoader's load_change_sets() implies some other
    # layer owns storage, not intelligence.
    change_sets: list[ChangeSet] = field(default_factory=list)


@dataclass
class _BatchAccumulator:
    """Everything the per-item loop in run_daily() builds up, one place
    instead of seven separate local variables — what keeps run_daily()
    itself readable at a glance. See _process_item() for how one item's
    result is folded in."""

    claims: list[DigestClaim] = field(default_factory=list)
    all_changes: list[Change] = field(default_factory=list)
    resolved_subjects: list[Subject] = field(default_factory=list)
    # Subject is hashable (frozen, see shared/schemas.py) -- this set
    # mirrors resolved_subjects purely so membership checks below are
    # O(1) instead of a list scan repeated for every item in the batch.
    seen_subjects: set[Subject] = field(default_factory=set)
    unresolved_item_ids: list[uuid.UUID] = field(default_factory=list)
    failed_item_ids: list[uuid.UUID] = field(default_factory=list)
    # Batch-scoped ChangeSet-id allocator (ADR 0007) -- owns "which
    # change_set_id has this subject already been assigned in THIS
    # batch", via change_sets.py::get_or_create_change_set_id(). Must be
    # a fresh dict every run_daily() call (never reused across days, and
    # never owned by FactStore, which persists across runs by design) --
    # _BatchAccumulator already has exactly that lifetime, since run_daily()
    # constructs one fresh instance per call.
    change_set_ids: dict[Subject, uuid.UUID] = field(default_factory=dict)


def _never_auto_publish_comparisons(digest: Digest, comparison_claim_ids: set[uuid.UUID]) -> Digest:
    """INTERIM SAFETY POLICY, kept in force by ADR 0005 (docs/adr/0005-
    structured-comparison-and-snapshot-resolution.md): no cross-subject
    comparison claim may cause a digest to auto-publish, regardless of
    its own validation status -- it can still be part of a "review"
    digest, same as any other unsupported claim, but never nudge a
    digest into "published" on its own. Mirrors validate.py's own rule
    that a check can only ever veto a publish, never grant one.

    Pre-ADR-0005, this existed because compare_subjects()'s numeric
    grounding check only caught a claim stating a specific WRONG number
    -- it did nothing for a qualitative/relational claim like "OpenAI is
    cheaper than Anthropic" (no number to check at all), or a claim that
    swapped which number belonged to which subject (both numbers real,
    just attributed backwards).

    ADR 0005's structured `ComparisonAssertion` + deterministic rendering
    (compare_subjects.py) closes both of those fabrication classes at the
    root: the model can no longer author a value, a relation, or any
    prose at all, so a swapped or invented comparison can no longer reach
    a claim's text in the first place. That does NOT retire this
    function. ADR 0005 explicitly keeps comparison auto-publish disabled
    regardless -- deterministic rendering removes the specific failure
    modes this policy was originally written for, but comparison output
    has not yet been proven correct in practice at scale, and relaxing
    this guardrail is a deliberate, separate decision for a future review
    step, not an automatic consequence of this refactor. Every
    cross-subject comparison still requires human review until that
    decision is made."""
    if comparison_claim_ids and digest.status == DigestStatus.PUBLISHED:
        return digest.model_copy(update={"status": DigestStatus.REVIEW})
    return digest


def _process_item(
    entry: BatchItem,
    graph: CompiledStateGraph[PipelineState, Any, PipelineState, PipelineState],
    known_snapshot_ids: set[uuid.UUID],
    snapshot_resolver: SnapshotResolver,
    acc: _BatchAccumulator,
) -> None:
    """Runs the per-item graph for one BatchItem and folds its result
    into `acc`. A raising graph.invoke() (a transient model failure, a
    malformed response that exhausts call_structured's retry, ...) is
    caught deliberately broadly: whatever failed, this one item's
    failure must not silently discard the whole batch's already-computed
    claims -- recorded in acc.failed_item_ids and logged with a
    traceback instead, so it's still diagnosable, not swallowed.

    `snapshot_resolver` is caller-owned (see run_daily()'s docstring) --
    registering this item's snapshot into it is best-effort: the
    SnapshotResolver Protocol only guarantees get_content(), not add().
    A real, persistent-store-backed resolver may already have this
    snapshot's content (or manage its own ingestion path entirely) and
    have no add() at all -- only register when the resolver actually
    supports it. Registration happens INSIDE the try block below,
    deliberately: InMemorySnapshotResolver.add() raises ValueError on a
    conflicting snapshot id (see shared/snapshot_resolver.py), and that
    must be caught the same broad way as any other per-item failure --
    one item with a bad/conflicting snapshot must not crash the whole
    batch any more than a transient model failure would."""
    try:
        known_snapshot_ids.add(entry.snapshot.id)
        if hasattr(snapshot_resolver, "add"):
            snapshot_resolver.add(entry.snapshot)
        result = graph.invoke({"item": entry.item, "snapshot": entry.snapshot})
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("daily_run_item_failed item_id=%s", entry.item.id)
        acc.failed_item_ids.append(entry.item.id)
        return

    subject = result.get("subject")
    if subject is None:
        acc.unresolved_item_ids.append(entry.item.id)
        return
    if subject not in acc.seen_subjects:
        acc.seen_subjects.add(subject)
        acc.resolved_subjects.append(subject)
    acc.claims.extend(result.get("claims", []))
    acc.all_changes.extend(result.get("changes", []))


def run_daily(  # pylint: disable=too-many-arguments,too-many-locals
    # 4 required positional (the batch to run and where to accumulate
    # state) + 1 required keyword-only (snapshot_resolver) + 5 optional
    # keyword-only DI/config params -- this is the orchestrator's public
    # entry point, and the *_call_fn params exist specifically so every
    # LLM call site downstream stays independently testable (see this
    # file's other tests' pattern); collapsing them into one options
    # object would just move the same surface area, not reduce it. The
    # local-variable count pylint also flags here is dominated by these
    # same 10 parameters (they count toward it too), not by hidden
    # complexity in the function body -- see _process_item/_BatchAccumulator
    # just above for where the actual per-item logic was already
    # extracted out to keep this function itself short.
    store: FactStore,
    known_snapshot_ids: set[uuid.UUID],
    batch: list[BatchItem],
    digest_date: date,
    *,
    snapshot_resolver: SnapshotResolver,
    batch_detected_at: datetime,
    comparison_fields: list[str] | None = None,
    alias_table: list[SubjectAlias] | None = None,
    resolve_llm_call_fn: Callable[[str, str], ResolveLLMResponse] | None = None,
    extract_call_fn: Callable[[str, str], FactExtractionResponse] | None = None,
    compare_call_fn: Callable[[str, str], ComparisonResponse] | None = None,
    title: str | None = None,
) -> DailyRunResult:
    """Runs today's batch through the per-item graph (one `graph.invoke`
    per item, sharing one compiled graph and one `store`), then a single
    cross-subject comparison pass over whichever subjects the batch
    actually touched, then assembles everything into one Digest.

    `store`, `known_snapshot_ids`, and `snapshot_resolver` are all
    mutated / owned across calls the same way — the caller threads the
    same objects into tomorrow's run so history, citation validity, and
    resolvable snapshot content all carry over. `snapshot_resolver` is
    REQUIRED and caller-supplied, deliberately not built internally here:
    a resolver's usefulness comes entirely from what it can resolve
    beyond just this batch (e.g. a real ingestion-store-backed resolver
    spanning many days), so run_daily() must never quietly construct its
    own batch-scoped one and discard the caller's broader view. Pass
    `InMemorySnapshotResolver()` for a batch-scoped resolver equivalent
    to this function's old internal behavior. Each processed item is
    registered into it via `.add()` when the resolver supports that (see
    `_process_item()`) — a resolver Protocol only guarantees
    `get_content()`, so a resolver with no `.add()` (e.g. one backed
    entirely by ingestion's own storage) is left to manage its own
    contents.

    An item that fails to resolve (even after the LLM fallback) is
    recorded in `unresolved_item_ids`, not silently dropped. An item
    whose processing *raises* (a transient model failure, a malformed
    response that exhausts `call_structured`'s retry, ...) is recorded in
    `failed_item_ids` and the rest of the batch still runs — one item's
    failure must not cost every other item's already-computed claims for
    the day, the same principle ingestion applies to its own sources.

    Every Change produced by the batch is also grouped into ChangeSet
    aggregates (see change_sets.py) and returned on the result. Each
    Change's `change_set_id` is allocated lazily and batch-scoped (ADR
    0007's "Batch-scoped ChangeSet ID allocation"): `acc` (below) owns
    the allocator, threaded into `build_graph()` so its `compare` node
    can request one per subject on first use within this run only.

    `batch_detected_at` is REQUIRED and caller-injected: the one instant every
    `Change` this run produces is stamped with as its `detected_at` (the
    `/v1/changes` sort key, ADR 0008 section 5.A). run_daily() validates and
    UTC-normalizes it once, up front -- a timezone-naive value fails the whole
    run before any item is processed -- then threads the normalized value into
    build_graph() and thus into every FactStore.update_fact() call. Nothing in
    this path reads the wall clock; pass an explicit value in tests.

    INTERIM SAFETY POLICY: no cross-subject comparison claim (from
    compare_subjects()) may cause the digest to auto-publish, regardless
    of its own validation status -- see _never_auto_publish_comparisons().
    """
    # Fail fast on a bad batch detection time -- before building the graph or
    # touching a single item (ADR 0008 section 5.A). Downstream (build_graph,
    # update_fact, Change) all get this already-canonical UTC value.
    normalized_batch_detected_at = normalize_ordering_timestamp(batch_detected_at)
    # _BatchAccumulator (and its change_set_ids allocator) must exist
    # BEFORE build_graph(), which closes over acc.change_set_ids the same
    # way it already closes over `store` -- the graph cannot be built
    # without something for its `compare` node to close over.
    acc = _BatchAccumulator()
    graph = build_graph(
        store,
        acc.change_set_ids,
        batch_detected_at=normalized_batch_detected_at,
        alias_table=alias_table,
        resolve_llm_call_fn=resolve_llm_call_fn,
        extract_call_fn=extract_call_fn,
    )

    for entry in batch:
        _process_item(entry, graph, known_snapshot_ids, snapshot_resolver, acc)

    comparison_claim_ids: set[uuid.UUID] = set()
    # Default to only the fields with a registered ComparisonRule (ADR
    # 0005 point 2 -- context_window_tokens, input_price_usd,
    # output_price_usd) -- there is no point asking the model to
    # consider a field compare_subjects() can never resolve to a claim
    # regardless of what it proposes.
    fields = comparison_fields if comparison_fields is not None else list(COMPARISON_RULES)
    if fields and len(acc.resolved_subjects) >= 2:
        try:
            rows = build_fact_table(store, acc.resolved_subjects, fields)
            comparison_claims = compare_subjects(rows, call_fn=compare_call_fn)
            comparison_claim_ids = {c.id for c in comparison_claims}
            acc.claims.extend(comparison_claims)
        except Exception:  # pylint: disable=broad-exception-caught
            # Same principle as _process_item: a comparison failure
            # shouldn't cost every per-item claim already gathered above.
            logger.exception("daily_run_comparison_failed")

    digest = assemble_digest(
        digest_date,
        acc.claims,
        known_snapshot_ids=known_snapshot_ids,
        snapshot_resolver=snapshot_resolver,
        title=title,
    )
    digest = _never_auto_publish_comparisons(digest, comparison_claim_ids)

    return DailyRunResult(
        digest=digest,
        resolved_subjects=acc.resolved_subjects,
        unresolved_item_ids=acc.unresolved_item_ids,
        failed_item_ids=acc.failed_item_ids,
        change_sets=build_change_sets(acc.all_changes),
    )
