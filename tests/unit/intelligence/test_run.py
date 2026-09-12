"""Unit tests for the daily intelligence runner CLI and helpers."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from ai_daily_digest.ingestion.db.models import DocumentSnapshotRow, SourceItemRow
from ai_daily_digest.intelligence.db.repository import PostgresFactStore
from ai_daily_digest.intelligence.extract_facts import FactCandidate, FactExtractionResponse
from ai_daily_digest.intelligence.resolve import SubjectAlias, load_alias_table
from ai_daily_digest.intelligence.resolve_llm import ResolveLLMResponse
from ai_daily_digest.intelligence.run import (
    DigestRunEvaluation,
    DigestRunReport,
    _claims_equivalent,
    _emit_failure,
    _never_auto_publish_comparisons,
    _parse_args,
    _resolve_and_extract_item,
    evaluate_digest_run,
    exit_code_for,
    main,
    previous_complete_utc_day,
    render_report,
    resolve_window,
    run_pipeline,
    select_snapshots_in_window,
    to_document_snapshot,
    to_source_item,
)
from ai_daily_digest.shared.ids import new_id
from ai_daily_digest.shared.schemas import (
    Change,
    Digest,
    DigestClaim,
    DigestStatus,
    DocumentSnapshot,
    ExtractedFact,
    FactObservation,
    SourceItem,
    Subject,
)
from tests.uuid_samples import (
    CLAIM_1,
    CLAIM_2,
    DIGEST_1,
    ITEM_1,
    SNAPSHOT_1,
    SNAPSHOT_MISSING,
)


def test_resolve_window_defaults() -> None:
    target_date = date(2026, 9, 7)
    window_start, window_end = resolve_window(target_date)

    expected_end = datetime(2026, 9, 8, 0, 0, 0, tzinfo=UTC)
    expected_start = datetime(2026, 9, 7, 0, 0, 0, tzinfo=UTC)
    assert window_end == expected_end
    assert window_start == expected_start


def test_resolve_window_custom_hours() -> None:
    target_date = date(2026, 9, 7)
    window_start, window_end = resolve_window(target_date, since="48h")

    expected_end = datetime(2026, 9, 8, 0, 0, 0, tzinfo=UTC)
    expected_start = expected_end - timedelta(hours=48)
    assert window_end == expected_end
    assert window_start == expected_start


def test_resolve_window_custom_days() -> None:
    target_date = date(2026, 9, 7)
    window_start, window_end = resolve_window(target_date, since="3d")

    expected_end = datetime(2026, 9, 8, 0, 0, 0, tzinfo=UTC)
    expected_start = expected_end - timedelta(days=3)
    assert window_end == expected_end
    assert window_start == expected_start


def test_resolve_window_custom_iso() -> None:
    target_date = date(2026, 9, 7)
    window_start, window_end = resolve_window(target_date, since="2026-09-05T12:00:00Z")

    expected_end = datetime(2026, 9, 8, 0, 0, 0, tzinfo=UTC)
    expected_start = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)
    assert window_end == expected_end
    assert window_start == expected_start


def test_resolve_window_rejects_inverted_range() -> None:
    target_date = date(2026, 9, 7)
    with pytest.raises(ValueError, match="strictly before"):
        resolve_window(target_date, since="2026-09-10T00:00:00Z")


def test_parse_args_defaults() -> None:
    args = _parse_args([])
    assert args.digest_date is None
    assert args.previous_complete_utc_day is False
    assert args.since == "24h"
    assert args.limit is None
    assert args.title is None


def test_parse_args_explicit() -> None:
    args = _parse_args(
        [
            "--digest-date",
            "2026-09-04",
            "--since",
            "12h",
            "--limit",
            "10",
            "--title",
            "Custom Title",
        ]
    )
    assert args.digest_date == "2026-09-04"
    assert args.previous_complete_utc_day is False
    assert args.since == "12h"
    assert args.limit == 10
    assert args.title == "Custom Title"


def test_parse_args_previous_complete_utc_day_flag() -> None:
    args = _parse_args(["--previous-complete-utc-day"])
    assert args.previous_complete_utc_day is True
    assert args.digest_date is None


def test_parse_args_rejects_digest_date_and_previous_complete_utc_day_together() -> None:
    with pytest.raises(SystemExit):
        _parse_args(["--digest-date", "2026-09-09", "--previous-complete-utc-day"])


def test_previous_complete_utc_day_at_0600_utc_selects_prior_calendar_day() -> None:
    execution_time = datetime(2026, 9, 10, 6, 0, 0, tzinfo=UTC)
    assert previous_complete_utc_day(execution_time) == date(2026, 9, 9)


def test_previous_complete_utc_day_window_is_the_exact_prior_utc_day() -> None:
    execution_time = datetime(2026, 9, 10, 6, 0, 0, tzinfo=UTC)
    digest_date = previous_complete_utc_day(execution_time)
    window_start, window_end = resolve_window(digest_date, since="24h")

    assert window_start == datetime(2026, 9, 9, 0, 0, 0, tzinfo=UTC)
    assert window_end == datetime(2026, 9, 10, 0, 0, 0, tzinfo=UTC)


def test_previous_complete_utc_day_consecutive_0600_runs_have_adjacent_windows() -> None:
    first_run = datetime(2026, 9, 10, 6, 0, 0, tzinfo=UTC)
    second_run = datetime(2026, 9, 11, 6, 0, 0, tzinfo=UTC)

    _, first_window_end = resolve_window(previous_complete_utc_day(first_run), since="24h")
    second_window_start, _ = resolve_window(previous_complete_utc_day(second_run), since="24h")

    # Adjacent half-open windows: no gap, no overlap.
    assert first_window_end == second_window_start
    assert first_window_end == datetime(2026, 9, 10, 0, 0, 0, tzinfo=UTC)


def test_previous_complete_utc_day_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        previous_complete_utc_day(datetime(2026, 9, 10, 6, 0, 0))


def test_previous_complete_utc_day_converts_non_utc_aware_datetime() -> None:
    # 2026-09-10 02:00 at +05:00 is 2026-09-09 21:00 UTC, so the last completed
    # UTC day is 2026-09-08.
    plus_five = timezone(timedelta(hours=5))
    execution_time = datetime(2026, 9, 10, 2, 0, 0, tzinfo=plus_five)
    assert previous_complete_utc_day(execution_time) == date(2026, 9, 8)


def test_main_previous_complete_utc_day_flag_selects_prior_day_window(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured_kwargs: dict[str, Any] = {}

    async def _capture_run(**kwargs: Any) -> DigestRunReport:
        captured_kwargs.update(kwargs)
        return DigestRunReport(
            digest_id=None,
            digest_date=kwargs["digest_date"].isoformat(),
            status="review",
            digest_status="review",
            selected_snapshot_count=0,
            processed_snapshot_count=0,
            failed_snapshot_count=0,
            unresolved_snapshot_count=0,
            extracted_change_count=0,
            claim_count=0,
            published=False,
            started_at=datetime(2026, 9, 10, 6, 0, 0, tzinfo=UTC),
            completed_at=datetime(2026, 9, 10, 6, 0, 0, tzinfo=UTC),
        )

    monkeypatch.setattr(
        "ai_daily_digest.intelligence.run._utc_now",
        lambda: datetime(2026, 9, 10, 6, 0, 0, tzinfo=UTC),
    )
    monkeypatch.setattr(
        "ai_daily_digest.intelligence.run.run_with_real_infrastructure",
        _capture_run,
    )

    code = main(["--previous-complete-utc-day", "--since", "24h", "--limit", "5"])

    assert code == 1  # review
    assert captured_kwargs["digest_date"] == date(2026, 9, 9)
    assert captured_kwargs["window_start"] == datetime(2026, 9, 9, 0, 0, 0, tzinfo=UTC)
    assert captured_kwargs["window_end"] == datetime(2026, 9, 10, 0, 0, 0, tzinfo=UTC)
    parsed = json.loads(capsys.readouterr().out.strip())
    assert parsed["digest_date"] == "2026-09-09"


def test_main_default_date_behaviour_unchanged(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured_kwargs: dict[str, Any] = {}

    async def _capture_run(**kwargs: Any) -> DigestRunReport:
        captured_kwargs.update(kwargs)
        return DigestRunReport(
            digest_id=None,
            digest_date=kwargs["digest_date"].isoformat(),
            status="review",
            digest_status="review",
            selected_snapshot_count=0,
            processed_snapshot_count=0,
            failed_snapshot_count=0,
            unresolved_snapshot_count=0,
            extracted_change_count=0,
            claim_count=0,
            published=False,
            started_at=datetime(2026, 9, 10, 6, 0, 0, tzinfo=UTC),
            completed_at=datetime(2026, 9, 10, 6, 0, 0, tzinfo=UTC),
        )

    monkeypatch.setattr(
        "ai_daily_digest.intelligence.run._utc_now",
        lambda: datetime(2026, 9, 10, 6, 0, 0, tzinfo=UTC),
    )
    monkeypatch.setattr(
        "ai_daily_digest.intelligence.run.run_with_real_infrastructure",
        _capture_run,
    )

    code = main(["--since", "24h"])

    assert code == 1
    # Unchanged default: digest date is *today* in UTC, not the prior day.
    assert captured_kwargs["digest_date"] == date(2026, 9, 10)
    assert captured_kwargs["window_start"] == datetime(2026, 9, 10, 0, 0, 0, tzinfo=UTC)
    assert captured_kwargs["window_end"] == datetime(2026, 9, 11, 0, 0, 0, tzinfo=UTC)


def test_render_report_json() -> None:
    digest_id = new_id()
    now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
    report = DigestRunReport(
        digest_id=digest_id,
        digest_date="2026-09-07",
        status="published",
        digest_status="published",
        selected_snapshot_count=5,
        processed_snapshot_count=5,
        failed_snapshot_count=0,
        unresolved_snapshot_count=0,
        extracted_change_count=2,
        claim_count=2,
        published=True,
        started_at=now,
        completed_at=now + timedelta(seconds=2),
        failures=[],
    )

    rendered = render_report(report)
    parsed = json.loads(rendered)

    assert parsed["digest_id"] == str(digest_id)
    assert parsed["status"] == "published"
    assert parsed["selected_snapshot_count"] == 5
    assert parsed["extracted_change_count"] == 2
    assert parsed["failures"] == []
    # Assert single line
    assert "\n" not in rendered


def test_exit_codes() -> None:
    now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)

    def _base_report(**kwargs: Any) -> DigestRunReport:
        defaults: dict[str, Any] = {
            "digest_id": None,
            "digest_date": "2026-09-07",
            "status": "draft",
            "digest_status": "draft",
            "selected_snapshot_count": 0,
            "processed_snapshot_count": 0,
            "failed_snapshot_count": 0,
            "unresolved_snapshot_count": 0,
            "extracted_change_count": 0,
            "claim_count": 0,
            "published": False,
            "started_at": now,
            "completed_at": now,
            "failures": [],
        }
        defaults.update(kwargs)
        return DigestRunReport(**defaults)

    # 1. Published -> 0
    assert exit_code_for(_base_report(status="published", published=True)) == 0
    assert (
        exit_code_for(
            _base_report(
                status="published",
                published=True,
                selected_snapshot_count=2,
                processed_snapshot_count=2,
                claim_count=1,
            )
        )
        == 0
    )

    # 2. Clean zero-change with processed snapshots -> 0
    assert (
        exit_code_for(
            _base_report(
                status="draft",
                digest_status="draft",
                selected_snapshot_count=2,
                processed_snapshot_count=2,
                extracted_change_count=0,
                claim_count=0,
                failed_snapshot_count=0,
                unresolved_snapshot_count=0,
                failures=[],
                published=False,
            )
        )
        == 0
    )

    # 3. Zero selected snapshots -> 1
    assert (
        exit_code_for(
            _base_report(
                status="draft",
                selected_snapshot_count=0,
                processed_snapshot_count=0,
            )
        )
        == 1
    )

    # 4. Partial -> 1
    assert (
        exit_code_for(
            _base_report(
                status="partial",
                selected_snapshot_count=2,
                processed_snapshot_count=1,
                failed_snapshot_count=1,
                failures=[{"snapshot_id": "s1", "error": "timeout"}],
            )
        )
        == 1
    )

    # 5. Review -> 1
    assert (
        exit_code_for(
            _base_report(
                status="review",
                selected_snapshot_count=2,
                processed_snapshot_count=2,
                claim_count=1,
            )
        )
        == 1
    )

    # 6. Unresolved snapshots -> 1
    assert (
        exit_code_for(
            _base_report(
                status="draft",
                selected_snapshot_count=2,
                processed_snapshot_count=2,
                unresolved_snapshot_count=1,
            )
        )
        == 1
    )

    # 7. Failed snapshots -> 1
    assert (
        exit_code_for(
            _base_report(
                status="draft",
                selected_snapshot_count=2,
                processed_snapshot_count=2,
                failed_snapshot_count=1,
            )
        )
        == 1
    )

    # 8. Explicit failed status -> 2
    assert exit_code_for(_base_report(status="failed", digest_status="failed")) == 2

    # 9. Processed/selected count mismatch -> 1
    assert (
        exit_code_for(
            _base_report(
                status="draft",
                selected_snapshot_count=3,
                processed_snapshot_count=2,
            )
        )
        == 1
    )

    # 10. Unexpected claims or changes in a draft -> 1
    assert (
        exit_code_for(
            _base_report(
                status="draft",
                selected_snapshot_count=2,
                processed_snapshot_count=2,
                claim_count=1,
            )
        )
        == 1
    )
    assert (
        exit_code_for(
            _base_report(
                status="draft",
                selected_snapshot_count=2,
                processed_snapshot_count=2,
                extracted_change_count=1,
            )
        )
        == 1
    )
    assert (
        exit_code_for(
            _base_report(
                status="draft",
                selected_snapshot_count=2,
                processed_snapshot_count=2,
                failures=[{"snapshot_id": "s1", "error": "err"}],
            )
        )
        == 1
    )
    assert (
        exit_code_for(
            _base_report(
                status="draft",
                selected_snapshot_count=2,
                processed_snapshot_count=2,
                published=True,
            )
        )
        == 1
    )


def test_never_auto_publish_comparisons() -> None:
    comp_id = new_id()
    digest = Digest(
        id=new_id(),
        digest_date=date(2026, 9, 7),
        status=DigestStatus.PUBLISHED,
        title="Published Digest",
        claims=[DigestClaim(id=comp_id, text="Comparison claim")],
    )

    demoted = _never_auto_publish_comparisons(digest, {comp_id})
    assert demoted.status == DigestStatus.REVIEW

    # Unaffected when no comparison claims
    kept = _never_auto_publish_comparisons(digest, set())
    assert kept.status == DigestStatus.PUBLISHED


def test_resolve_window_invalid_unit() -> None:
    target_date = date(2026, 9, 7)
    with pytest.raises(ValueError, match="Invalid isoformat string"):
        resolve_window(target_date, since="invalid_since")


@pytest.mark.parametrize("invalid_since", ["0h", "0d", "0"])
def test_resolve_window_rejects_zero_or_negative_duration(invalid_since: str) -> None:
    target_date = date(2026, 9, 7)
    with pytest.raises(ValueError, match="Lookback duration must be strictly positive"):
        resolve_window(target_date, since=invalid_since)


@pytest.mark.parametrize("invalid_limit", ["0", "-1", "not_a_number"])
def test_parse_args_invalid_limit(invalid_limit: str) -> None:
    with pytest.raises(SystemExit) as exc_info:
        _parse_args(["--limit", invalid_limit])
    assert exc_info.value.code == 2


@pytest.mark.asyncio
async def test_select_snapshots_in_window_rejects_non_positive_limit() -> None:
    session = AsyncMock()
    now = datetime(2026, 9, 7, 0, 0, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="limit must be strictly positive"):
        await select_snapshots_in_window(
            session, window_start=now, window_end=now + timedelta(days=1), limit=0
        )
    with pytest.raises(ValueError, match="limit must be strictly positive"):
        await select_snapshots_in_window(
            session, window_start=now, window_end=now + timedelta(days=1), limit=-5
        )


def test_emit_failure(capsys: pytest.CaptureFixture[str]) -> None:
    code = _emit_failure(ValueError("Bad configuration"))
    assert code == 2
    captured = capsys.readouterr()
    parsed = json.loads(captured.out.strip())
    assert parsed == {"error": "ValueError", "status": "failed"}


def test_main_invalid_digest_date(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--digest-date", "invalid-date"])
    assert code == 2
    captured = capsys.readouterr()
    parsed = json.loads(captured.out.strip())
    assert parsed["status"] == "failed"
    assert parsed["error"] == "ValueError"


def test_main_invalid_since(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--since", "invalid-since-format"])
    assert code == 2
    captured = capsys.readouterr()
    parsed = json.loads(captured.out.strip())
    assert parsed["status"] == "failed"
    assert parsed["error"] == "ValueError"


def test_main_rejects_zero_duration_since(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--since", "0h"])
    assert code == 2
    captured = capsys.readouterr()
    parsed = json.loads(captured.out.strip())
    assert parsed["status"] == "failed"
    assert parsed["error"] == "ValueError"


def test_main_runtime_exception_handling(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def _failing_run(**_kwargs: Any) -> Any:
        raise OSError("Simulated connection failure")

    monkeypatch.setattr(
        "ai_daily_digest.intelligence.run.run_with_real_infrastructure",
        _failing_run,
    )
    code = main(["--since", "24h"])
    assert code == 2
    captured = capsys.readouterr()
    parsed = json.loads(captured.out.strip())
    assert parsed == {"error": "OSError", "status": "failed"}


def test_to_source_item_conversion() -> None:
    item_id = new_id()
    now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
    row = SourceItemRow(
        id=item_id,
        dedupe_key=f"sha256:{item_id}",
        source_id="openai_news",
        publisher="OpenAI",
        title="Sample Title",
        canonical_url="https://openai.example.com/item",
        first_fetched_at=now,
        authors=["Author A"],
        tags=["ai", "release"],
        language="en",
    )
    item = to_source_item(row)
    assert item.id == item_id
    assert item.publisher == "OpenAI"
    assert str(item.canonical_url) == "https://openai.example.com/item"
    assert item.authors == ["Author A"]
    assert item.tags == ["ai", "release"]
    assert item.language == "en"


def test_to_document_snapshot_conversion() -> None:
    snap_id = new_id()
    item_id = new_id()
    now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
    row = DocumentSnapshotRow(
        id=snap_id,
        source_item_id=item_id,
        fetched_at=now,
        content_hash="abc123hash",
        content_text="Sample text",
        collector_version="1.0.0",
    )
    snapshot = to_document_snapshot(row)
    assert snapshot.id == snap_id
    assert snapshot.source_item_id == item_id
    assert snapshot.content_text == "Sample text"
    assert snapshot.collector_version == "1.0.0"


@pytest.mark.asyncio
async def test_run_pipeline_persists_as_draft_under_pr84_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify run_pipeline demotes assembled published digest to draft for persist_digest,
    then uses publish_digest, satisfying PR #84 publication gate."""
    target_date = date(2026, 9, 7)
    now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)

    async def _mock_select(*_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr("ai_daily_digest.intelligence.run.select_snapshots_in_window", _mock_select)

    assembled_id = new_id()
    assembled_digest = Digest(
        id=assembled_id,
        digest_date=target_date,
        status=DigestStatus.PUBLISHED,
        title="Assembled Published Digest",
        claims=[],
    )
    monkeypatch.setattr(
        "ai_daily_digest.intelligence.run.assemble_digest",
        lambda **_kwargs: assembled_digest,
    )

    persisted_statuses: list[DigestStatus] = []
    published_calls: list[uuid.UUID] = []

    class MockPR84Store:
        def __init__(self, _session: Any) -> None:
            pass

        async def persist_digest(self, digest: Digest) -> Digest:
            if digest.status == DigestStatus.PUBLISHED:
                raise ValueError(
                    "Cannot persist a new digest with status='published' via "
                    "persist_digest(); use publish_digest() to validate and "
                    "transition to published"
                )
            persisted_statuses.append(digest.status)
            return digest

        async def publish_digest(
            self,
            digest_id: uuid.UUID,
            *,
            known_snapshot_ids: Any,
            snapshot_resolver: Any,
        ) -> Digest:
            del known_snapshot_ids, snapshot_resolver
            published_calls.append(digest_id)
            return assembled_digest

        async def get_published_digest_by_date(self, *args: Any, **kwargs: Any) -> Digest | None:
            del args, kwargs
            return None

    monkeypatch.setattr("ai_daily_digest.intelligence.run.PostgresFactStore", MockPR84Store)

    class MockResult:
        def all(self) -> list[Any]:
            return []

    class MockSession:
        async def execute(self, _stmt: Any) -> Any:
            return MockResult()

        async def commit(self) -> None:
            pass

    @asynccontextmanager
    async def mock_session_factory() -> AsyncIterator[MockSession]:
        yield MockSession()

    report = await run_pipeline(
        session_factory=cast(Any, mock_session_factory),
        digest_date=target_date,
        window_start=now - timedelta(hours=24),
        window_end=now,
    )

    assert persisted_statuses == [DigestStatus.DRAFT]
    assert published_calls == [assembled_id]
    assert report.published is True
    assert report.status == "published"


@pytest.mark.asyncio
async def test_run_pipeline_reuses_change_set_id_for_same_subject(  # pylint: disable=too-many-locals
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Snapshots for the same subject in one run must reuse the same change_set_id (ADR 0007)."""
    now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
    subj_a = Subject(company="Anthropic", product="Claude 3.5")
    subj_b = Subject(company="Google", product="Gemini 1.5")

    item1 = SourceItemRow(
        id=new_id(),
        dedupe_key="k1",
        source_id="s1",
        publisher="Anthropic",
        title="Claude update 1",
        canonical_url="https://example.com/1",
        first_fetched_at=now,
        language="en",
    )
    snap1 = DocumentSnapshotRow(
        id=new_id(),
        source_item_id=item1.id,
        content_hash="h1",
        fetched_at=now,
        content_text="Claude text 1",
    )
    item2 = SourceItemRow(
        id=new_id(),
        dedupe_key="k2",
        source_id="s1",
        publisher="Anthropic",
        title="Claude update 2",
        canonical_url="https://example.com/2",
        first_fetched_at=now + timedelta(minutes=5),
        language="en",
    )
    snap2 = DocumentSnapshotRow(
        id=new_id(),
        source_item_id=item2.id,
        content_hash="h2",
        fetched_at=now + timedelta(minutes=5),
        content_text="Claude text 2",
    )
    item3 = SourceItemRow(
        id=new_id(),
        dedupe_key="k3",
        source_id="s2",
        publisher="Google",
        title="Gemini update",
        canonical_url="https://example.com/3",
        first_fetched_at=now + timedelta(minutes=10),
        language="en",
    )
    snap3 = DocumentSnapshotRow(
        id=new_id(),
        source_item_id=item3.id,
        content_hash="h3",
        fetched_at=now + timedelta(minutes=10),
        content_text="Gemini text",
    )

    candidates = [(item1, snap1), (item2, snap2), (item3, snap3)]
    monkeypatch.setattr(
        "ai_daily_digest.intelligence.run.select_snapshots_in_window",
        AsyncMock(return_value=candidates),
    )

    async def _mock_resolve_and_extract(
        item: Any, *args: Any, **kwargs: Any
    ) -> tuple[Subject | None, list[Any]]:
        del args, kwargs
        if item.publisher == "Anthropic":
            return subj_a, []
        return subj_b, []

    monkeypatch.setattr(
        "ai_daily_digest.intelligence.run._resolve_and_extract_item",
        _mock_resolve_and_extract,
    )

    recorded_change_set_ids: list[tuple[Subject, uuid.UUID]] = []

    class MockStore:
        def __init__(self, _session: Any) -> None:
            pass

        async def detect_and_persist_changes(
            self,
            *,
            subject: Subject,
            change_set_id: uuid.UUID | None = None,
            **_kwargs: Any,
        ) -> list[Any]:
            assert change_set_id is not None
            recorded_change_set_ids.append((subject, change_set_id))
            return []

        async def persist_digest(self, digest: Digest) -> Digest:
            return digest

        async def get_published_digest_by_date(self, *args: Any, **kwargs: Any) -> Digest | None:
            del args, kwargs
            return None

    monkeypatch.setattr("ai_daily_digest.intelligence.run.PostgresFactStore", MockStore)

    class MockResult:
        def all(self) -> list[Any]:
            return []

    class MockSession:
        async def execute(self, _stmt: Any) -> Any:
            return MockResult()

        async def commit(self) -> None:
            pass

    @asynccontextmanager
    async def mock_session_factory() -> AsyncIterator[MockSession]:
        yield MockSession()

    await run_pipeline(
        session_factory=cast(Any, mock_session_factory),
        digest_date=date(2026, 9, 7),
        window_start=now - timedelta(hours=24),
        window_end=now,
    )

    assert len(recorded_change_set_ids) == 3
    # Two snapshots for subj_a must share the exact same change_set_id
    assert recorded_change_set_ids[0][0] == subj_a
    assert recorded_change_set_ids[1][0] == subj_a
    assert recorded_change_set_ids[0][1] == recorded_change_set_ids[1][1]

    # Different subject must have a different change_set_id
    assert recorded_change_set_ids[2][0] == subj_b
    assert recorded_change_set_ids[2][1] != recorded_change_set_ids[0][1]


@pytest.mark.asyncio
async def test_failed_items_forces_digest_to_review(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
    item1 = SourceItemRow(
        id=new_id(),
        dedupe_key="k1",
        source_id="s1",
        publisher="OpenAI",
        title="Valid Item",
        canonical_url="https://example.com/1",
        first_fetched_at=now,
        language="en",
    )
    snap1 = DocumentSnapshotRow(
        id=new_id(),
        source_item_id=item1.id,
        content_hash="h1",
        fetched_at=now,
        content_text="Valid content",
    )
    item2 = SourceItemRow(
        id=new_id(),
        dedupe_key="k2",
        source_id="s1",
        publisher="OpenAI",
        title="Failing Item",
        canonical_url="https://example.com/2",
        first_fetched_at=now + timedelta(minutes=5),
        language="en",
    )
    snap2 = DocumentSnapshotRow(
        id=new_id(),
        source_item_id=item2.id,
        content_hash="h2",
        fetched_at=now + timedelta(minutes=5),
        content_text="Failing content",
    )

    monkeypatch.setattr(
        "ai_daily_digest.intelligence.run.select_snapshots_in_window",
        AsyncMock(return_value=[(item1, snap1), (item2, snap2)]),
    )

    async def _mock_resolve_and_extract(
        item: Any, *args: Any, **kwargs: Any
    ) -> tuple[Subject | None, list[Any]]:
        del args, kwargs
        if item.title == "Failing Item":
            raise RuntimeError("Extraction failed on item2")
        return Subject(company="OpenAI", product="GPT-4o"), []

    monkeypatch.setattr(
        "ai_daily_digest.intelligence.run._resolve_and_extract_item",
        _mock_resolve_and_extract,
    )

    persisted_digests: list[Digest] = []

    class MockStore:
        def __init__(self, _session: Any) -> None:
            pass

        async def detect_and_persist_changes(self, *args: Any, **kwargs: Any) -> list[Any]:
            del args, kwargs
            return []

        async def get_changes_for_snapshot(self, *args: Any, **kwargs: Any) -> list[Any]:
            del args, kwargs
            return []

        async def persist_digest(self, digest: Digest) -> Digest:
            persisted_digests.append(digest)
            return digest

        async def publish_digest(self, *args: Any, **kwargs: Any) -> Digest:
            raise AssertionError("publish_digest should never be called when items failed")

        async def get_published_digest_by_date(self, *args: Any, **kwargs: Any) -> Digest | None:
            del args, kwargs
            return None

    monkeypatch.setattr("ai_daily_digest.intelligence.run.PostgresFactStore", MockStore)

    # Mock assemble_digest to return a digest with PUBLISHED status
    dummy_digest = Digest(
        id=new_id(),
        digest_date=date(2026, 9, 7),
        status=DigestStatus.PUBLISHED,
        title="AI Daily Digest",
        claims=[],
    )
    monkeypatch.setattr(
        "ai_daily_digest.intelligence.run.assemble_digest",
        lambda *args, **kwargs: dummy_digest,
    )

    class MockResult:
        def all(self) -> list[Any]:
            return []

    class MockSession:
        async def execute(self, _stmt: Any) -> Any:
            return MockResult()

        async def commit(self) -> None:
            pass

    @asynccontextmanager
    async def mock_session_factory() -> AsyncIterator[MockSession]:
        yield MockSession()

    report = await run_pipeline(
        session_factory=cast(Any, mock_session_factory),
        digest_date=date(2026, 9, 7),
        window_start=now - timedelta(hours=24),
        window_end=now,
    )

    # Must be forced to REVIEW and unpublished
    assert len(persisted_digests) == 1
    assert persisted_digests[0].status == DigestStatus.REVIEW
    assert report.digest_status == "review"
    assert report.published is False
    assert report.status == "partial"
    assert report.failed_snapshot_count == 1


@pytest.mark.asyncio
async def test_conversion_and_resolver_failure_isolated_to_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
    # Item 1 has invalid canonical_url which fails pydantic HttpUrl in to_source_item
    item1 = SourceItemRow(
        id=new_id(),
        dedupe_key="k1",
        source_id="s1",
        publisher="OpenAI",
        title="Invalid URL Item",
        canonical_url="not-a-valid-url",
        first_fetched_at=now,
        language="en",
    )
    snap1 = DocumentSnapshotRow(
        id=new_id(),
        source_item_id=item1.id,
        content_hash="h1",
        fetched_at=now,
        content_text="Text 1",
    )
    # Item 2 is valid
    item2 = SourceItemRow(
        id=new_id(),
        dedupe_key="k2",
        source_id="s1",
        publisher="OpenAI",
        title="Valid Item 2",
        canonical_url="https://example.com/2",
        first_fetched_at=now + timedelta(minutes=5),
        language="en",
    )
    snap2 = DocumentSnapshotRow(
        id=new_id(),
        source_item_id=item2.id,
        content_hash="h2",
        fetched_at=now + timedelta(minutes=5),
        content_text="Text 2",
    )

    monkeypatch.setattr(
        "ai_daily_digest.intelligence.run.select_snapshots_in_window",
        AsyncMock(return_value=[(item1, snap1), (item2, snap2)]),
    )

    processed_titles: list[str] = []

    async def _mock_resolve_and_extract(
        item: Any, *args: Any, **kwargs: Any
    ) -> tuple[Subject | None, list[Any]]:
        del args, kwargs
        processed_titles.append(item.title)
        return Subject(company="OpenAI", product="GPT-4o"), []

    monkeypatch.setattr(
        "ai_daily_digest.intelligence.run._resolve_and_extract_item",
        _mock_resolve_and_extract,
    )

    class MockStore:
        def __init__(self, _session: Any) -> None:
            pass

        async def detect_and_persist_changes(self, *args: Any, **kwargs: Any) -> list[Any]:
            del args, kwargs
            return []

        async def get_changes_for_snapshot(self, *args: Any, **kwargs: Any) -> list[Any]:
            del args, kwargs
            return []

        async def persist_digest(self, digest: Digest) -> Digest:
            return digest

        async def get_published_digest_by_date(self, *args: Any, **kwargs: Any) -> Digest | None:
            del args, kwargs
            return None

    monkeypatch.setattr("ai_daily_digest.intelligence.run.PostgresFactStore", MockStore)

    class MockResult:
        def all(self) -> list[Any]:
            return []

    class MockSession:
        async def execute(self, _stmt: Any) -> Any:
            return MockResult()

        async def commit(self) -> None:
            pass

    @asynccontextmanager
    async def mock_session_factory() -> AsyncIterator[MockSession]:
        yield MockSession()

    report = await run_pipeline(
        session_factory=cast(Any, mock_session_factory),
        digest_date=date(2026, 9, 7),
        window_start=now - timedelta(hours=24),
        window_end=now,
    )

    # Item 1 failed in to_source_item, but item 2 was processed!
    assert report.failed_snapshot_count == 1
    assert report.processed_snapshot_count == 1
    assert processed_titles == ["Valid Item 2"]
    assert report.failures[0]["item_id"] == str(item1.id)
    assert report.failures[0]["error"] == "ValidationError"


@pytest.mark.asyncio
async def test_item_exception_does_not_leak_message_or_traceback_to_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Per-item exceptions log only the exception type, redacting raw message and traceback."""
    now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
    item = SourceItemRow(
        id=new_id(),
        dedupe_key="k1",
        source_id="s1",
        publisher="OpenAI",
        title="Valid Item",
        canonical_url="https://example.com/item",
        first_fetched_at=now,
        language="en",
    )
    snap = DocumentSnapshotRow(
        id=new_id(),
        source_item_id=item.id,
        content_hash="h1",
        fetched_at=now,
        content_text="Sample text",
    )

    monkeypatch.setattr(
        "ai_daily_digest.intelligence.run.select_snapshots_in_window",
        AsyncMock(return_value=[(item, snap)]),
    )

    sentinel_marker = "private-leak-secret-content-xyz123"

    async def _failing_resolve(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"boom with sensitive message: {sentinel_marker}")

    monkeypatch.setattr(
        "ai_daily_digest.intelligence.run._resolve_and_extract_item",
        _failing_resolve,
    )

    class MockStore:
        def __init__(self, _session: Any) -> None:
            pass

        async def get_published_digest_by_date(self, *args: Any, **kwargs: Any) -> Digest | None:
            del args, kwargs
            return None

        async def persist_digest(self, digest: Digest) -> Digest:
            return digest

    monkeypatch.setattr("ai_daily_digest.intelligence.run.PostgresFactStore", MockStore)

    class MockResult:
        def all(self) -> list[Any]:
            return []

    class MockSession:
        async def execute(self, _stmt: Any) -> Any:
            return MockResult()

        async def commit(self) -> None:
            pass

    @asynccontextmanager
    async def mock_session_factory() -> AsyncIterator[MockSession]:
        yield MockSession()

    with caplog.at_level(logging.INFO):
        report = await run_pipeline(
            session_factory=cast(Any, mock_session_factory),
            digest_date=date(2026, 9, 7),
            window_start=now - timedelta(hours=24),
            window_end=now,
        )

    assert report.failed_snapshot_count == 1
    assert sentinel_marker not in caplog.text
    err_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(err_records) == 1
    assert getattr(err_records[0], "exception_type", None) == "RuntimeError"
    assert err_records[0].exc_info is None


@pytest.mark.asyncio
async def test_comparison_exception_does_not_leak_message_or_traceback_to_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Comparison exceptions log only the exception type, redacting raw message and traceback."""
    now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
    item1 = SourceItemRow(
        id=new_id(),
        dedupe_key="k1",
        source_id="s1",
        publisher="OpenAI",
        title="Item 1",
        canonical_url="https://example.com/1",
        first_fetched_at=now,
        language="en",
    )
    snap1 = DocumentSnapshotRow(
        id=new_id(),
        source_item_id=item1.id,
        content_hash="h1",
        fetched_at=now,
        content_text="Text 1",
    )
    item2 = SourceItemRow(
        id=new_id(),
        dedupe_key="k2",
        source_id="s1",
        publisher="Anthropic",
        title="Item 2",
        canonical_url="https://example.com/2",
        first_fetched_at=now,
        language="en",
    )
    snap2 = DocumentSnapshotRow(
        id=new_id(),
        source_item_id=item2.id,
        content_hash="h2",
        fetched_at=now,
        content_text="Text 2",
    )

    monkeypatch.setattr(
        "ai_daily_digest.intelligence.run.select_snapshots_in_window",
        AsyncMock(return_value=[(item1, snap1), (item2, snap2)]),
    )

    async def _mock_resolve(
        item: Any, *_args: Any, **_kwargs: Any
    ) -> tuple[Subject | None, list[Any]]:
        company = "OpenAI" if item.publisher == "OpenAI" else "Anthropic"
        return Subject(company=company, product="Model"), []

    monkeypatch.setattr(
        "ai_daily_digest.intelligence.run._resolve_and_extract_item",
        _mock_resolve,
    )

    sentinel_marker = "sensitive-comparison-secret-999"

    async def _failing_build_facts(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"comparison failed with sentinel: {sentinel_marker}")

    monkeypatch.setattr(
        "ai_daily_digest.intelligence.run._build_fact_table_from_postgres",
        _failing_build_facts,
    )

    class MockStore:
        def __init__(self, _session: Any) -> None:
            pass

        async def detect_and_persist_changes(self, *args: Any, **kwargs: Any) -> list[Any]:
            del args, kwargs
            return []

        async def get_changes_for_snapshot(self, *args: Any, **kwargs: Any) -> list[Any]:
            del args, kwargs
            return []

        async def get_published_digest_by_date(self, *args: Any, **kwargs: Any) -> Digest | None:
            del args, kwargs
            return None

        async def persist_digest(self, digest: Digest) -> Digest:
            return digest

    monkeypatch.setattr("ai_daily_digest.intelligence.run.PostgresFactStore", MockStore)

    class MockResult:
        def all(self) -> list[Any]:
            return []

    class MockSession:
        async def execute(self, _stmt: Any) -> Any:
            return MockResult()

        async def commit(self) -> None:
            pass

    @asynccontextmanager
    async def mock_session_factory() -> AsyncIterator[MockSession]:
        yield MockSession()

    with caplog.at_level(logging.INFO):
        report = await run_pipeline(
            session_factory=cast(Any, mock_session_factory),
            digest_date=date(2026, 9, 7),
            window_start=now - timedelta(hours=24),
            window_end=now,
        )

    assert report.processed_snapshot_count == 2
    assert sentinel_marker not in caplog.text
    err_records = [
        r for r in caplog.records if r.levelname == "ERROR" and "comparison" in r.message
    ]
    assert len(err_records) == 1
    assert getattr(err_records[0], "exception_type", None) == "RuntimeError"
    assert err_records[0].exc_info is None


@pytest.mark.asyncio
async def test_run_pipeline_reuses_existing_published_digest_on_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a published digest already exists for the date, run_pipeline returns it without re-persisting."""
    now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
    existing_id = new_id()
    existing_digest = Digest(
        id=existing_id,
        digest_date=date(2026, 9, 7),
        status=DigestStatus.PUBLISHED,
        title="Existing Published Digest",
        claims=[],
    )

    monkeypatch.setattr(
        "ai_daily_digest.intelligence.run.select_snapshots_in_window",
        AsyncMock(return_value=[]),
    )

    persist_called = False

    class MockStore:
        def __init__(self, _session: Any) -> None:
            pass

        async def get_published_digest_by_date(self, digest_date: date) -> Digest | None:
            if digest_date == date(2026, 9, 7):
                return existing_digest
            return None

        async def persist_digest(self, digest: Digest) -> Digest:
            nonlocal persist_called
            persist_called = True
            return digest

    monkeypatch.setattr("ai_daily_digest.intelligence.run.PostgresFactStore", MockStore)

    class MockResult:
        def all(self) -> list[Any]:
            return []

    class MockSession:
        async def execute(self, _stmt: Any) -> Any:
            return MockResult()

        async def commit(self) -> None:
            pass

    @asynccontextmanager
    async def mock_session_factory() -> AsyncIterator[MockSession]:
        yield MockSession()

    report = await run_pipeline(
        session_factory=cast(Any, mock_session_factory),
        digest_date=date(2026, 9, 7),
        window_start=now - timedelta(hours=24),
        window_end=now,
    )

    assert report.digest_id == existing_id
    assert report.status == "published"
    assert report.published is True
    assert report.digest_status == "published"
    assert persist_called is False


def test_claims_equivalent_behavior() -> None:
    """Test claim equivalence helper under matching and mismatching conditions."""
    cid1, cid2 = new_id(), new_id()
    c1 = DigestClaim(id=new_id(), text="Claim A", citation_snapshot_ids=[cid1, cid2])
    c2 = DigestClaim(id=new_id(), text="  Claim A  ", citation_snapshot_ids=[cid2, cid1])
    c3 = DigestClaim(id=new_id(), text="Claim B", citation_snapshot_ids=[cid1])

    assert _claims_equivalent([c1], [c2]) is True
    assert _claims_equivalent([c1], [c3]) is False
    assert _claims_equivalent([c1, c3], [c3, c2]) is True
    assert _claims_equivalent([c1], []) is False
    assert _claims_equivalent([], []) is True


@pytest.mark.asyncio
async def test_run_pipeline_routes_to_review_when_new_claims_differ_from_published_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a published digest already exists for the date, but the freshly assembled digest

    has new/differing claims, the run routes to review status rather than reporting success.
    """
    now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
    existing_id = new_id()
    existing_digest = Digest(
        id=existing_id,
        digest_date=date(2026, 9, 7),
        status=DigestStatus.PUBLISHED,
        title="Existing Published Digest",
        claims=[],  # Published digest had no claims
    )

    item = SourceItemRow(
        id=new_id(),
        dedupe_key="k1",
        source_id="s1",
        publisher="OpenAI",
        title="Item 1",
        canonical_url="https://example.com/1",
        first_fetched_at=now,
        language="en",
    )
    snap = DocumentSnapshotRow(
        id=new_id(),
        source_item_id=item.id,
        content_hash="h1",
        fetched_at=now,
        content_text="Sample text",
    )

    monkeypatch.setattr(
        "ai_daily_digest.intelligence.run.select_snapshots_in_window",
        AsyncMock(return_value=[(item, snap)]),
    )
    monkeypatch.setattr(
        "ai_daily_digest.intelligence.run._resolve_and_extract_item",
        AsyncMock(return_value=(Subject(company="OpenAI", product="GPT-4o"), [])),
    )

    persisted_digests: list[Digest] = []

    class MockStore:
        def __init__(self, _session: Any) -> None:
            pass

        async def detect_and_persist_changes(self, *args: Any, **kwargs: Any) -> list[Any]:
            del args, kwargs
            return []

        async def get_changes_for_snapshot(self, *args: Any, **kwargs: Any) -> list[Any]:
            del args, kwargs
            return []

        async def get_published_digest_by_date(self, digest_date: date) -> Digest | None:
            if digest_date == date(2026, 9, 7):
                return existing_digest
            return None

        async def persist_digest(self, digest: Digest) -> Digest:
            persisted_digests.append(digest)
            return digest

    monkeypatch.setattr("ai_daily_digest.intelligence.run.PostgresFactStore", MockStore)

    mock_claim = DigestClaim(
        id=new_id(),
        text="New claim not in existing published digest",
        citation_snapshot_ids=[snap.id],
    )
    new_digest_id = new_id()
    assembled = Digest(
        id=new_digest_id,
        digest_date=date(2026, 9, 7),
        status=DigestStatus.PUBLISHED,
        title="Newly Assembled Digest",
        claims=[mock_claim],
    )
    monkeypatch.setattr(
        "ai_daily_digest.intelligence.run.assemble_digest",
        lambda **_kwargs: assembled,
    )

    class MockResult:
        def all(self) -> list[Any]:
            return []

    class MockSession:
        async def execute(self, _stmt: Any) -> Any:
            return MockResult()

        async def commit(self) -> None:
            pass

    @asynccontextmanager
    async def mock_session_factory() -> AsyncIterator[MockSession]:
        yield MockSession()

    report = await run_pipeline(
        session_factory=cast(Any, mock_session_factory),
        digest_date=date(2026, 9, 7),
        window_start=now - timedelta(hours=24),
        window_end=now,
    )

    assert report.digest_id == new_digest_id
    assert report.status == "review"
    assert report.digest_status == "review"
    assert report.published is False
    assert exit_code_for(report) == 1
    assert len(persisted_digests) == 1
    assert persisted_digests[0].status == DigestStatus.REVIEW
    assert persisted_digests[0].id == new_digest_id


# --- evaluate_digest_run ---


@pytest.mark.asyncio
async def test_evaluate_digest_run_raises_when_digest_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock()
    missing_id = uuid.uuid4()

    monkeypatch.setattr(
        PostgresFactStore,
        "get_digest_by_id",
        AsyncMock(return_value=None),
    )

    with pytest.raises(ValueError, match=f"Digest with id {missing_id} not found"):
        await evaluate_digest_run(missing_id, session)


@pytest.mark.asyncio
async def test_evaluate_digest_run_scores_persisted_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock()
    digest_id = DIGEST_1
    snap_id = SNAPSHOT_1
    claim = DigestClaim(
        id=CLAIM_1,
        text="GPT-4o has 128000 context window.",
        citation_snapshot_ids=[snap_id],
    )
    digest = Digest(
        id=digest_id,
        digest_date=date(2026, 8, 20),
        status=DigestStatus.DRAFT,
        title="Test",
        claims=[claim],
    )

    snap_row = DocumentSnapshotRow(
        id=snap_id,
        source_item_id=ITEM_1,
        fetched_at=datetime(2026, 8, 20, tzinfo=UTC),
        content_hash=f"sha256:{snap_id}",
        content_text="OpenAI introduces GPT-4o with 128000 context window.",
    )

    monkeypatch.setattr(
        PostgresFactStore,
        "get_digest_by_id",
        AsyncMock(return_value=digest),
    )

    exec_res = MagicMock()
    exec_res.scalars.return_value.all.return_value = [snap_row]
    session.execute = AsyncMock(return_value=exec_res)

    eval_result: DigestRunEvaluation = await evaluate_digest_run(digest_id, session)
    assert eval_result.citation_validity == 1.0
    assert eval_result.unsupported_claim_count == 0
    assert eval_result.duplicate_rate == 0.0


@pytest.mark.asyncio
async def test_evaluate_digest_run_detects_unsupported_claims_and_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock()
    digest_id = DIGEST_1
    snap_id_valid = SNAPSHOT_1
    snap_id_missing = SNAPSHOT_MISSING

    claim1 = DigestClaim(
        id=CLAIM_1,
        text="GPT-4o has 128000 context window.",
        citation_snapshot_ids=[snap_id_valid],
    )
    claim2 = DigestClaim(
        id=CLAIM_2,
        text="GPT-4o has 128000 context window.",
        citation_snapshot_ids=[snap_id_missing],
    )
    digest = Digest(
        id=digest_id,
        digest_date=date(2026, 8, 20),
        status=DigestStatus.DRAFT,
        title="Test",
        claims=[claim1, claim2],
    )

    snap_row_valid = DocumentSnapshotRow(
        id=snap_id_valid,
        source_item_id=ITEM_1,
        fetched_at=datetime(2026, 8, 20, tzinfo=UTC),
        content_hash=f"sha256:{snap_id_valid}",
        content_text="OpenAI introduces GPT-4o with 128000 context window.",
    )

    monkeypatch.setattr(
        PostgresFactStore,
        "get_digest_by_id",
        AsyncMock(return_value=digest),
    )

    exec_res = MagicMock()
    exec_res.scalars.return_value.all.return_value = [snap_row_valid]
    session.execute = AsyncMock(return_value=exec_res)

    eval_result: DigestRunEvaluation = await evaluate_digest_run(digest_id, session)
    assert eval_result.citation_validity == 0.5
    assert eval_result.unsupported_claim_count == 1
    assert eval_result.duplicate_rate == 0.5


@pytest.mark.asyncio
async def test_run_pipeline_processes_langchain_and_langgraph_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that snapshots from langchain_pypi and langgraph_pypi resolve deterministically
    and proceed through fact extraction, change detection, and claim assembly."""
    target_date = date(2026, 9, 10)
    now = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)

    item_lc = SourceItemRow(
        id=new_id(),
        dedupe_key="k_lc",
        source_id="langchain_pypi",
        publisher="Python Package Index",
        title="9.9.0",
        canonical_url="https://pypi.org/project/langchain/9.9.0/",
        first_fetched_at=now,
        language="en",
    )
    snap_lc = DocumentSnapshotRow(
        id=new_id(),
        source_item_id=item_lc.id,
        content_hash="h_lc",
        fetched_at=now,
        content_text="Release 9.9.0 of LangChain with updated context.",
    )

    item_lg = SourceItemRow(
        id=new_id(),
        dedupe_key="k_lg",
        source_id="langgraph_pypi",
        publisher="Python Package Index",
        title="0.2.1",
        canonical_url="https://pypi.org/project/langgraph/0.2.1/",
        first_fetched_at=now,
        language="en",
    )
    snap_lg = DocumentSnapshotRow(
        id=new_id(),
        source_item_id=item_lg.id,
        content_hash="h_lg",
        fetched_at=now,
        content_text="Release notes for 0.2.1 with workflow graph features.",
    )

    async def _mock_select(*_args: Any, **_kwargs: Any) -> list[Any]:
        return [(item_lc, snap_lc), (item_lg, snap_lg)]

    monkeypatch.setattr("ai_daily_digest.intelligence.run.select_snapshots_in_window", _mock_select)

    class MockLangChainStore:
        def __init__(self, _session: Any) -> None:
            pass

        async def detect_and_persist_changes(self, **kwargs: Any) -> list[Any]:
            del kwargs
            return []

        async def get_changes_for_snapshot(self, _snap_id: Any) -> list[Any]:
            return []

        async def get_published_digest_by_date(self, *args: Any, **kwargs: Any) -> Digest | None:
            del args, kwargs
            return None

        async def persist_digest(self, digest: Digest) -> Digest:
            return digest

        async def publish_digest(self, digest_id: uuid.UUID, **kwargs: Any) -> Digest:
            del kwargs
            return Digest(
                id=digest_id,
                digest_date=target_date,
                status=DigestStatus.PUBLISHED,
                title="Published Digest",
                claims=[],
            )

    monkeypatch.setattr("ai_daily_digest.intelligence.run.PostgresFactStore", MockLangChainStore)

    class MockResult:
        def all(self) -> list[Any]:
            return []

    class MockSession:
        async def execute(self, _stmt: Any) -> Any:
            return MockResult()

        async def commit(self) -> None:
            pass

    @asynccontextmanager
    async def mock_session_factory() -> AsyncIterator[MockSession]:
        yield MockSession()

    report = await run_pipeline(
        session_factory=cast(Any, mock_session_factory),
        digest_date=target_date,
        window_start=now - timedelta(hours=24),
        window_end=now,
        extract_call_fn=lambda _sys, _pr: FactExtractionResponse(facts=[]),
    )

    assert report.selected_snapshot_count == 2
    assert report.unresolved_snapshot_count == 0
    assert report.processed_snapshot_count == 2
    assert report.failed_snapshot_count == 0


# ---------------------------------------------------------------------------
# _resolve_and_extract_item -- the actual resolution/extraction step
# `run_pipeline` runs per snapshot (this is the code path wired to the
# `generate-digest` production entry point / Render cron, unlike
# graph.py's LangGraph wiring). Direct unit tests, no DB needed.
# ---------------------------------------------------------------------------


def _source_item(item_id: uuid.UUID, title: str) -> SourceItem:
    return SourceItem(
        id=item_id,
        dedupe_key=f"sha256:{item_id}",
        source_id="openai_news",
        publisher="OpenAI",
        title=title,
        canonical_url="https://openai.com/news/example",  # type: ignore[arg-type]
        first_fetched_at=datetime(2026, 9, 10, tzinfo=UTC),
    )


def _document_snapshot(snapshot_id: uuid.UUID, item_id: uuid.UUID, text: str) -> DocumentSnapshot:
    return DocumentSnapshot(
        id=snapshot_id,
        source_item_id=item_id,
        fetched_at=datetime(2026, 9, 10, tzinfo=UTC),
        content_hash=f"sha256:{snapshot_id}",
        content_text=text,
    )


def _no_facts(system: str, prompt: str) -> FactExtractionResponse:
    return FactExtractionResponse(facts=[])


@pytest.mark.asyncio
async def test_resolve_and_extract_item_skips_llm_for_multi_subject_item() -> None:
    """Item 1 from the rehearsal: "Codex and ChatGPT" both phrase-match.
    The LLM fallback must never be invoked at all -- asserted with a
    call_fn that raises if it's ever called, not just by checking the
    returned subject."""
    chatgpt = Subject(company="OpenAI", product="ChatGPT")
    codex = Subject(company="OpenAI", product="Codex")
    alias_table = [
        SubjectAlias(subject=chatgpt, aliases=[]),
        SubjectAlias(subject=codex, aliases=[]),
    ]

    def resolve_llm_must_not_be_called(system: str, prompt: str) -> ResolveLLMResponse:
        raise AssertionError("resolve_via_llm must never be called for a multi-subject item")

    item_id = new_id()
    item = _source_item(
        item_id,
        "How a researcher uses Codex and ChatGPT to search for new antimicrobial molecules",
    )
    snapshot = _document_snapshot(
        new_id(),
        item_id,
        "The lab uses Codex and ChatGPT to search genomes for antimicrobial candidates.",
    )

    subject, facts = await _resolve_and_extract_item(
        item,
        snapshot,
        set(),
        alias_table,
        resolve_llm_must_not_be_called,
        _no_facts,
    )

    assert subject is None
    assert facts == []


@pytest.mark.asyncio
async def test_resolve_and_extract_item_rejects_unsupported_gpt4o_evidence() -> None:
    """Regression test for the actual rehearsal failure at the
    run_pipeline layer: the real government/policy item title and
    description (fetched verbatim from https://openai.com/news/rss.xml),
    with a fake LLM response proposing the catalogue-valid OpenAI/GPT-4o
    at high confidence and a real, verbatim, company-only quote. Must
    resolve to no subject, not silently merge onto GPT-4o."""
    gpt4o = Subject(company="OpenAI", product="GPT-4o")
    alias_table = [SubjectAlias(subject=gpt4o, aliases=["gpt4o", "gpt-4 omni"])]
    description = (
        "OpenAI and GSA will offer eligible federal, state, local, and tribal "
        "governments $0 license fees, 50% off usage, and expanded cyber defense "
        "support."
    )

    def resolve_fake(system: str, prompt: str) -> ResolveLLMResponse:
        return ResolveLLMResponse(
            company="OpenAI", product="GPT-4o", supporting_quote=description, confidence=0.85
        )

    item_id = new_id()
    item = _source_item(
        item_id,
        "Expanding AI access and cyber defense for federal, state, local, and tribal governments",
    )
    snapshot = _document_snapshot(new_id(), item_id, description)

    subject, facts = await _resolve_and_extract_item(
        item,
        snapshot,
        set(),
        alias_table,
        resolve_fake,
        _no_facts,
    )

    assert subject is None
    assert facts == []


@pytest.mark.asyncio
async def test_resolve_and_extract_item_resolves_gpt_live_1_without_llm() -> None:
    """Item 5 from the rehearsal resolves deterministically -- the LLM
    fallback must not even be invoked."""
    gpt_live_1 = Subject(company="OpenAI", product="GPT-Live-1")
    alias_table = [SubjectAlias(subject=gpt_live_1, aliases=[])]

    def resolve_llm_must_not_be_called(system: str, prompt: str) -> ResolveLLMResponse:
        raise AssertionError("resolve_via_llm must never be called for a deterministic match")

    item_id = new_id()
    item = _source_item(
        item_id, "Build more natural voice experiences with GPT\u2011Live\u20111 in the API"
    )
    snapshot = _document_snapshot(
        new_id(),
        item_id,
        "GPT\u2011Live\u20111 brings natural, full-duplex voice conversations to the API.",
    )

    subject, _facts = await _resolve_and_extract_item(
        item,
        snapshot,
        set(),
        alias_table,
        resolve_llm_must_not_be_called,
        _no_facts,
    )

    assert subject == gpt_live_1


@pytest.mark.asyncio
async def test_anthropic_rehearsal_both_snapshots_resolve_to_canonical_claude() -> None:
    """Proves both Anthropic rehearsal articles (100K and Claude 2.1) resolve
    deterministically to Subject(company='Anthropic', product='Claude')."""
    alias_table = load_alias_table()

    def fail_llm(system: str, prompt: str) -> ResolveLLMResponse:
        raise AssertionError("resolve_via_llm must not be called")

    # Item 1: 100K Context Windows
    item1_id = new_id()
    item1 = SourceItem(
        id=item1_id,
        dedupe_key="anthropic_100k",
        source_id="anthropic_news",
        publisher="Anthropic",
        title="Introducing 100K Context Windows",
        canonical_url="https://www.anthropic.com/news/100k-context-windows",  # type: ignore[arg-type]
        first_fetched_at=datetime(2023, 5, 11, 9, 0, 0, tzinfo=UTC),
    )
    snap1 = DocumentSnapshot(
        id=new_id(),
        source_item_id=item1_id,
        fetched_at=datetime(2023, 5, 11, 9, 0, 0, tzinfo=UTC),
        content_hash="sha256:100k",
        content_text="Claude now supports a 100,000 token context window.",
    )

    subj1, _ = await _resolve_and_extract_item(
        item1, snap1, set(), alias_table, fail_llm, _no_facts
    )
    assert subj1 == Subject(company="Anthropic", product="Claude")

    # Item 2: Claude 2.1 (200K)
    item2_id = new_id()
    item2 = SourceItem(
        id=item2_id,
        dedupe_key="anthropic_200k",
        source_id="anthropic_news",
        publisher="Anthropic",
        title="Claude 2.1",
        canonical_url="https://www.anthropic.com/news/claude-2-1",  # type: ignore[arg-type]
        first_fetched_at=datetime(2023, 11, 21, 9, 0, 0, tzinfo=UTC),
    )
    snap2 = DocumentSnapshot(
        id=new_id(),
        source_item_id=item2_id,
        fetched_at=datetime(2023, 11, 21, 9, 0, 0, tzinfo=UTC),
        content_hash="sha256:200k",
        content_text="Claude 2.1 provides a 200,000 token context window.",
    )

    subj2, _ = await _resolve_and_extract_item(
        item2, snap2, set(), alias_table, fail_llm, _no_facts
    )
    assert subj2 == Subject(company="Anthropic", product="Claude")


@pytest.mark.asyncio
async def test_anthropic_rehearsal_two_snapshot_progression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proves the full 2-snapshot Anthropic rehearsal progression:
    1. 100K snapshot processed first -> establishes baseline in current_facts (0 changes).
    2. 200K snapshot processed second -> compares against baseline -> produces 1 real change
       (100,000 -> 200,000) and 1 grounded claim.
    3. run_pipeline produces a published digest (status='published', claim_count=1, exit_code=0)."""
    target_date = date(2026, 9, 11)
    t1 = datetime(2023, 5, 11, 9, 0, 0, tzinfo=UTC)
    t2 = datetime(2023, 11, 21, 9, 0, 0, tzinfo=UTC)

    claude_subject = Subject(company="Anthropic", product="Claude")

    item1_row = SourceItemRow(
        id=new_id(),
        dedupe_key="anthropic_100k",
        source_id="anthropic_news",
        publisher="Anthropic",
        title="Introducing 100K Context Windows",
        canonical_url="https://www.anthropic.com/news/100k-context-windows",
        first_fetched_at=t1,
        language="en",
    )
    snap1_row = DocumentSnapshotRow(
        id=new_id(),
        source_item_id=item1_row.id,
        content_hash="sha256:100k",
        fetched_at=t1,
        content_text="Claude now supports a 100,000 token context window.",
    )

    item2_row = SourceItemRow(
        id=new_id(),
        dedupe_key="anthropic_200k",
        source_id="anthropic_news",
        publisher="Anthropic",
        title="Claude 2.1",
        canonical_url="https://www.anthropic.com/news/claude-2-1",
        first_fetched_at=t2,
        language="en",
    )
    snap2_row = DocumentSnapshotRow(
        id=new_id(),
        source_item_id=item2_row.id,
        content_hash="sha256:200k",
        fetched_at=t2,
        content_text="Claude 2.1 provides a 200,000 token context window.",
    )

    # In-memory mock store simulating PostgresFactStore behavior
    current_facts_state: dict[tuple[str, str, str], tuple[FactObservation, ExtractedFact]] = {}
    committed_changes: list[Change] = []
    persisted_digests: list[Digest] = []

    class MockFactStore:
        def __init__(self, _session: Any) -> None:
            pass

        async def read_current_facts(
            self, subject: Subject, fields: list[str]
        ) -> dict[str, tuple[Any, Any]]:
            res: dict[str, tuple[Any, Any]] = {}
            for f in fields:
                key = (subject.company, subject.product, f)
                if key in current_facts_state:
                    obs, ef = current_facts_state[key]
                    # Format as (CurrentFactRow-like, ExtractedFactRow-like)
                    cf_mock = MagicMock()
                    cf_mock.fact_id = ef.id
                    cf_mock.snapshot_id = obs.snapshot_id
                    cf_mock.observed_at = obs.observed_at
                    cf_mock.extraction_version = 1
                    ef_mock = MagicMock()
                    ef_mock.value = obs.value
                    ef_mock.disclosure_status = ef.disclosure_status
                    res[f] = (cf_mock, ef_mock)
            return res

        async def lock_subject_fields(self, subject: Subject, fields: list[str]) -> None:
            pass

        async def record_extracted_facts(
            self,
            subject: Subject,
            facts: Sequence[ExtractedFact],
            *,
            snapshot_observed_at: datetime,
            extraction_version: int = 1,
        ) -> list[ExtractedFact]:
            return list(facts)

        async def advance_current_facts(
            self, subject: Subject, facts: Sequence[ExtractedFact]
        ) -> dict[str, bool]:
            return {f.field: True for f in facts}

        async def get_changes_for_snapshot(self, snapshot_id: uuid.UUID) -> list[Change]:
            return [c for c in committed_changes if c.current.snapshot_id == snapshot_id]

        async def detect_and_persist_changes(
            self,
            subject: Subject,
            facts: Sequence[ExtractedFact],
            *,
            snapshot_observed_at: datetime,
            detected_at: datetime,
            extraction_version: int = 1,
            change_set_id: uuid.UUID | None = None,
        ) -> list[Change]:
            changes: list[Change] = []
            for f in facts:
                key = (subject.company, subject.product, f.field)
                prior = current_facts_state.get(key)
                curr_obs = FactObservation(
                    value=f.value,
                    observed_at=snapshot_observed_at,
                    snapshot_id=f.snapshot_id,
                )
                if prior is None:
                    # Baseline established
                    current_facts_state[key] = (curr_obs, f)
                else:
                    prior_obs, _ = prior
                    if prior_obs.value != f.value:
                        ch = Change(
                            id=new_id(),
                            change_set_id=change_set_id or new_id(),
                            subject=subject,
                            field=f.field,
                            change_type="increased",
                            previous=prior_obs,
                            current=curr_obs,
                            confidence=f.confidence or 1.0,
                            detected_at=detected_at,
                        )
                        changes.append(ch)
                        committed_changes.append(ch)
                        current_facts_state[key] = (curr_obs, f)
            return changes

        async def persist_digest(self, digest: Digest) -> Digest:
            persisted_digests.append(digest)
            return digest

        async def publish_digest(
            self,
            digest_id: uuid.UUID,
            *,
            known_snapshot_ids: Any,
            snapshot_resolver: Any,
        ) -> Digest:
            for d in persisted_digests:
                if d.id == digest_id:
                    published = d.model_copy(update={"status": DigestStatus.PUBLISHED})
                    return published
            return persisted_digests[0].model_copy(update={"status": DigestStatus.PUBLISHED})

        async def get_published_digest_by_date(self, *args: Any, **kwargs: Any) -> Digest | None:
            return None

    monkeypatch.setattr("ai_daily_digest.intelligence.run.PostgresFactStore", MockFactStore)

    # Mock DB session
    class MockResult:
        def __init__(self, rows: list[Any]) -> None:
            self._rows = rows

        def all(self) -> list[Any]:
            return self._rows

        def scalars(self) -> Any:
            mock_scalars = MagicMock()
            mock_scalars.all.return_value = self._rows
            return mock_scalars

    class MockSession:
        async def execute(self, stmt: Any) -> Any:
            # Check if selecting snapshots
            stmt_str = str(stmt)
            if "source_items" in stmt_str and "document_snapshots" in stmt_str:
                # Deterministic oldest-first order
                return MockResult([(item1_row, snap1_row), (item2_row, snap2_row)])
            if "subjects" in stmt_str:
                return MockResult([(claude_subject.company, claude_subject.product)])
            return MockResult([])

        async def commit(self) -> None:
            pass

    @asynccontextmanager
    async def mock_session_factory() -> AsyncIterator[MockSession]:
        yield MockSession()

    def fake_extract_call(system: str, prompt: str) -> FactExtractionResponse:
        # Exercises prompt extraction returning context_window alias or canonical
        if "100K" in prompt or "100,000" in prompt:
            return FactExtractionResponse(
                facts=[
                    FactCandidate(
                        field="context_window",  # Model returns alias; normalized by FactCandidate
                        value="100000",
                        quoted_span="Claude now supports a 100,000 token context window.",
                        confidence=0.98,
                    )
                ]
            )
        return FactExtractionResponse(
            facts=[
                FactCandidate(
                    field="context_window_tokens",
                    value="200000",
                    quoted_span="Claude 2.1 provides a 200,000 token context window.",
                    confidence=0.98,
                )
            ]
        )

    report = await run_pipeline(
        session_factory=cast(Any, mock_session_factory),
        digest_date=target_date,
        window_start=datetime(2023, 1, 1, tzinfo=UTC),
        window_end=datetime(2024, 1, 1, tzinfo=UTC),
        extract_call_fn=fake_extract_call,
    )

    assert report.selected_snapshot_count == 2
    assert report.processed_snapshot_count == 2
    assert report.failed_snapshot_count == 0
    assert report.unresolved_snapshot_count == 0
    assert report.extracted_change_count == 1
    assert report.claim_count == 1
    assert report.published is True
    assert report.status == "published"
    assert exit_code_for(report) == 0
    assert len(committed_changes) == 1
    change = committed_changes[0]
    assert change.subject == claude_subject
    assert change.field == "context_window_tokens"
    assert change.previous is not None
    assert change.previous.value == "100000"
    assert change.current.value == "200000"
