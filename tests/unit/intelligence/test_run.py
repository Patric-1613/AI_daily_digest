"""Unit tests for the daily intelligence runner CLI and helpers."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from ai_daily_digest.ingestion.db.models import DocumentSnapshotRow, SourceItemRow
from ai_daily_digest.intelligence.db.repository import PostgresFactStore
from ai_daily_digest.intelligence.run import (
    DigestRunEvaluation,
    DigestRunReport,
    _claims_equivalent,
    _emit_failure,
    _never_auto_publish_comparisons,
    _parse_args,
    evaluate_digest_run,
    exit_code_for,
    main,
    render_report,
    resolve_window,
    run_pipeline,
    select_snapshots_in_window,
    to_document_snapshot,
    to_source_item,
)
from ai_daily_digest.shared.ids import new_id
from ai_daily_digest.shared.schemas import Digest, DigestClaim, DigestStatus, Subject
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
    assert args.since == "12h"
    assert args.limit == 10
    assert args.title == "Custom Title"


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

    def _make_report(status: str) -> DigestRunReport:
        return DigestRunReport(
            digest_id=None,
            digest_date="2026-09-07",
            status=status,
            digest_status=status,
            selected_snapshot_count=0,
            processed_snapshot_count=0,
            failed_snapshot_count=0,
            unresolved_snapshot_count=0,
            extracted_change_count=0,
            claim_count=0,
            published=(status == "published"),
            started_at=now,
            completed_at=now,
        )

    assert exit_code_for(_make_report("published")) == 0
    assert exit_code_for(_make_report("partial")) == 1
    assert exit_code_for(_make_report("review")) == 1
    assert exit_code_for(_make_report("draft")) == 1
    assert exit_code_for(_make_report("failed")) == 2


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
