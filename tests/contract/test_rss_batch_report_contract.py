"""Contract for the `collect-rss-batch` JSON batch report
(`ingestion/rss/batch.py::render_batch_report`).

`collect-rss-batch` is a worker command, not an HTTP route, but its
stdout is a stable machine interface a cloud-cron service and its log
pipeline parse. These tests pin the envelope: the exact top-level and
`totals` keys, the per-source summary keys, the value types, the closed
`status` / `error_category` vocabularies, deterministic source ordering,
and -- the security-relevant guarantee -- that the report has no field
that can carry a URL, a query string, feed content, or a raw exception
string.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest

from ai_daily_digest.ingestion.rss.batch import (
    BatchReport,
    BatchStatus,
    SourceRunResult,
    render_batch_report,
)
from ai_daily_digest.ingestion.rss.collector import CollectionStatus

pytestmark = pytest.mark.contract

_TOP_LEVEL_KEYS = {
    "job_run_id",
    "started_at",
    "finished_at",
    "requested_source_ids",
    "concurrency",
    "status",
    "totals",
    "sources",
}
_TOTALS_KEYS = {
    "requested",
    "succeeded",
    "partial",
    "failed",
    "items_processed",
    "new_snapshots",
    "unchanged",
    "failed_items",
}
_SOURCE_KEYS = {
    "source_id",
    "status",
    "fetched_entry_count",
    "created_item_count",
    "created_snapshot_count",
    "unchanged_count",
    "failed_item_count",
    "fetch_attempts",
    "error_category",
}
_ALLOWED_ERROR_CATEGORIES = {
    None,
    "partial_entry_failures",
    "fetch_failed",
    "parse_failed",
    "empty_feed",
    "collection_failed",
}
_FORBIDDEN_KEYS = {"failures", "link", "url", "reason", "detail", "message", "error", "traceback"}


def _result(
    source_id: str, status: CollectionStatus, error_category: str | None
) -> SourceRunResult:
    return SourceRunResult(
        source_id=source_id,
        status=status,
        fetched_entry_count=3,
        created_item_count=2,
        created_snapshot_count=2,
        unchanged_count=1,
        failed_item_count=1,
        fetch_attempts=2,
        error_category=error_category,
    )


def _report(*results: SourceRunResult, status: BatchStatus) -> BatchReport:
    return BatchReport(
        job_run_id=uuid.UUID("018f00d0-0000-7000-8000-00000000c0de"),
        started_at=datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
        finished_at=datetime(2026, 9, 8, 12, 0, 9, tzinfo=UTC),
        requested_source_ids=tuple(r.source_id for r in results),
        concurrency=3,
        status=status,
        results=results,
    )


def test_envelope_shape_and_types() -> None:
    report = _report(
        _result("langchain_pypi", CollectionStatus.OK, None),
        _result("openai_news", CollectionStatus.PARTIAL, "partial_entry_failures"),
        _result("langgraph_pypi", CollectionStatus.FAILED, "fetch_failed"),
        status=BatchStatus.PARTIAL,
    )
    rendered = render_batch_report(report)
    assert "\n" not in rendered

    payload = json.loads(rendered)
    assert set(payload) == _TOP_LEVEL_KEYS
    assert list(payload) == sorted(payload)
    assert payload["job_run_id"] == "018f00d0-0000-7000-8000-00000000c0de"
    assert payload["started_at"] == "2026-09-08T12:00:00+00:00"
    assert payload["finished_at"] == "2026-09-08T12:00:09+00:00"
    assert payload["status"] == "partial"
    assert payload["concurrency"] == 3
    assert payload["requested_source_ids"] == ["langchain_pypi", "openai_news", "langgraph_pypi"]

    assert set(payload["totals"]) == _TOTALS_KEYS
    assert payload["totals"]["requested"] == 3
    assert payload["totals"]["succeeded"] == 1
    assert payload["totals"]["partial"] == 1
    assert payload["totals"]["failed"] == 1
    assert all(isinstance(v, int) for v in payload["totals"].values())

    for entry in payload["sources"]:
        assert set(entry) == _SOURCE_KEYS
        assert entry["status"] in {"ok", "partial", "failed"}
        assert entry["error_category"] in _ALLOWED_ERROR_CATEGORIES
        for count_key in _SOURCE_KEYS - {"source_id", "status", "error_category"}:
            assert isinstance(entry[count_key], int)


def test_source_order_is_result_order_not_key_sorted() -> None:
    report = _report(
        _result("langgraph_pypi", CollectionStatus.OK, None),
        _result("langchain_pypi", CollectionStatus.OK, None),
        _result("openai_news", CollectionStatus.OK, None),
        status=BatchStatus.OK,
    )
    payload = json.loads(render_batch_report(report))
    assert [s["source_id"] for s in payload["sources"]] == [
        "langgraph_pypi",
        "langchain_pypi",
        "openai_news",
    ]


def test_report_has_no_field_that_could_carry_content_or_a_url() -> None:
    report = _report(
        _result("openai_news", CollectionStatus.FAILED, "collection_failed"),
        status=BatchStatus.FAILED,
    )
    payload = json.loads(render_batch_report(report))
    assert not (_TOP_LEVEL_KEYS & _FORBIDDEN_KEYS)
    assert not (set(payload["totals"]) & _FORBIDDEN_KEYS)
    assert not (set(payload["sources"][0]) & _FORBIDDEN_KEYS)
