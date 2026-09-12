"""Shared data contract — mirrors docs/API_CONTRACT.md exactly. Any
divergence between this file and that document is a bug; whichever is
wrong, fix it in the same PR (see API_CONTRACT.md's "Contract-change
process"). docs/API_CONTRACT.md is currently "Draft v0.1 for agreement by
all three module owners" — treat this file the same way: not yet final.

Scope note: this file covers the models intelligence produces/consumes
(SourceItem, DocumentSnapshot, Change/ChangeSet, ExtractedFact,
Digest/DigestClaim). Source/CollectionRun (ingestion) and
Subscription/EmailDelivery (delivery) aren't reproduced here yet — add
them when a module actually needs the shared type, per shared/README.md's
"smallest stable set... required across modules", not preemptively.

Requires: pydantic>=2.11 (docs/adr/0007-uuid-v7-identifier-strategy.md --
the built-in UUID7 type that shared/ids.py::Uuid7Id re-exports ships in
2.11.0, not the wider pydantic>=2 this file used to require).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    TypeAdapter,
    field_validator,
    model_validator,
)

from ai_daily_digest.shared.ids import Uuid7Id

# A confidence score, everywhere one appears in this contract or in an LLM
# response model that feeds it (ExtractedFact.confidence, Change.confidence,
# and intelligence/extract_facts.py::FactCandidate,
# intelligence/resolve_llm.py::ResolveLLMResponse both import this rather
# than redeclaring `float`). bounded [0, 1] AND allow_inf_nan=False --
# ge/le alone already reject NaN (every comparison with NaN is False, so a
# bare `ge=0` constraint fails closed on it), but allow_inf_nan=False makes
# that rejection an explicit, intentional guarantee rather than a side
# effect of how IEEE754 comparisons happen to behave. Without this, a
# confidence=NaN response silently bypassed every "confidence < threshold"
# check in the codebase (NaN < 0.6 is also False) — verified as a real gap
# in review, not hypothetical.
Confidence = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


def _normalize_to_utc(value: datetime) -> datetime:
    """Shift an aware datetime to UTC. `astimezone` only changes the tzinfo
    offset, never the instant, so microseconds are preserved exactly."""
    return value.astimezone(UTC)


# The one validation/normalization boundary for a business-ordering timestamp --
# a pagination sort key (SourceItem.first_fetched_at, Change.detected_at). ADR
# 0008 section 5: reject timezone-naive datetimes (the service never guesses a
# zone), normalize aware values to UTC, preserve microseconds. `AwareDatetime`
# does the naive rejection; `AfterValidator(_normalize_to_utc)` does the UTC
# conversion. One definition so the model boundary here and the pre-UUID check
# in FactStore.update_fact()/run_daily() cannot drift.
OrderingTimestamp = Annotated[AwareDatetime, AfterValidator(_normalize_to_utc)]

_ORDERING_TIMESTAMP_ADAPTER: TypeAdapter[datetime] = TypeAdapter(OrderingTimestamp)


def normalize_ordering_timestamp(value: datetime) -> datetime:
    """Validate and normalize a business-ordering timestamp exactly the way the
    model boundary (`OrderingTimestamp`) does -- for callers that must check it
    BEFORE allocating a UUID (`FactStore.update_fact()`) or before any batch
    processing (`intelligence/daily_run.py::run_daily()`). Raises
    `pydantic.ValidationError` for a timezone-naive value; returns the
    UTC-normalized value otherwise (microseconds preserved)."""
    return _ORDERING_TIMESTAMP_ADAPTER.validate_python(value)


# ---------------------------------------------------------------------------
# Ingestion output, intelligence input
# ---------------------------------------------------------------------------


class SourceItem(BaseModel):
    """The normalized identity and metadata of a published item. Content
    lives separately in DocumentSnapshot — an item may have multiple
    snapshots over time; this record never holds body text itself.

    `id` and `first_fetched_at` are the `(business_sort_value, id)` ordering
    tuple `/v1/updates` paginates on (ADR 0008 section 5). Both are
    `Field(frozen=True)`: ordinary attribute reassignment raises. The model as
    a whole is NOT frozen -- `latest_snapshot_id`, `updated_at` and similar
    fields legitimately change as later snapshots arrive. Database-level
    enforcement (a `BEFORE UPDATE` trigger or restricted column-update
    privileges -- not a plain `CHECK` constraint) is deferred to the
    persistence/migration PR."""

    id: Uuid7Id = Field(frozen=True)
    dedupe_key: str  # sha256 of the normalized canonical_url; DB-unique
    source_id: str  # sources.yaml registry slug, e.g. "openai_news" -- a
    # human-readable configuration key, never converted to a UUID (ADR 0007).
    publisher: str
    title: str
    canonical_url: HttpUrl
    published_at: datetime | None = None
    updated_at: datetime | None = None
    # first_fetched_at: when the service first discovered this item -- the
    # `/v1/updates` sort key. `OrderingTimestamp` rejects a timezone-naive
    # value and normalizes an aware one to UTC (microseconds preserved).
    # `frozen=True`: ingestion must never rewrite it -- not on a re-fetch, a
    # content-hash change, a new DocumentSnapshot, or any correction. Distinct
    # from `published_at` (publisher-controlled, nullable) and `updated_at`,
    # whose semantics this PR does not touch (ADR 0008 section 5.C).
    first_fetched_at: OrderingTimestamp = Field(frozen=True)
    latest_snapshot_id: Uuid7Id | None = None
    # event_id: intentionally NOT Uuid7Id. It's a human-readable grouping
    # key today (e.g. "ev-gpt4o-256k" in the fixture pack), not a
    # generated resource id -- no Event model is persisted and nothing
    # constructs one via shared/ids.py::new_id(). Retype only alongside a
    # future ADR that defines a real, generated Event resource (ADR
    # 0007's Consequences section).
    event_id: str | None = None  # nullable until items are grouped by event
    authors: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    language: str = "en"


class DocumentSnapshot(BaseModel):
    """One immutable version of fetched content -- a raw source snapshot
    is never mutated, only superseded by a new one (AGENTS.md: "Raw
    source snapshots are immutable"). `model_config = ConfigDict(frozen=True)`
    enforces that at the Python object level (`docs/adr/0002-postgres-pgvector.md`
    section 9/16); PostgreSQL's own `BEFORE UPDATE`/`DELETE`/`TRUNCATE`
    triggers enforce it at the storage layer once persistence exists.
    `content_text` is required -- every *stored* snapshot has content; a
    delivery list projection that omits the body is a separate, narrower
    response schema under `delivery/api/`, not this domain model."""

    model_config = ConfigDict(frozen=True)

    id: Uuid7Id
    source_item_id: Uuid7Id
    # fetched_at: the value ADR 0002 section 13 compares (alongside `id`)
    # to decide which of two concurrently-arriving snapshots is newest
    # when advancing a source item's latest-snapshot pointer -- a
    # business-ordering timestamp exactly like `SourceItem.first_fetched_at`,
    # just not one `/v1/updates` itself paginates on. Reuses
    # `OrderingTimestamp` rather than a second, near-identical Annotated
    # alias with the same rule set (reject naive datetimes, normalize
    # aware ones to UTC, preserve microseconds) -- one definition, so
    # this and the pagination sort key cannot drift apart.
    fetched_at: OrderingTimestamp
    content_hash: str  # sha256 content hash -- a hash, never a UUID (ADR 0007).
    content_text: str
    raw_location: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    collector_version: str | None = None


# ---------------------------------------------------------------------------
# Intelligence-internal identity, not a standalone API resource
# ---------------------------------------------------------------------------


class Subject(BaseModel):
    """What a Change/ChangeSet is about. Embedded, not a top-level
    resource with its own opaque id — intelligence resolves a SourceItem
    to a Subject (see intelligence/resolve.py); there is no public
    "Entity" endpoint in docs/API_CONTRACT.md.

    frozen=True: a pure immutable value object, and hashable so callers
    can put Subjects in sets/dict keys (e.g. resolve.py's candidate
    dedup) without a workaround."""

    model_config = ConfigDict(frozen=True)

    company: str
    product: str


# ---------------------------------------------------------------------------
# Fact extraction (intelligence)
# ---------------------------------------------------------------------------


class DisclosureStatus(StrEnum):
    """ADR 0006's two persisted disclosure states -- owned by ExtractedFact.
    Deliberately does NOT include "unknown": that's a FactRow-only,
    intelligence-local concept (compare_subjects.py) meaning "no
    ExtractedFact exists at all", never a state a real extracted fact can
    itself be in. See ADR 0009 for why this stays a closed, two-member
    Enum rather than the three-value Literal FactRow uses."""

    DISCLOSED = "disclosed"
    NOT_DISCLOSED = "not_disclosed"


class ExtractionMethod(StrEnum):
    """How an ExtractedFact's value was produced -- owned by ExtractedFact.
    A closed set by construction: every fact in this codebase is built
    either deterministically or via one structured LLM extraction path
    (intelligence/extract_facts.py); a third method would be a real,
    reviewed addition to this Enum, not an open string (ADR 0009)."""

    DETERMINISTIC = "deterministic"
    LLM_STRUCTURED_OUTPUT = "llm_structured_output"


class ExtractedFact(BaseModel):
    """One field-level fact extracted from a single snapshot. Facts
    created by deterministic code use extraction_method="deterministic";
    facts created by an LLM must also record extraction_model and
    prompt_version so evaluations are reproducible.

    quoted_span/confidence: the evidence a fact was built from, kept
    (not discarded) so it can be audited later -- did this fact's value
    actually come from the text it claims, and how confident was the
    extraction. Optional because deterministic facts don't always have a
    natural "quote" to attach; LLM-extracted facts always populate both
    (see intelligence/extract_facts.py) and this is now enforced here,
    not just by extraction code and contract tests, per ADR 0004's
    accepted clarification -- see _require_evidence_for_llm_facts below.
    Added by docs/adr/0004-extracted-fact-keeps-evidence.md.

    disclosure_status/value: ADR 0006's "unknown" vs. "not disclosed" are
    different claims. `value=None` here means "the source explicitly
    states this is being withheld" (disclosure_status="not_disclosed"),
    itself a groundable claim needing its own citation -- NOT "no
    extraction ever found a value", which is represented by no
    ExtractedFact existing at all, never by one with a null value. `value`
    has no default (unlike the truly optional fields below) precisely so
    every caller must decide and state which of those two cases applies
    -- a construction site that forgets `value` entirely is rejected at
    construction, never silently defaulted into "not disclosed". See
    docs/adr/0006-disclosure-status-semantics.md."""

    id: Uuid7Id
    snapshot_id: Uuid7Id
    field: str
    value: str | None
    disclosure_status: DisclosureStatus = DisclosureStatus.DISCLOSED
    extraction_method: ExtractionMethod
    extraction_model: str | None = None
    prompt_version: str | None = None
    quoted_span: str | None = None
    confidence: Confidence | None = None

    @model_validator(mode="after")
    def _require_evidence_for_llm_facts(self) -> ExtractedFact:
        """ADR 0004's accepted clarification: the requirement that
        LLM-extracted facts carry both quoted_span and confidence was
        previously enforced only by intelligence/extract_facts.py's own
        construction code and by tests/contract/test_fixture_contract.py
        -- nothing stopped a DIFFERENT construction path (a future
        extraction call site, a hand-built fixture) from creating an
        LLM-attributed fact with no evidence at all. This model-level
        invariant makes that impossible regardless of how the object is
        built. Deterministic facts are unaffected -- they don't always
        have a natural quote to attach (see this class's own docstring)."""
        if self.extraction_method == ExtractionMethod.LLM_STRUCTURED_OUTPUT:
            if self.quoted_span is None or not self.quoted_span.strip():
                raise ValueError(
                    "ExtractedFact with extraction_method='llm_structured_output' "
                    "must have a non-empty quoted_span (ADR 0004)"
                )
            if self.confidence is None:
                raise ValueError(
                    "ExtractedFact with extraction_method='llm_structured_output' "
                    "must have confidence set (ADR 0004)"
                )
        return self

    @model_validator(mode="after")
    def _require_valid_disclosure_state(self) -> ExtractedFact:
        """ADR 0006: "not disclosed" is a groundable claim, not a default
        inferred from silence -- both invalid states below are rejected
        at construction, not just documented as a convention:
          - disclosure_status="not_disclosed" together with a non-null
            value -- contradictory: a fact can't simultaneously state a
            value and claim none was given.
          - disclosure_status="not_disclosed" without grounded evidence
            (a non-empty quoted_span citing the actual non-disclosure
            statement) -- the same evidence requirement ADR 0004
            established for a disclosed value applies here too; "not
            disclosed" needs a citation, not a default. Required
            regardless of extraction_method -- unlike
            _require_evidence_for_llm_facts above, ADR 0006 draws no
            deterministic-vs-LLM distinction here.
          - disclosure_status="disclosed" (the default) without a real,
            non-empty value -- the "normal" case's own invariant, now
            enforced at the model level now that `value` is Optional at
            the type level."""
        if self.disclosure_status == DisclosureStatus.NOT_DISCLOSED:
            if self.value is not None:
                raise ValueError(
                    "ExtractedFact with disclosure_status='not_disclosed' must have "
                    "value=None (ADR 0006) -- a fact can't state a value and also "
                    "claim none was given"
                )
            if self.quoted_span is None or not self.quoted_span.strip():
                raise ValueError(
                    "ExtractedFact with disclosure_status='not_disclosed' must have a "
                    "non-empty quoted_span citing the actual non-disclosure statement "
                    "(ADR 0006) -- 'not disclosed' is a groundable claim, not a "
                    "default inferred from silence"
                )
        elif not self.value:
            raise ValueError(
                "ExtractedFact with disclosure_status='disclosed' (the default) must "
                "have a non-empty value"
            )
        return self


# ---------------------------------------------------------------------------
# Change detection (intelligence)
# ---------------------------------------------------------------------------


class FactObservation(BaseModel):
    """The previous/current sub-object nested inside a Change — distinct
    from ExtractedFact, which is the persisted per-snapshot record this is
    derived from."""

    value: str | None = None
    observed_at: datetime | None = None
    snapshot_id: Uuid7Id | None = None
    source_url: HttpUrl | None = None


def _is_grounded(obs: FactObservation | None) -> bool:
    """True if `obs` is a real, citable observation -- present, with a
    non-null value AND a non-null snapshot_id. Shared by every
    validate_change_shape() branch below that needs to assert "this side
    is a real observation, not a placeholder or a disclosure-boundary
    null" -- extracted so that predicate has exactly one definition
    (ADR 0009's Phase 1 cleanup), not one hand-copied per branch."""
    return obs is not None and obs.value is not None and obs.snapshot_id is not None


def validate_change_shape(
    change_type: str,
    previous: FactObservation | None,
    current: FactObservation,
) -> None:
    """Enforces mutually exclusive, valid observation shapes per
    change_type — per review, this is FactStore.update_fact()'s own real
    invariant (verified by hand against every code path that constructs a
    Change there before relying on it). Standalone (not a method) so
    update_fact() can call it directly, BEFORE new_id()/
    change_set_id_factory() run, on the exact (change_type, previous,
    current) triple it is about to construct a Change from — the same
    check Change's own `_require_valid_change_shape` model_validator
    below applies at construction, so no OTHER construction path (a
    hand-built fixture, a future ingestion write path) can produce a
    self-contradictory Change either. Raises ValueError if the shape is
    contradictory; returns None (not the observations) since it never
    needs to transform them, only judge them.
      - "not_disclosed": `previous` must exist with a non-null value and
        a real snapshot_id (the fact WAS disclosed before); `current`
        must have value=None and a real snapshot_id (the citation for
        the withholding statement itself).
      - "disclosed": `current` must have a non-null value and a real
        snapshot_id. `previous`, if present at all, must have
        value=None and a real snapshot_id (a not_disclosed -> disclosed
        transition) — `previous=None` entirely (a genuine first-ever
        disclosure, no prior ExtractedFact of any kind) is also valid
        and unconstrained here.
      - Every other change_type -- "increased"/"decreased"/"changed",
        and any other string a future caller introduces (open,
        deliberately not a closed allowlist): both `previous` and
        `current` must have a non-null value and a real snapshot_id.
        "increased"/"decreased"/"changed" only ever arise from two real,
        grounded observations differing, never from a disclosure
        boundary (see _infer_change_type, facts.py) -- and an
        unrecognised change_type gets the same, stricter "two real
        observations" shape rather than silently skipping validation
        entirely, since "disclosed"/"not_disclosed" are the only
        change_types this model knows to relax that for."""
    if change_type == "not_disclosed":
        if not _is_grounded(previous):
            raise ValueError(
                "Change with change_type='not_disclosed' requires previous observation "
                "with non-null value and snapshot_id"
            )
        if current.value is not None or current.snapshot_id is None:
            raise ValueError(
                "Change with change_type='not_disclosed' requires current observation "
                "with value=None and snapshot_id"
            )
    elif change_type == "disclosed":
        if not _is_grounded(current):
            raise ValueError(
                "Change with change_type='disclosed' requires current observation "
                "with non-null value and snapshot_id"
            )
        if previous is not None and (previous.value is not None or previous.snapshot_id is None):
            raise ValueError(
                "Change with change_type='disclosed' having a previous observation "
                "requires previous.value=None (transition from not_disclosed) and "
                "snapshot_id"
            )
    else:
        # Generic real-change shape -- "increased"/"decreased"/"changed"
        # and any change_type this model doesn't otherwise recognise.
        # Deliberately NOT `elif change_type in (...)`: a closed
        # allowlist would let an unrecognised change_type string skip
        # shape validation entirely, which is the one thing this
        # function exists to prevent (any hand-built or future-caller-
        # constructed Change must still prove both sides are real
        # observations unless it's one of the two disclosure-boundary
        # types explicitly relaxed above).
        if not _is_grounded(previous):
            raise ValueError(
                f"Change with change_type='{change_type}' requires previous "
                "observation with non-null value and snapshot_id"
            )
        if not _is_grounded(current):
            raise ValueError(
                f"Change with change_type='{change_type}' requires current "
                "observation with non-null value and snapshot_id"
            )


class Change(BaseModel):
    """One field-level change. `previous` is null only when the absence
    means "not previously disclosed" — never used to mean "unknown".

    `previous.value`/`current.value` (FactObservation, not this field
    itself) can independently be null too — ADR 0006's disclosure-status
    transitions: change_type="disclosed" has previous.value=None,
    current.value=real; change_type="not_disclosed" has the reverse.
    Either way `previous`/`current` themselves (the FactObservation
    objects) are always present when the transition is a real Change —
    only their own `.value` goes null, carrying the citation for
    whichever side lacks a value.

    The exact required shape per change_type is enforced at construction
    by _require_valid_change_shape below, not just documented as a
    convention — see validate_change_shape's own docstring for the rules
    themselves.

    `id` and `detected_at` are the `(business_sort_value, id)` ordering tuple
    `/v1/changes` paginates on (ADR 0008 section 5). Both are
    `Field(frozen=True)`. `detected_at` is when the intelligence pipeline
    detected this change -- NOT `current.observed_at` (when a fact was observed
    in a source) or when a publisher published: those are different instants
    with different meanings. It is required, timezone-aware (naive rejected),
    normalized to UTC with microseconds preserved, and supplied by the
    orchestrator's injected batch detection time -- never `datetime.now()`
    inside this model or a graph node (ADR 0008 section 5.A)."""

    id: Uuid7Id = Field(frozen=True)
    change_set_id: Uuid7Id
    subject: Subject
    field: str
    change_type: str  # e.g. "increased", "decreased", "disclosed", "not_disclosed", "changed"
    previous: FactObservation | None = None
    current: FactObservation
    confidence: Confidence
    detected_at: OrderingTimestamp = Field(frozen=True)
    review_status: str = "pending"  # "pending" | "validated" | "rejected"

    @model_validator(mode="after")
    def _require_valid_change_shape(self) -> Change:
        """Delegates to the standalone validate_change_shape() above, so
        the exact same rule applies whether a Change is constructed
        directly (here) or pre-checked by a caller like FactStore.
        update_fact() before it spends any ids (see that function's own
        comment for why) — one definition, not two that could drift."""
        validate_change_shape(self.change_type, self.previous, self.current)
        return self


class ChangeSet(BaseModel):
    """The internal aggregate GET /v1/changes/{change_id} returns — one or
    more Changes about the same subject, grouped with their supporting
    snapshot ids."""

    id: Uuid7Id
    subject: Subject
    changes: list[Change] = Field(default_factory=list)
    previous_snapshot_ids: list[Uuid7Id] = Field(default_factory=list)
    current_snapshot_ids: list[Uuid7Id] = Field(default_factory=list)
    review_status: str = "pending"


# ---------------------------------------------------------------------------
# Digest (intelligence produces, delivery renders unchanged)
# ---------------------------------------------------------------------------


class ClaimValidationStatus(StrEnum):
    """A DigestClaim's evidence-check outcome -- owned by DigestClaim.
    "supported" is earned only by validate.py's checks (real citations,
    every cited snapshot known, grounded numbers); nothing else may set
    it directly (ADR 0009)."""

    PENDING = "pending"
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"


class DigestStatus(StrEnum):
    """A Digest's publication lifecycle -- owned by Digest. "published" is
    reachable only through validate.py::publish_digest() (ADR 0009);
    every other construction site starts a Digest at DRAFT."""

    DRAFT = "draft"
    REVIEW = "review"
    PUBLISHED = "published"


class DigestCitation(BaseModel):
    """A citation linking a claim to an immutable snapshot and its source metadata."""

    snapshot_id: Uuid7Id
    canonical_url: HttpUrl
    source_title: str

    @field_validator("source_title", mode="before")
    @classmethod
    def _validate_source_title(cls, v: object) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("source_title must be a non-empty string")
        return v.strip()


class DigestClaimChange(BaseModel):
    """Structured projection of a claim's originating change diff."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: Uuid7Id
    company: str
    product: str
    field: str
    change_type: str
    previous_value: str | None = None
    current_value: str | None = None


class DigestClaim(BaseModel):
    """Every factual claim requires >=1 valid citation. A digest
    containing an unsupported claim cannot enter "published" status
    automatically."""

    id: Uuid7Id
    change_id: Uuid7Id | None = None
    change: DigestClaimChange | None = None
    text: str
    citation_snapshot_ids: list[Uuid7Id] = Field(default_factory=list)
    citations: list[DigestCitation] = Field(default_factory=list)
    validation_status: ClaimValidationStatus = ClaimValidationStatus.PENDING

    @model_validator(mode="after")
    def _validate_change_linkage(self) -> Self:
        if self.change is not None:
            if self.change_id is None:
                raise ValueError("change_id must be set when change is provided")
            if self.change_id != self.change.id:
                raise ValueError(
                    f"change_id ({self.change_id}) must match change.id ({self.change.id})"
                )
        return self


class Digest(BaseModel):
    """`id` and `digest_date` are the `(business_sort_value, id)` ordering
    tuple `/v1/digests` paginates on (ADR 0008 section 5). Both are
    `Field(frozen=True)`. `digest_date` is a real `datetime.date` -- an
    impossible value like `"2026-13-40"` is rejected at construction rather
    than stored as an unparseable string -- and still serializes to exactly
    `YYYY-MM-DD` on the wire. Digest uniqueness / regeneration / version
    history is a separate lifecycle decision (ADR 0008 section 5.B)."""

    id: Uuid7Id = Field(frozen=True)
    digest_date: date = Field(frozen=True)  # wire: YYYY-MM-DD
    status: DigestStatus = DigestStatus.DRAFT
    title: str
    claims: list[DigestClaim] = Field(default_factory=list)
