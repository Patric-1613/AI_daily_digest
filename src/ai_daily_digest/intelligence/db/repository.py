"""PostgreSQL FactStore, change persistence, and feed repository — ADR 0011 §5."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_daily_digest.intelligence.db.models import (
    ChangeModel,
    ChangeSetModel,
    CurrentFactModel,
    DigestClaimCitationModel,
    DigestClaimModel,
    DigestModel,
    ExtractedFactModel,
    SubjectModel,
)
from ai_daily_digest.intelligence.facts import _infer_change_type, normalise_name
from ai_daily_digest.intelligence.validate import publish_digest as _validate_publish_digest
from ai_daily_digest.shared.ids import new_id
from ai_daily_digest.shared.repositories import (
    ChangeFeedFilter,
    DigestFeedFilter,
)
from ai_daily_digest.shared.schemas import (
    Change,
    ClaimValidationStatus,
    Confidence,
    Digest,
    DigestClaim,
    DigestStatus,
    ExtractedFact,
    FactObservation,
    Subject,
    normalize_ordering_timestamp,
    validate_change_shape,
)
from ai_daily_digest.shared.snapshot_resolver import SnapshotResolver

__all__ = [
    "PostgresChangeFeedRepository",
    "PostgresDigestFeedRepository",
    "PostgresDigestRepository",
    "PostgresFactStore",
]

_CHANGES_KEYSET_PREDICATE_SQL = text("(detected_at, id) < (:after_ts, :after_id)")
_DIGESTS_KEYSET_PREDICATE_SQL = text("(digest_date, id) < (:after_date, :after_id)")


def _subject_keys(subject: Subject) -> tuple[str, str]:
    """Return normalized canonical (company_key, product_key)."""
    return normalise_name(subject.company), normalise_name(subject.product)


def _ensure_utc(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _hash_lock_key(company_key: str, product_key: str, field: str) -> int:
    """Generate a stable 63-bit signed integer for pg_advisory_xact_lock."""
    raw = f"{company_key}:{product_key}:{field}".encode()
    digest = hashlib.sha256(raw).digest()
    # Use first 8 bytes signed integer (PostgreSQL bigint advisory lock key)
    val = int.from_bytes(digest[:8], byteorder="big", signed=True)
    return val


@dataclass(frozen=True)
class PriorFactState:
    """Immutable snapshot of prior current_fact state before advance."""

    fact_id: uuid.UUID
    snapshot_id: uuid.UUID
    observed_at: datetime
    extraction_version: int
    value: str | None
    disclosure_status: str


@dataclass(frozen=True)
class _ChangeMaterial:
    """Everything needed to build a real Change, short of the ids that must
    stay lazily allocated (ADR 0007) -- see detect_and_persist_changes()."""

    field: str
    change_type: str
    previous: FactObservation
    current: FactObservation
    confidence: Confidence


class PostgresFactStore:
    """Concrete PostgreSQL persistence repository for intelligence facts and changes.

    Guarantees:
    - Advisory locking on (subject, field) serializes read-compare-advance sequences.
    - 4-part ordering tuple: (observed_at DESC, snapshot_id DESC, extraction_version DESC, id DESC).
    - Idempotent replay with full 10-attribute verification.
    - Corrections on the current snapshot advance pointers without emitting false Changes.
    - ChangeSet citations derived dynamically with MIN(position) ASC order.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def ensure_subject(self, subject: Subject) -> SubjectModel:
        """Find or create canonical subject record (first-seen display name wins).

        Uses an atomic INSERT ... ON CONFLICT DO NOTHING rather than a plain
        check-then-act SELECT/INSERT: two workers racing to create the SAME new
        subject while processing different fields (e.g. the very first
        observation for a company/product split across concurrent extraction
        calls) would otherwise both see no existing row via the SELECT and
        both attempt the INSERT -- the second raises IntegrityError on the
        (company_key, product_key) primary key instead of just resolving to
        the row the first worker already created. DO NOTHING makes the loser
        a no-op instead, and the re-SELECT below returns whichever row won,
        preserving "first-seen display name wins" regardless of which
        worker's INSERT actually landed.
        """
        ck, pk = _subject_keys(subject)
        now = datetime.now(UTC)
        bind = self._session.get_bind()
        is_sqlite = bind is not None and bind.dialect.name == "sqlite"
        created_at = now.isoformat() if is_sqlite else now

        await self._session.execute(
            text("""
                INSERT INTO subjects (company_key, product_key, company, product, created_at)
                VALUES (:company_key, :product_key, :company, :product, :created_at)
                ON CONFLICT (company_key, product_key) DO NOTHING
            """),
            {
                "company_key": ck,
                "product_key": pk,
                "company": subject.company,
                "product": subject.product,
                "created_at": created_at,
            },
        )
        await self._session.flush()
        existing = await self._session.get(SubjectModel, (ck, pk))
        if existing is None:
            # Unreachable in practice: the INSERT above either created the row
            # or a concurrent committed writer already had -- re-fetching
            # after the statement completes must find one or the other. A
            # clear failure here beats returning an Optional callers would
            # need to null-check everywhere else in this class.
            raise RuntimeError(
                f"ensure_subject(): no subjects row found for ({ck!r}, {pk!r}) "
                "immediately after INSERT ... ON CONFLICT DO NOTHING"
            )
        return existing

    async def lock_subject_fields(self, subject: Subject, fields: Sequence[str]) -> None:
        """Acquire transaction-scoped advisory locks on (subject, field) in sorted order."""
        bind = self._session.bind
        dialect_name = bind.dialect.name if bind is not None else ""
        if dialect_name != "postgresql":
            # In SQLite or mock engines, row-level advisory lock functions do not exist
            return

        ck, pk = _subject_keys(subject)
        sorted_fields = sorted(fields)
        for field_name in sorted_fields:
            lock_id = _hash_lock_key(ck, pk, field_name)
            await self._session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": lock_id},
            )

    async def record_extracted_facts(  # pylint: disable=too-many-branches
        self,
        subject: Subject,
        facts: Sequence[ExtractedFact],
        *,
        snapshot_observed_at: datetime,
        extraction_version: int = 1,
    ) -> list[ExtractedFactModel]:
        """Insert extracted facts idempotently; verify on replay; fail closed on divergence."""
        await self.ensure_subject(subject)
        ck, pk = _subject_keys(subject)

        recorded: list[ExtractedFactModel] = []
        for fact in facts:
            # Check for existing record
            stmt = select(ExtractedFactModel).where(
                ExtractedFactModel.snapshot_id == fact.snapshot_id,
                ExtractedFactModel.company_key == ck,
                ExtractedFactModel.product_key == pk,
                ExtractedFactModel.field == fact.field,
                ExtractedFactModel.extraction_version == extraction_version,
            )
            res = await self._session.execute(stmt)
            existing = res.scalar_one_or_none()

            if existing is not None:
                # Replay verification: all 10 immutable attributes must match
                mismatches: list[str] = []
                if existing.value != fact.value:
                    mismatches.append(f"value: {existing.value} != {fact.value}")
                if existing.disclosure_status != fact.disclosure_status:
                    mismatches.append(
                        f"disclosure_status: {existing.disclosure_status} != "
                        f"{fact.disclosure_status}"
                    )
                if existing.extraction_method != fact.extraction_method:
                    mismatches.append(
                        f"extraction_method: {existing.extraction_method} != "
                        f"{fact.extraction_method}"
                    )
                if existing.extraction_model != fact.extraction_model:
                    mismatches.append(
                        f"extraction_model: {existing.extraction_model} != {fact.extraction_model}"
                    )
                if existing.prompt_version != fact.prompt_version:
                    mismatches.append(
                        f"prompt_version: {existing.prompt_version} != {fact.prompt_version}"
                    )
                if existing.quoted_span != fact.quoted_span:
                    mismatches.append(f"quoted_span: {existing.quoted_span} != {fact.quoted_span}")
                if existing.confidence is not None and fact.confidence is not None:
                    if abs(existing.confidence - fact.confidence) > 1e-6:
                        mismatches.append(f"confidence: {existing.confidence} != {fact.confidence}")
                elif existing.confidence != fact.confidence:
                    mismatches.append(f"confidence: {existing.confidence} != {fact.confidence}")
                if existing.observed_at != snapshot_observed_at:
                    mismatches.append(
                        f"observed_at: {existing.observed_at} != {snapshot_observed_at}"
                    )

                if mismatches:
                    msg = (
                        f"Replay verification failed for fact {fact.field} on "
                        f"snapshot {fact.snapshot_id}: " + "; ".join(mismatches)
                    )
                    raise ValueError(msg)
                recorded.append(existing)
            else:
                model = ExtractedFactModel(
                    id=fact.id,
                    snapshot_id=fact.snapshot_id,
                    company_key=ck,
                    product_key=pk,
                    field=fact.field,
                    value=fact.value,
                    disclosure_status=fact.disclosure_status,
                    extraction_method=fact.extraction_method,
                    extraction_model=fact.extraction_model,
                    prompt_version=fact.prompt_version,
                    extraction_version=extraction_version,
                    quoted_span=fact.quoted_span,
                    confidence=fact.confidence,
                    observed_at=snapshot_observed_at,
                    created_at=datetime.now(UTC),
                )
                self._session.add(model)
                await self._session.flush()
                recorded.append(model)

        return recorded

    async def read_current_facts(
        self, subject: Subject, fields: Sequence[str]
    ) -> dict[str, tuple[CurrentFactModel, ExtractedFactModel]]:
        """Read current confirmed facts and their referenced extracted facts.

        Per ADR 0011 SS5.1, this is always called *inside* the caller's advisory lock
        (see `lock_subject_fields()` / `detect_and_persist_changes()`), which is what
        guarantees a fresh, non-stale read -- not row-level locking here. A `SELECT ...
        FOR UPDATE OF current_facts` was tried instead and reverted: under READ
        COMMITTED, EvalPlanQual re-fetches only the locked `current_facts` row after
        unblocking, but re-evaluates the `JOIN extracted_facts` against the *original*
        per-statement snapshot -- so a row inserted by the just-committed writer isn't
        visible to the join yet, and the read spuriously returns nothing.
        """
        ck, pk = _subject_keys(subject)
        stmt = (
            select(CurrentFactModel, ExtractedFactModel)
            .join(
                ExtractedFactModel,
                CurrentFactModel.fact_id == ExtractedFactModel.id,
            )
            .where(
                CurrentFactModel.company_key == ck,
                CurrentFactModel.product_key == pk,
                CurrentFactModel.field.in_(fields),
            )
        )
        res = await self._session.execute(stmt)
        return {cf.field: (cf, ef) for cf, ef in res.all()}

    async def _upsert_current_fact(
        self,
        *,
        company_key: str,
        product_key: str,
        fact: ExtractedFactModel,
        now_dt: datetime,
        is_sqlite: bool,
    ) -> bool:
        fid = fact.id.hex if is_sqlite else fact.id
        sid = fact.snapshot_id.hex if is_sqlite else fact.snapshot_id
        obs = (
            fact.observed_at.isoformat()
            if is_sqlite and hasattr(fact.observed_at, "isoformat")
            else fact.observed_at
        )
        upd = now_dt.isoformat() if is_sqlite else now_dt

        stmt = text("""
            INSERT INTO current_facts
                (company_key, product_key, field, fact_id, snapshot_id, observed_at,
                 extraction_version, updated_at)
            VALUES (:company_key, :product_key, :field, :fact_id, :snapshot_id, :observed_at,
                    :extraction_version, :updated_at)
            ON CONFLICT (company_key, product_key, field)
            DO UPDATE SET
                fact_id = EXCLUDED.fact_id,
                snapshot_id = EXCLUDED.snapshot_id,
                observed_at = EXCLUDED.observed_at,
                extraction_version = EXCLUDED.extraction_version,
                updated_at = EXCLUDED.updated_at
            WHERE (EXCLUDED.observed_at, EXCLUDED.snapshot_id, EXCLUDED.extraction_version, EXCLUDED.fact_id)
                > (current_facts.observed_at, current_facts.snapshot_id, current_facts.extraction_version,
                   current_facts.fact_id)
            RETURNING fact_id
        """)
        result = await self._session.execute(
            stmt,
            {
                "company_key": company_key,
                "product_key": product_key,
                "field": fact.field,
                "fact_id": fid,
                "snapshot_id": sid,
                "observed_at": obs,
                "extraction_version": fact.extraction_version,
                "updated_at": upd,
            },
        )
        return result.first() is not None

    async def advance_current_facts(
        self,
        subject: Subject,
        recorded_facts: Sequence[ExtractedFactModel],
    ) -> dict[str, bool]:
        """Conditionally advance current_facts using PostgreSQL atomic upsert.

        Executes atomic INSERT ... ON CONFLICT DO UPDATE, gated by a full row-value
        comparison over (observed_at, snapshot_id, extraction_version, fact_id) -- see
        `_upsert_current_fact()`.
        Returns a dict mapping field_name -> bool (True if the pointer advanced).
        """
        ck, pk = _subject_keys(subject)
        advanced_map: dict[str, bool] = {}
        now_dt = datetime.now(UTC)

        bind = self._session.get_bind()
        is_sqlite = bind is not None and bind.dialect.name == "sqlite"

        for fact in recorded_facts:
            advanced = await self._upsert_current_fact(
                company_key=ck,
                product_key=pk,
                fact=fact,
                now_dt=now_dt,
                is_sqlite=is_sqlite,
            )
            advanced_map[fact.field] = advanced

        await self._session.flush()
        return advanced_map

    async def detect_and_persist_changes(  # pylint: disable=too-many-arguments,too-many-locals
        self,
        subject: Subject,
        facts: Sequence[ExtractedFact],
        *,
        snapshot_observed_at: datetime,
        detected_at: datetime,
        extraction_version: int = 1,
        change_set_id: uuid.UUID | None = None,
    ) -> list[Change]:
        """Detect and persist changes atomically under an advisory lock.

        Invariants enforced:
        - Advisory locks on all candidate fields in lexicographical order.
        - Corrections on the current snapshot advance pointers without emitting a Change.
        - Losing or identical candidates emit no Change.
        - Distinct snapshot differences emit valid Changes with explicit positions.
        - Entire batch commits atomically.
        """
        detection_time = normalize_ordering_timestamp(detected_at)

        ck, pk = _subject_keys(subject)
        fields = [f.field for f in facts]
        await self.lock_subject_fields(subject, fields)

        # Read current state prior to advance into an immutable snapshot. This happens
        # after lock_subject_fields() above has acquired the advisory lock for every
        # field, so any concurrent worker that was mid-write on the same (subject, field)
        # has already been forced to finish and commit before we reach this read (ADR
        # 0011 SS5.1) -- the read is guaranteed fresh, never a stale pre-lock snapshot.
        current_map = await self.read_current_facts(subject, fields)
        prior_state: dict[str, PriorFactState] = {
            field: PriorFactState(
                fact_id=cf.fact_id,
                snapshot_id=cf.snapshot_id,
                observed_at=_ensure_utc(cf.observed_at),
                extraction_version=cf.extraction_version,
                value=ef.value,
                disclosure_status=ef.disclosure_status,
            )
            for field, (cf, ef) in current_map.items()
        }

        # Record facts into immutable extracted_facts
        recorded_facts = await self.record_extracted_facts(
            subject,
            facts,
            snapshot_observed_at=snapshot_observed_at,
            extraction_version=extraction_version,
        )

        # Advance current facts conditionally
        advanced_map = await self.advance_current_facts(subject, recorded_facts)

        # Pass 1: determine WHICH fields produced a genuine business Change,
        # without allocating any id yet. ADR 0007 requires a ChangeSet id
        # (and each Change's own id) to be allocated lazily, only once at
        # least one real Change is confirmed -- first observations,
        # same-snapshot corrections, and unchanged values must not consume
        # an id just because they were candidates.
        materials: list[_ChangeMaterial] = []
        fact_by_field = {f.field: f for f in recorded_facts}

        for field_name, advanced in advanced_map.items():
            if not advanced:
                continue

            candidate_fact = fact_by_field[field_name]
            prior = prior_state.get(field_name)

            if prior is None:
                # Genuine first observation: establishes baseline state
                # without emitting a business Change
                continue

            # INVARIANT: Correction of the same snapshot must NOT emit a business Change!
            if prior.snapshot_id == candidate_fact.snapshot_id:
                continue

            # Check if values actually differ
            if (
                prior.value == candidate_fact.value
                and prior.disclosure_status == candidate_fact.disclosure_status
            ):
                continue

            prev_obs = FactObservation(
                value=prior.value,
                observed_at=prior.observed_at,
                snapshot_id=prior.snapshot_id,
            )
            curr_obs = FactObservation(
                value=candidate_fact.value,
                observed_at=_ensure_utc(candidate_fact.observed_at),
                snapshot_id=candidate_fact.snapshot_id,
            )

            change_type = str(_infer_change_type(prev_obs.value, curr_obs.value))
            conf: Confidence = (
                candidate_fact.confidence if candidate_fact.confidence is not None else 1.0
            )

            validate_change_shape(change_type, prev_obs, curr_obs)

            materials.append(
                _ChangeMaterial(
                    field=field_name,
                    change_type=change_type,
                    previous=prev_obs,
                    current=curr_obs,
                    confidence=conf,
                )
            )

        if not materials:
            return []

        # Pass 2: at least one real Change is now confirmed -- only now
        # allocate the ChangeSet id (and each Change's own id) and build the
        # actual Change/ChangeSetModel/ChangeModel objects.
        resolved_change_set_id = change_set_id or new_id()
        candidate_changes: list[Change] = [
            Change(
                id=new_id(),
                change_set_id=resolved_change_set_id,
                detected_at=detection_time,
                subject=subject,
                field=m.field,
                change_type=m.change_type,
                previous=m.previous,
                current=m.current,
                confidence=m.confidence,
                review_status="pending",
            )
            for m in materials
        ]

        cs_model = ChangeSetModel(
            id=resolved_change_set_id,
            company_key=ck,
            product_key=pk,
            review_status="pending",
            created_at=datetime.now(UTC),
        )
        self._session.add(cs_model)
        await self._session.flush()

        for position, change in enumerate(candidate_changes):
            ch_model = ChangeModel(
                id=change.id,
                detected_at=change.detected_at,
                change_set_id=resolved_change_set_id,
                position=position,
                company_key=ck,
                product_key=pk,
                field=change.field,
                change_type=change.change_type,
                confidence=change.confidence,
                review_status=change.review_status,
                previous_value=change.previous.value if change.previous else None,
                previous_observed_at=change.previous.observed_at if change.previous else None,
                previous_snapshot_id=change.previous.snapshot_id if change.previous else None,
                current_value=change.current.value,
                current_observed_at=change.current.observed_at,
                current_snapshot_id=change.current.snapshot_id,
                created_at=datetime.now(UTC),
            )
            self._session.add(ch_model)

        await self._session.flush()
        return candidate_changes

    async def get_changes_for_changeset(self, change_set_id: uuid.UUID) -> list[ChangeModel]:
        """Fetch changes for a ChangeSet ordered deterministically by position ASC."""
        stmt = (
            select(ChangeModel)
            .where(ChangeModel.change_set_id == change_set_id)
            .order_by(ChangeModel.position.asc())
        )
        res = await self._session.execute(stmt)
        return list(res.scalars().all())

    async def derive_changeset_citations(
        self, change_set_id: uuid.UUID
    ) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
        """Derive (current_snapshot_ids, previous_snapshot_ids) ordered by MIN(position) ASC."""
        current_stmt = (
            select(ChangeModel.current_snapshot_id)
            .where(ChangeModel.change_set_id == change_set_id)
            .group_by(ChangeModel.current_snapshot_id)
            .order_by(func.min(ChangeModel.position).asc())
        )
        curr_res = await self._session.execute(current_stmt)
        current_ids = list(curr_res.scalars().all())

        previous_stmt = (
            select(ChangeModel.previous_snapshot_id)
            .where(
                ChangeModel.change_set_id == change_set_id,
                ChangeModel.previous_snapshot_id.is_not(None),
            )
            .group_by(ChangeModel.previous_snapshot_id)
            .order_by(func.min(ChangeModel.position).asc())
        )
        prev_res = await self._session.execute(previous_stmt)
        previous_ids = [pid for pid in prev_res.scalars().all() if pid is not None]

        return current_ids, previous_ids

    # -- shared feed read protocols (shared/repositories.py) ------------

    async def list_changes(  # pylint: disable=too-many-locals
        self,
        *,
        feed_filter: ChangeFeedFilter | None = None,
        after: tuple[datetime, uuid.UUID] | None = None,
        limit: int = 20,
    ) -> Sequence[Change]:
        """Fetch up to limit + 1 changes ordered by (detected_at DESC, id DESC) — ADR 0008.

        Args:
            feed_filter: Optional filter criteria (company_key, product_key, field).
            after: Keyset continuation tuple (detected_at, id), if resuming.
            limit: Maximum items to return in the page (returns up to limit + 1
                   to support forward cursor generation).

        Returns:
            A sequence of up to limit + 1 Change domain instances matching the
            filters and keyset predicate.
        """
        if limit <= 0:
            raise ValueError(f"limit must be positive, got {limit}")

        stmt = select(ChangeModel, SubjectModel.company, SubjectModel.product).join(
            SubjectModel,
            (ChangeModel.company_key == SubjectModel.company_key)
            & (ChangeModel.product_key == SubjectModel.product_key),
        )
        if feed_filter is not None:
            if feed_filter.company_key is not None:
                stmt = stmt.where(ChangeModel.company_key == feed_filter.company_key)
            if feed_filter.product_key is not None:
                stmt = stmt.where(ChangeModel.product_key == feed_filter.product_key)
            if feed_filter.field is not None:
                stmt = stmt.where(ChangeModel.field == feed_filter.field)
            if feed_filter.detected_from is not None:
                stmt = stmt.where(ChangeModel.detected_at >= feed_filter.detected_from)
            if feed_filter.detected_to is not None:
                stmt = stmt.where(ChangeModel.detected_at < feed_filter.detected_to)
        if after is not None:
            after_ts, after_id = after
            stmt = stmt.where(
                _CHANGES_KEYSET_PREDICATE_SQL.bindparams(after_ts=after_ts, after_id=after_id)
            )

        stmt = stmt.order_by(ChangeModel.detected_at.desc(), ChangeModel.id.desc()).limit(limit + 1)
        res = await self._session.execute(stmt)
        rows = res.all()

        results: list[Change] = []
        for change_row, company_name, product_name in rows:
            prev_obs: FactObservation | None = None
            if (
                change_row.previous_snapshot_id is not None
                or change_row.previous_observed_at is not None
                or change_row.previous_value is not None
            ):
                prev_obs = FactObservation(
                    value=change_row.previous_value,
                    observed_at=change_row.previous_observed_at,
                    snapshot_id=change_row.previous_snapshot_id,
                )
            curr_obs = FactObservation(
                value=change_row.current_value,
                observed_at=change_row.current_observed_at,
                snapshot_id=change_row.current_snapshot_id,
            )
            results.append(
                Change(
                    id=change_row.id,
                    change_set_id=change_row.change_set_id,
                    subject=Subject(company=company_name, product=product_name),
                    field=change_row.field,
                    change_type=change_row.change_type,
                    previous=prev_obs,
                    current=curr_obs,
                    confidence=change_row.confidence,
                    detected_at=change_row.detected_at,
                    review_status=change_row.review_status,
                )
            )
        return results

    async def list_digests(
        self,
        *,
        feed_filter: DigestFeedFilter | None = None,
        after: tuple[date, uuid.UUID] | None = None,
        limit: int = 20,
    ) -> Sequence[Digest]:
        """Fetch up to limit + 1 published digests ordered by (digest_date DESC, id DESC).

        IMPORTANT: Only digests with status='published' are returned, matching the
        idx_digests_pagination partial index.

        Args:
            feed_filter: Optional filter criteria (date_from, date_to).
            after: Keyset continuation tuple (digest_date, id), if resuming.
            limit: Maximum items to return in the page (returns up to limit + 1
                   to support forward cursor generation).

        Returns:
            A sequence of up to limit + 1 published Digest domain instances matching
            the filters and keyset predicate.
        """
        if limit <= 0:
            raise ValueError(f"limit must be positive, got {limit}")

        stmt = select(DigestModel).where(DigestModel.status == DigestStatus.PUBLISHED.value)
        if feed_filter is not None:
            if feed_filter.date_from is not None:
                stmt = stmt.where(DigestModel.digest_date >= feed_filter.date_from)
            if feed_filter.date_to is not None:
                stmt = stmt.where(DigestModel.digest_date < feed_filter.date_to)
        if after is not None:
            after_date, after_id = after
            stmt = stmt.where(
                _DIGESTS_KEYSET_PREDICATE_SQL.bindparams(after_date=after_date, after_id=after_id)
            )

        stmt = stmt.order_by(DigestModel.digest_date.desc(), DigestModel.id.desc()).limit(limit + 1)
        res = await self._session.execute(stmt)
        digest_rows = list(res.scalars().all())
        return await self._hydrate_digests(digest_rows)

    # pylint: disable=too-many-locals
    async def _hydrate_digests(self, digest_rows: list[DigestModel]) -> list[Digest]:
        """Hydrate DigestModel instances into domain Digest objects with claims and citations in
        exact positional order."""
        if not digest_rows:
            return []

        digest_ids = [d.id for d in digest_rows]
        claims_stmt = (
            select(DigestClaimModel)
            .where(DigestClaimModel.digest_id.in_(digest_ids))
            .order_by(DigestClaimModel.digest_id, DigestClaimModel.position.asc())
        )
        claims_res = await self._session.execute(claims_stmt)
        claims_rows = list(claims_res.scalars().all())

        claims_by_digest: dict[uuid.UUID, list[DigestClaimModel]] = {}
        for c in claims_rows:
            claims_by_digest.setdefault(c.digest_id, []).append(c)

        citations_by_claim: dict[uuid.UUID, list[uuid.UUID]] = {}
        if claims_rows:
            claim_ids = [c.id for c in claims_rows]
            citations_stmt = (
                select(DigestClaimCitationModel)
                .where(DigestClaimCitationModel.claim_id.in_(claim_ids))
                .order_by(
                    DigestClaimCitationModel.claim_id, DigestClaimCitationModel.position.asc()
                )
            )
            cit_res = await self._session.execute(citations_stmt)
            for cit in cit_res.scalars().all():
                citations_by_claim.setdefault(cit.claim_id, []).append(cit.snapshot_id)

        digests: list[Digest] = []
        for d in digest_rows:
            c_models = claims_by_digest.get(d.id, [])
            digest_claims = [
                DigestClaim(
                    id=c.id,
                    text=c.text,
                    citation_snapshot_ids=citations_by_claim.get(c.id, []),
                    validation_status=ClaimValidationStatus(c.validation_status),
                )
                for c in c_models
            ]
            digests.append(
                Digest(
                    id=d.id,
                    digest_date=d.digest_date,
                    status=DigestStatus(d.status),
                    title=d.title,
                    claims=digest_claims,
                )
            )
        return digests

    async def get_digest_by_id(self, digest_id: uuid.UUID) -> Digest | None:
        """Retrieve a digest aggregate by its unique ID, preserving claim and citation order."""
        stmt = select(DigestModel).where(DigestModel.id == digest_id)
        res = await self._session.execute(stmt)
        row = res.scalar_one_or_none()
        if row is None:
            return None
        hydrated = await self._hydrate_digests([row])
        return hydrated[0] if hydrated else None

    async def get_latest_published_digest(self) -> Digest | None:
        """Retrieve the most recently published digest by (digest_date DESC, id DESC)."""
        stmt = (
            select(DigestModel)
            .where(DigestModel.status == DigestStatus.PUBLISHED.value)
            .order_by(DigestModel.digest_date.desc(), DigestModel.id.desc())
            .limit(1)
        )
        res = await self._session.execute(stmt)
        row = res.scalar_one_or_none()
        if row is None:
            return None
        hydrated = await self._hydrate_digests([row])
        return hydrated[0] if hydrated else None

    async def _insert_claims_and_citations(self, digest: Digest) -> None:
        """Insert ordered claims and citations for a digest."""
        now = datetime.now(UTC)
        bind = self._session.get_bind()
        is_sqlite = bind is not None and bind.dialect.name == "sqlite"
        created_at = now.isoformat() if is_sqlite else now

        for claim_pos, claim in enumerate(digest.claims):
            c_model = DigestClaimModel(
                id=claim.id,
                digest_id=digest.id,
                position=claim_pos,
                text=claim.text,
                validation_status=claim.validation_status.value,
                created_at=created_at,
            )
            self._session.add(c_model)
        await self._session.flush()

        for claim in digest.claims:
            for cit_pos, snap_id in enumerate(claim.citation_snapshot_ids):
                cit_model = DigestClaimCitationModel(
                    claim_id=claim.id,
                    snapshot_id=snap_id,
                    position=cit_pos,
                    created_at=created_at,
                )
                self._session.add(cit_model)
        await self._session.flush()

    async def persist_digest(self, digest: Digest) -> Digest:
        """Persist a digest aggregate with its ordered claims and citations.

        Idempotent on repeat calls with identical attributes.
        Raises ValueError if an already-published digest is modified.
        """
        existing = await self.get_digest_by_id(digest.id)
        if existing is not None:
            if existing.status == DigestStatus.PUBLISHED:
                if (
                    existing.digest_date == digest.digest_date
                    and existing.title == digest.title
                    and existing.claims == digest.claims
                ):
                    return existing
                raise ValueError(
                    f"Cannot modify already-published digest {digest.id}: attributes differ"
                )
            if digest.status == DigestStatus.PUBLISHED:
                raise ValueError(
                    f"Cannot publish existing digest {digest.id} via persist_digest(); "
                    "use publish_digest() to validate and transition to published"
                )
            if existing == digest:
                return existing

            d_model = await self._session.get(DigestModel, digest.id)
            if d_model is not None:
                d_model.digest_date = digest.digest_date
                d_model.title = digest.title
                d_model.status = digest.status.value

            old_claims_res = await self._session.execute(
                select(DigestClaimModel.id).where(DigestClaimModel.digest_id == digest.id)
            )
            old_claim_ids = list(old_claims_res.scalars().all())
            if old_claim_ids:
                await self._session.execute(
                    delete(DigestClaimCitationModel).where(
                        DigestClaimCitationModel.claim_id.in_(old_claim_ids)
                    )
                )
                await self._session.execute(
                    delete(DigestClaimModel).where(DigestClaimModel.digest_id == digest.id)
                )
                await self._session.flush()

            await self._insert_claims_and_citations(digest)
            await self._session.flush()
            return await self.get_digest_by_id(digest.id) or digest

        now = datetime.now(UTC)
        bind = self._session.get_bind()
        is_sqlite = bind is not None and bind.dialect.name == "sqlite"
        created_at = now.isoformat() if is_sqlite else now

        initial_status = (
            DigestStatus.DRAFT.value
            if digest.status == DigestStatus.PUBLISHED
            else digest.status.value
        )

        d_model = DigestModel(
            id=digest.id,
            digest_date=digest.digest_date,
            status=initial_status,
            title=digest.title,
            created_at=created_at,
        )
        self._session.add(d_model)
        await self._session.flush()

        await self._insert_claims_and_citations(digest)
        await self._session.flush()

        if digest.status == DigestStatus.PUBLISHED:
            d_model.status = DigestStatus.PUBLISHED.value
            await self._session.flush()

        return await self.get_digest_by_id(digest.id) or digest

    async def publish_digest(
        self,
        digest_id: uuid.UUID,
        *,
        known_snapshot_ids: set[uuid.UUID],
        snapshot_resolver: SnapshotResolver,
    ) -> Digest:
        """Publish an existing digest via the intelligence validation gate.

        Delegates to validate.py::publish_digest() and updates database state.
        Sequencing guarantee: digest_claims.validation_status rows are updated
        and flushed before DigestModel.status = 'published' is flushed, ensuring
        trg_enforce_digest_publication observes supported claims.

        Raises:
            ValueError: If digest_id is not found.
        """
        stored_digest = await self.get_digest_by_id(digest_id)
        if stored_digest is None:
            raise ValueError(f"Digest with id {digest_id} not found")

        validated = _validate_publish_digest(
            stored_digest,
            known_snapshot_ids,
            snapshot_resolver=snapshot_resolver,
        )

        claims_stmt = select(DigestClaimModel).where(DigestClaimModel.digest_id == digest_id)
        res = await self._session.execute(claims_stmt)
        claim_models = {c.id: c for c in res.scalars().all()}

        for claim in validated.claims:
            c_model = claim_models.get(claim.id)
            if c_model is not None:
                c_model.validation_status = claim.validation_status.value

        await self._session.flush()

        digest_model = await self._session.get(DigestModel, digest_id)
        if digest_model is None:
            raise ValueError(f"Digest with id {digest_id} not found")
        digest_model.status = validated.status.value

        await self._session.flush()
        return validated


PostgresChangeFeedRepository = PostgresFactStore
PostgresDigestFeedRepository = PostgresFactStore
PostgresDigestRepository = PostgresFactStore
