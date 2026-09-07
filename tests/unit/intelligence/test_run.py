"""Unit tests for the daily intelligence runner CLI and helpers."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

from ai_daily_digest.intelligence.run import (
    DigestRunReport,
    _never_auto_publish_comparisons,
    _parse_args,
    exit_code_for,
    render_report,
    resolve_window,
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
    args = _parse_args([
        "--digest-date",
        "2026-09-04",
        "--since",
        "12h",
        "--limit",
        "10",
        "--title",
        "Custom Title",
    ])
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
