"""Production composition root for the daily intelligence pipeline.

`generate-digest` (see `[project.scripts]`), or
`python -m ai_daily_digest.intelligence.run`:

1. parse CLI flags: `--digest-date` (defaults to current date in UTC) or,
   mutually exclusive with it, `--previous-complete-utc-day` (the last
   fully elapsed UTC day -- what a 06:00 UTC cron should process);
   `--since` (defaults to a fixed lookback, e.g. 24h), optional `--limit`,
   and optional `--title`;
2. resolve the half-open query window `[window_start, window_end)` without
   introducing a new processed/ledger table;
3. read `DATABASE_URL` through `shared.config.DatabaseConfig.from_env()`;
4. build the process's engine + session factory with
   `shared.db.build_engine` / `build_session_factory` (creates no engine,
   metadata, or migration of its own, per ADR 0002);
5. select `DocumentSnapshotRow` and `SourceItemRow` within the window;
6. process snapshots in individual transactions (one commit per item, per
   ADR 0002 section 13 failure isolation);
7. assemble and publish the daily `Digest` aggregate via `PostgresFactStore`,
   preserving two-layer publication gates and safety policies;
8. print `DigestRunReport` as single-line JSON to stdout and exit
   `0` (published) / `1` (partial or review) / `2` (failed / could not start).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import re
import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from pydantic import HttpUrl
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ai_daily_digest.ingestion.db.models import DocumentSnapshotRow, SourceItemRow
from ai_daily_digest.intelligence.assemble_digest import assemble_digest
from ai_daily_digest.intelligence.change_sets import get_or_create_change_set_id
from ai_daily_digest.intelligence.compare_subjects import (
    ComparisonResponse,
    FactRow,
    compare_subjects,
)
from ai_daily_digest.intelligence.db.models import SubjectModel
from ai_daily_digest.intelligence.db.repository import PostgresFactStore
from ai_daily_digest.intelligence.draft_claims import draft_change_claim
from ai_daily_digest.intelligence.evaluate import (
    citation_validity,
    duplicate_rate,
    unsupported_claim_count,
)
from ai_daily_digest.intelligence.extract_facts import FactExtractionResponse, extract_facts
from ai_daily_digest.intelligence.resolve import (
    SubjectAlias,
    load_alias_table,
    resolve_deterministic,
)
from ai_daily_digest.intelligence.resolve_llm import ResolveLLMResponse, resolve_via_llm
from ai_daily_digest.shared.attributes import COMPARISON_RULES
from ai_daily_digest.shared.config import DatabaseConfig
from ai_daily_digest.shared.db import build_engine, build_session_factory
from ai_daily_digest.shared.schemas import (
    Change,
    Digest,
    DigestClaim,
    DigestStatus,
    DocumentSnapshot,
    SourceItem,
    Subject,
    normalize_ordering_timestamp,
)
from ai_daily_digest.shared.snapshot_resolver import InMemorySnapshotResolver

LOGGER = logging.getLogger(__name__)

_SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
_HOURS_RE = re.compile(r"^(\d+)\s*h(?:ours?)?$", re.IGNORECASE)
_DAYS_RE = re.compile(r"^(\d+)\s*d(?:ays?)?$", re.IGNORECASE)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def previous_complete_utc_day(now: datetime) -> date:
    """Return the previous *completed* UTC calendar date relative to ``now``.

    Pure and clock-injectable so callers (and tests) supply the reference
    instant explicitly. ``now`` must be timezone-aware; it is converted to
    UTC before the calendar date is taken, so a non-UTC aware value still
    yields the correct UTC day. A naive datetime is rejected rather than
    silently assumed to be UTC.

    Example: ``now = 2026-09-10T06:00:00+00:00`` -> ``date(2026, 9, 9)``.
    Paired with ``resolve_window(that_date, since="24h")`` this gives the
    half-open window ``[2026-09-09T00:00Z, 2026-09-10T00:00Z)`` -- the last
    fully elapsed UTC day at the time the cron fires.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("previous_complete_utc_day requires a timezone-aware datetime")
    return now.astimezone(UTC).date() - timedelta(days=1)


