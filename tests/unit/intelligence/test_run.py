"""Unit tests for the daily intelligence runner CLI and helpers."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import pytest

from ai_daily_digest.ingestion.db.models import DocumentSnapshotRow, SourceItemRow
from ai_daily_digest.intelligence.run import (
    DigestRunReport,
    _emit_failure,
    _never_auto_publish_comparisons,
    _parse_args,
    exit_code_for,
    main,
    render_report,
    resolve_window,
    run_pipeline,
    to_document_snapshot,
    to_source_item,
)
from ai_daily_digest.shared.ids import new_id
from ai_daily_digest.shared.schemas import Digest, DigestClaim, DigestStatus


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


def test_parse_args_invalid_limit() -> None:
    with pytest.raises(SystemExit):
        _parse_args(["--limit", "not_a_number"])


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