def _never_auto_publish_comparisons(digest: Digest, comparison_claim_ids: set[uuid.UUID]) -> Digest:
    """Safety guard: no cross-subject comparison claim may auto-publish a digest."""
    if comparison_claim_ids and digest.status == DigestStatus.PUBLISHED:
        return digest.model_copy(update={"status": DigestStatus.REVIEW})
    return digest


def _claims_equivalent(
    claims_a: Sequence[DigestClaim],
    claims_b: Sequence[DigestClaim],
) -> bool:
    """Check whether two sequences of claims have identical claim text and citations."""
    if len(claims_a) != len(claims_b):
        return False

    def _sig(claim: DigestClaim) -> tuple[str, tuple[str, ...]]:
        sorted_citations = tuple(sorted(str(cid) for cid in claim.citation_snapshot_ids))
        return (claim.text.strip(), sorted_citations)

    return sorted(_sig(c) for c in claims_a) == sorted(_sig(c) for c in claims_b)


def to_source_item(row: SourceItemRow) -> SourceItem:
    """Convert SourceItemRow ORM instance to domain SourceItem."""
    return SourceItem(
        id=row.id,
        dedupe_key=row.dedupe_key,
        source_id=row.source_id,
        publisher=row.publisher,
        title=row.title,
        canonical_url=HttpUrl(row.canonical_url),
        published_at=row.published_at,
        updated_at=row.updated_at,
        first_fetched_at=row.first_fetched_at,
        latest_snapshot_id=row.latest_snapshot_id,
        event_id=row.event_id,
        authors=list(row.authors or []),
        tags=list(row.tags or []),
        language=row.language,
    )


def to_document_snapshot(row: DocumentSnapshotRow) -> DocumentSnapshot:
    """Convert DocumentSnapshotRow ORM instance to domain DocumentSnapshot."""
    return DocumentSnapshot(
        id=row.id,
        source_item_id=row.source_item_id,
        fetched_at=row.fetched_at,
        content_hash=row.content_hash,
        content_text=row.content_text,
        raw_location=row.raw_location,
        etag=row.etag,
        last_modified=row.last_modified,
        collector_version=row.collector_version,
    )


@dataclasses.dataclass(frozen=True)
class DigestRunReport:  # pylint: disable=too-many-instance-attributes
    """Machine-readable report of one intelligence digest generation pass."""

    digest_id: uuid.UUID | None
    digest_date: str
    status: str
    digest_status: str
    selected_snapshot_count: int
    processed_snapshot_count: int
    failed_snapshot_count: int
    unresolved_snapshot_count: int
    extracted_change_count: int
    claim_count: int
    published: bool
    started_at: datetime
    completed_at: datetime
    failures: list[dict[str, str]] = dataclasses.field(default_factory=list)


def render_report(report: DigestRunReport) -> str:
    """Render report as a deterministic, single line of JSON."""
    payload: dict[str, Any] = {
        "claim_count": report.claim_count,
        "completed_at": report.completed_at.isoformat(),
        "digest_date": report.digest_date,
        "digest_id": str(report.digest_id) if report.digest_id else None,
        "digest_status": report.digest_status,
        "extracted_change_count": report.extracted_change_count,
        "failed_snapshot_count": report.failed_snapshot_count,
        "failures": report.failures,
        "processed_snapshot_count": report.processed_snapshot_count,
        "published": report.published,
        "selected_snapshot_count": report.selected_snapshot_count,
        "started_at": report.started_at.isoformat(),
        "status": report.status,
        "unresolved_snapshot_count": report.unresolved_snapshot_count,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def exit_code_for(report: DigestRunReport) -> int:
    """0 ok/published, 1 review/partial, 2 failed."""
    if report.status == "published":
        return 0
    if report.status in ("partial", "review", "draft"):
        return 1
    return 2


def resolve_window(
    digest_date: date,
    since: str | datetime | None = None,
) -> tuple[datetime, datetime]:
    """Derive [window_start, window_end) for snapshot selection.

    `window_end` is the start of the next calendar day in UTC (midnight).
    `window_start` defaults to 24 hours prior to `window_end` or is parsed from `since`.
    """
    window_end = datetime.combine(digest_date + timedelta(days=1), time.min, tzinfo=UTC)

    if since is None:
        window_start = window_end - timedelta(hours=24)
        return window_start, window_end

    if isinstance(since, datetime):
        window_start = normalize_ordering_timestamp(since)
        if window_start >= window_end:
            raise ValueError(
                f"window_start ({window_start}) must be strictly before window_end ({window_end})"
            )
        return window_start, window_end

    since_str = since.strip()
    match_h = _HOURS_RE.match(since_str)
    if match_h:
        hours = int(match_h.group(1))
        if hours <= 0:
            raise ValueError(f"Lookback duration must be strictly positive (> 0), got: {since!r}")
        window_start = window_end - timedelta(hours=hours)
        return window_start, window_end

    match_d = _DAYS_RE.match(since_str)
    if match_d:
        days = int(match_d.group(1))
        if days <= 0:
            raise ValueError(f"Lookback duration must be strictly positive (> 0), got: {since!r}")
        window_start = window_end - timedelta(days=days)
        return window_start, window_end

    if since_str.isdigit():
        hours = int(since_str)
        if hours <= 0:
            raise ValueError(f"Lookback duration must be strictly positive (> 0), got: {since!r}")
        window_start = window_end - timedelta(hours=hours)
        return window_start, window_end

    parsed_dt = datetime.fromisoformat(since_str)
    if parsed_dt.tzinfo is None:
        parsed_dt = parsed_dt.replace(tzinfo=UTC)
    else:
        parsed_dt = parsed_dt.astimezone(UTC)
    window_start = parsed_dt

    if window_start >= window_end:
        raise ValueError(
            f"window_start ({window_start}) must be strictly before window_end ({window_end})"
        )

    return window_start, window_end


async def select_snapshots_in_window(
    session: AsyncSession,
    *,
    window_start: datetime,
    window_end: datetime,
    limit: int | None = None,
) -> list[tuple[SourceItemRow, DocumentSnapshotRow]]:
    """Select (SourceItemRow, DocumentSnapshotRow) pairs within [window_start, window_end)
    ordered by fetched_at ASC, id ASC."""
    stmt = (
        select(SourceItemRow, DocumentSnapshotRow)
        .join(DocumentSnapshotRow, DocumentSnapshotRow.source_item_id == SourceItemRow.id)
        .where(
            DocumentSnapshotRow.fetched_at >= window_start,
            DocumentSnapshotRow.fetched_at < window_end,
        )
        .order_by(DocumentSnapshotRow.fetched_at.asc(), DocumentSnapshotRow.id.asc())
    )
    if limit is not None:
        if limit <= 0:
            raise ValueError(f"limit must be strictly positive (> 0), got: {limit}")
        stmt = stmt.limit(limit)
    res = await session.execute(stmt)
    return [(r[0], r[1]) for r in res.all()]


async def _preflight(engine: AsyncEngine) -> None:
    """Fail fast if database is misconfigured or unreachable."""
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


def _emit_failure(exc: Exception) -> int:
    LOGGER.error("digest generation could not start: %s", type(exc).__name__)
    print(
        json.dumps(
            {"error": type(exc).__name__, "status": "failed"},
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 2


async def _build_fact_table_from_postgres(
    session_factory: _SessionFactory,
    subjects: list[Subject],
    fields: list[str],
) -> list[FactRow]:
    """Build fact table for cross-subject comparison by querying PostgreSQL current_facts."""
    rows: list[FactRow] = []
    async with session_factory() as session:
        store = PostgresFactStore(session)
        for subj in subjects:
            current_map = await store.read_current_facts(subj, fields)
            for f in fields:
                if f in current_map:
                    _, ef = current_map[f]
                    rows.append(
                        FactRow(
                            subject=subj,
                            field=f,
                            value=ef.value,
                            disclosure_status=ef.disclosure_status,  # type: ignore[arg-type]
                            snapshot_id=ef.snapshot_id,
                        )
                    )
                else:
                    rows.append(FactRow(subject=subj, field=f))
    return rows


async def _resolve_and_extract_item(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    item: SourceItem,
    snapshot: DocumentSnapshot,
    known_subjects: set[Subject],
    alias_table: list[SubjectAlias],
    resolve_llm_call_fn: Callable[[str, str], ResolveLLMResponse] | None,
    extract_call_fn: Callable[[str, str], FactExtractionResponse] | None,
) -> tuple[Subject | None, list[Any]]:
    """Resolve subject and extract facts for a single snapshot."""
    resolution = resolve_deterministic(
        item,
        list(known_subjects),
        alias_table,
        item_text=snapshot.content_text or "",
    )
    subject = resolution.subject
    if subject is None and resolution.method != "ambiguous_multi_subject":
        # "ambiguous_multi_subject" (two or more tracked subjects both
        # phrase-matched, e.g. Codex and ChatGPT) is never sent to the LLM
        # fallback -- the schema allows only one Subject per item, so an
        # LLM pick between two real matches would be a silent false merge,
        # not a resolution. It stays unresolved; see resolve.py's module
        # docstring and graph.py's route_after_classify.
        resolution_llm = resolve_via_llm(
            item,
            resolution.candidate_subjects,
            item_text=snapshot.content_text or "",
            alias_table=alias_table,
            call_fn=resolve_llm_call_fn,
        )
        subject = resolution_llm.subject

    if subject is None:
        return None, []

    facts = extract_facts(subject, snapshot, call_fn=extract_call_fn)
    return subject, facts


async def run_pipeline(  # pylint: disable=too-many-arguments,too-many-locals,too-many-branches,too-many-statements
    *,
    session_factory: _SessionFactory,
    digest_date: date,
    window_start: datetime,
    window_end: datetime,
    limit: int | None = None,
    title: str | None = None,
    alias_table: list[SubjectAlias] | None = None,
    resolve_llm_call_fn: Callable[[str, str], ResolveLLMResponse] | None = None,
    extract_call_fn: Callable[[str, str], FactExtractionResponse] | None = None,
    compare_call_fn: Callable[[str, str], ComparisonResponse] | None = None,
    comparison_fields: list[str] | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> DigestRunReport:
    """The offline-testable persistent intelligence pipeline core.

    Processes selected snapshots through individual transactions (one commit
    per item) and persists the resulting digest aggregate in PostgreSQL.
    """
    started_at = clock()
    batch_detected_at = started_at

    async with session_factory() as session:
        candidates = await select_snapshots_in_window(
            session,
            window_start=window_start,
            window_end=window_end,
            limit=limit,
        )
        subj_res = await session.execute(select(SubjectModel.company, SubjectModel.product))
        known_subjects = {Subject(company=c, product=p) for c, p in subj_res.all()}

    resolved_alias_table = alias_table if alias_table is not None else load_alias_table()
    snapshot_resolver = InMemorySnapshotResolver()
    known_snapshot_ids: set[uuid.UUID] = set()
    all_claims: list[DigestClaim] = []
    all_changes: list[Change] = []
    resolved_subjects: list[Subject] = []
    seen_subjects: set[Subject] = set()
    failed_items: list[dict[str, str]] = []
    unresolved_items: list[uuid.UUID] = []
    processed_count = 0
    change_set_ids: dict[Subject, uuid.UUID] = {}

    for item_row, snapshot_row in candidates:
        try:
            item = to_source_item(item_row)
            snapshot = to_document_snapshot(snapshot_row)
            known_snapshot_ids.add(snapshot.id)
            snapshot_resolver.add(snapshot)

            subject, facts = await _resolve_and_extract_item(
                item,
                snapshot,
                known_subjects,
                resolved_alias_table,
                resolve_llm_call_fn,
                extract_call_fn,
            )
            if subject is None:
                unresolved_items.append(item.id)
                continue

            if subject not in seen_subjects:
                seen_subjects.add(subject)
                resolved_subjects.append(subject)
                known_subjects.add(subject)

            # Per-item transaction boundary (ADR 0002 §13)
            async with session_factory() as session:
                store = PostgresFactStore(session)
                cs_id = get_or_create_change_set_id(change_set_ids, subject)
                changes = await store.detect_and_persist_changes(
                    subject=subject,
                    facts=facts,
                    snapshot_observed_at=snapshot.fetched_at,
                    detected_at=batch_detected_at,
                    extraction_version=1,
                    change_set_id=cs_id,
                )
                if not changes:
                    # Resumable recovery: if no new changes were emitted (e.g. facts were already
                    # confirmed in current_facts by an earlier attempt), recover any committed
                    # changes for this snapshot so they are not lost from the digest.
                    changes = await store.get_changes_for_snapshot(snapshot.id)
                await session.commit()

            for change in changes:
                all_changes.append(change)
                all_claims.append(draft_change_claim(change))
                if change.previous and change.previous.snapshot_id:
                    known_snapshot_ids.add(change.previous.snapshot_id)

            processed_count += 1
        except Exception as exc:  # pylint: disable=broad-exception-caught
            LOGGER.error(
                "intelligence item processing failed item_id=%s snapshot_id=%s",
                item_row.id,
                snapshot_row.id,
                extra={"exception_type": type(exc).__name__},
            )
            failed_items.append(
                {
                    "error": type(exc).__name__,
                    "item_id": str(item_row.id),
                    "snapshot_id": str(snapshot_row.id),
                }
            )

    # Ensure historical cited snapshots are resolvable by snapshot_resolver
    missing_snapshot_ids = [
        sid for sid in known_snapshot_ids if snapshot_resolver.get_content(sid) is None
    ]
    if missing_snapshot_ids:
        async with session_factory() as session:
            missing_stmt = select(DocumentSnapshotRow).where(
                DocumentSnapshotRow.id.in_(missing_snapshot_ids)
            )
            missing_res = await session.execute(missing_stmt)
            for m_row in missing_res.scalars().all():
                snapshot_resolver.add(to_document_snapshot(m_row))

    # Cross-subject comparison pass
    comparison_claim_ids: set[uuid.UUID] = set()
    fields = comparison_fields if comparison_fields is not None else list(COMPARISON_RULES)
    if fields and len(resolved_subjects) >= 2:
        try:
            fact_rows = await _build_fact_table_from_postgres(
                session_factory, resolved_subjects, fields
            )
            comp_claims = compare_subjects(fact_rows, call_fn=compare_call_fn)
            for claim in comp_claims:
                comparison_claim_ids.add(claim.id)
                all_claims.append(claim)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            LOGGER.error(
                "cross-subject comparison failed in persistent runner",
                extra={"exception_type": type(exc).__name__},
            )

    digest = assemble_digest(
        digest_date=digest_date,
        claims=all_claims,
        known_snapshot_ids=known_snapshot_ids,
        snapshot_resolver=snapshot_resolver,
        title=title,
    )
    digest = _never_auto_publish_comparisons(digest, comparison_claim_ids)
    if failed_items:
        digest = digest.model_copy(update={"status": DigestStatus.REVIEW})

    # Digest persistence and publication transaction
    async with session_factory() as session:
        store = PostgresFactStore(session)
        existing_published = await store.get_published_digest_by_date(digest_date)
        if (
            existing_published is not None
            and not failed_items
            and _claims_equivalent(digest.claims, existing_published.claims)
        ):
            LOGGER.info(
                "published digest already exists for %s (id=%s) with equivalent claims; "
                "reusing existing digest on replay",
                digest_date,
                existing_published.id,
            )
            final_digest = existing_published
        else:
            if existing_published is not None:
                LOGGER.warning(
                    "published digest already exists for %s (id=%s) but assembled digest "
                    "has new/differing claims; routing to review",
                    digest_date,
                    existing_published.id,
                )
                digest = digest.model_copy(update={"status": DigestStatus.REVIEW})

            digest_to_persist = (
                digest.model_copy(update={"status": DigestStatus.DRAFT})
                if digest.status == DigestStatus.PUBLISHED
                else digest
            )
            persisted = await store.persist_digest(digest_to_persist)
            if digest.status == DigestStatus.PUBLISHED:
                final_digest = await store.publish_digest(
                    persisted.id,
                    known_snapshot_ids=known_snapshot_ids,
                    snapshot_resolver=snapshot_resolver,
                )
            else:
                final_digest = persisted
            await session.commit()

    completed_at = clock()
    if final_digest.status == DigestStatus.PUBLISHED and not failed_items:
        status = "published"
    elif failed_items:
        status = "partial"
    elif final_digest.status == DigestStatus.REVIEW:
        status = "review"
    else:
        status = "draft"

    return DigestRunReport(
        digest_id=final_digest.id,
        digest_date=digest_date.isoformat(),
        status=status,
        digest_status=final_digest.status.value,
        selected_snapshot_count=len(candidates),
        processed_snapshot_count=processed_count,
        failed_snapshot_count=len(failed_items),
        unresolved_snapshot_count=len(unresolved_items),
        extracted_change_count=len(all_changes),
        claim_count=len(final_digest.claims),
        published=(final_digest.status == DigestStatus.PUBLISHED),
        started_at=started_at,
        completed_at=completed_at,
        failures=failed_items,
    )


async def run_with_real_infrastructure(
    *,
    digest_date: date,
    window_start: datetime,
    window_end: datetime,
    limit: int | None = None,
    title: str | None = None,
) -> DigestRunReport:
    """Build engine and session factory from environment and execute pipeline pass."""
    config = DatabaseConfig.from_env()
    engine = build_engine(config)
    try:
        await _preflight(engine)
        session_factory = build_session_factory(engine)
        return await run_pipeline(
            session_factory=session_factory,
            digest_date=digest_date,
            window_start=window_start,
            window_end=window_end,
            limit=limit,
            title=title,
        )
    finally:
        await engine.dispose()


@dataclasses.dataclass(frozen=True)
class DigestRunEvaluation:
    """Non-gold-reference evaluation metrics scored on a pipeline-produced digest."""

    citation_validity: float
    unsupported_claim_count: int
    duplicate_rate: float


async def evaluate_digest_run(
    digest_id: uuid.UUID,
    session: AsyncSession,
) -> DigestRunEvaluation:
    """Score a persisted digest using non-gold-reference evaluation metrics.

    Evaluates citation_validity, unsupported_claim_count, and duplicate_rate
    against real stored snapshots. Does not compute change_recall since no
    gold reference changes exist for arbitrary pipeline runs.

    Operator/CLI-only helper, not to be exposed via any HTTP route (e.g.
    GET /v1/digests/{id}), and a missing digest raising ValueError here should
    not be reused as an HTTP 500 by any future caller.

    Args:
        digest_id: Unique ID of the persisted digest.
        session: Active asynchronous SQLAlchemy session.

    Returns:
        DigestRunEvaluation containing citation_validity, unsupported_claim_count,
        and duplicate_rate.

    Raises:
        ValueError: If the digest with the given digest_id is not found.
    """
    store = PostgresFactStore(session)
    digest = await store.get_digest_by_id(digest_id)
    if digest is None:
        raise ValueError(f"Digest with id {digest_id} not found.")

    cited_snapshot_ids: set[uuid.UUID] = {
        sid for claim in digest.claims for sid in claim.citation_snapshot_ids
    }

    resolver = InMemorySnapshotResolver()
    known_snapshot_ids: set[uuid.UUID] = set()

    if cited_snapshot_ids:
        stmt = select(DocumentSnapshotRow).where(DocumentSnapshotRow.id.in_(cited_snapshot_ids))
        res = await session.execute(stmt)
        for row in res.scalars().all():
            snap = to_document_snapshot(row)
            resolver.add(snap)
            known_snapshot_ids.add(snap.id)

    return DigestRunEvaluation(
        citation_validity=citation_validity(
            digest,
            known_snapshot_ids,
            snapshot_resolver=resolver,
        ),
        unsupported_claim_count=unsupported_claim_count(
            digest,
            known_snapshot_ids,
            snapshot_resolver=resolver,
        ),
        duplicate_rate=duplicate_rate(digest),
    )


def _positive_int(value: str) -> int:
    """Argparse type validator ensuring an integer is strictly positive (> 0)."""
    try:
        ival = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid integer value: {value!r}") from exc
    if ival <= 0:
        raise argparse.ArgumentTypeError(f"Limit must be strictly positive (> 0), got: {ival}")
    return ival


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="generate-digest",
        description=(
            "Run daily intelligence pipeline to select snapshots within a window, "
            "extract facts, detect changes, and publish an evidence-backed digest."
        ),
    )
    digest_date_selection = parser.add_mutually_exclusive_group()
    digest_date_selection.add_argument(
        "--digest-date",
        default=None,
        help="Target date for the digest (YYYY-MM-DD); defaults to current UTC date",
    )
    digest_date_selection.add_argument(
        "--previous-complete-utc-day",
        action="store_true",
        help=(
            "Set the digest date to the previous completed UTC calendar date "
            "(the last fully elapsed UTC day). Intended for a scheduled run such "
            "as a 06:00 UTC cron so each execution processes a whole, already- "
            "finished day. Mutually exclusive with --digest-date."
        ),
    )
    parser.add_argument(
        "--since",
        default="24h",
        help="Lookback window (e.g. '24h', '48h', or ISO-8601 timestamp; defaults to '24h')",
    )
    parser.add_argument(
        "--limit",
        type=_positive_int,
        default=None,
        help="Maximum snapshots to select and process (must be > 0)",
    )
    parser.add_argument(
        "--title",
        default=None,
        help="Optional custom title for the generated digest",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """`generate-digest` entry point.

    Parses CLI flags, resolves selection window, runs pipeline, emits JSON report,
    and returns 0 (published) / 1 (partial or review) / 2 (failed to start).
    """
    logging.basicConfig(level=logging.INFO)
    args = _parse_args(argv)

    try:
        if args.digest_date:
            target_date = date.fromisoformat(args.digest_date)
        elif args.previous_complete_utc_day:
            target_date = previous_complete_utc_day(_utc_now())
        else:
            target_date = _utc_now().date()
        window_start, window_end = resolve_window(target_date, since=args.since)
    except (ValueError, TypeError) as exc:
        return _emit_failure(exc)

    try:
        report = asyncio.run(
            run_with_real_infrastructure(
                digest_date=target_date,
                window_start=window_start,
                window_end=window_end,
                limit=args.limit,
                title=args.title,
            )
        )
    except (ValueError, OSError, SQLAlchemyError) as exc:
        return _emit_failure(exc)

    print(render_report(report))
    return exit_code_for(report)


if __name__ == "__main__":
    raise SystemExit(main())
