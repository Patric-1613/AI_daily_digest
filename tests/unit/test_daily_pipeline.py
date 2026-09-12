"""Unit tests for the combined daily pipeline orchestrator
(`daily_pipeline.py`). Every test injects fake `run_collection` /
`run_intelligence` collaborators -- no real network, database, or LLM call
is ever made. `run_rss_batch` and `intelligence.run.run_pipeline` each have
their own full offline test suites elsewhere; these tests only cover this
module's own glue: window-boundary propagation and the combined
outcome-semantics table."""

from __future__ import annotations

import argparse
import json
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ai_daily_digest.daily_pipeline import (
    PRODUCTION_RSS_SOURCE_IDS,
    CombinedPipelineStatus,
    DailyPipelineReport,
    _combine_status,
    _concurrency_arg,
    _emit_start_failure,
    _parse_args,
    _positive_int,
    daily_pipeline_exit_code,
    main,
    render_daily_pipeline_report,
    run_daily_pipeline,
    run_daily_pipeline_with_real_infrastructure,
)
from ai_daily_digest.ingestion.rss.batch import BatchReport, BatchStatus, SourceRunResult
from ai_daily_digest.ingestion.rss.collector import CollectionStatus
from ai_daily_digest.ingestion.rss.run import SourceSelectionError
from ai_daily_digest.ingestion.sources import load_source_registry
from ai_daily_digest.intelligence.run import DigestRunReport
from ai_daily_digest.shared.ids import new_id

_REGISTRY = load_source_registry()
_NOW = datetime(2026, 9, 12, 6, 0, 0, tzinfo=UTC)


def _fixed_clock() -> datetime:
    return _NOW


class _UnusedSessionFactory(AbstractAsyncContextManager[AsyncSession]):
    """Standing in for `session_factory` in every test here: `run_daily_pipeline`
    only ever forwards it to the (fully faked) `run_collection`/`run_intelligence`
    collaborators, so this must never actually be entered."""

    def __call__(self) -> AbstractAsyncContextManager[AsyncSession]:
        return self

    async def __aenter__(self) -> AsyncSession:
        raise AssertionError("run_daily_pipeline must never open a session itself")

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


_unused_session_factory = _UnusedSessionFactory()


def _source_run_result(**overrides: Any) -> SourceRunResult:
    defaults: dict[str, Any] = {
        "source_id": "openai_news",
        "status": CollectionStatus.OK,
        "fetched_entry_count": 5,
        "created_item_count": 2,
        "created_snapshot_count": 2,
        "unchanged_count": 3,
        "failed_item_count": 0,
        "fetch_attempts": 1,
        "error_category": None,
    }
    defaults.update(overrides)
    return SourceRunResult(**defaults)


def _batch_report(**overrides: Any) -> BatchReport:
    defaults: dict[str, Any] = {
        "job_run_id": new_id(),
        "started_at": _NOW,
        "finished_at": _NOW + timedelta(seconds=30),
        "requested_source_ids": PRODUCTION_RSS_SOURCE_IDS,
        "concurrency": 3,
        "status": BatchStatus.OK,
        "results": (_source_run_result(),),
    }
    defaults.update(overrides)
    return BatchReport(**defaults)


def _digest_report(**overrides: Any) -> DigestRunReport:
    defaults: dict[str, Any] = {
        "digest_id": None,
        "digest_date": "2026-09-12",
        "status": "draft",
        "digest_status": "draft",
        "selected_snapshot_count": 2,
        "processed_snapshot_count": 2,
        "failed_snapshot_count": 0,
        "unresolved_snapshot_count": 0,
        "extracted_change_count": 0,
        "claim_count": 0,
        "published": False,
        "started_at": _NOW,
        "completed_at": _NOW + timedelta(seconds=5),
        "failures": [],
    }
    defaults.update(overrides)
    return DigestRunReport(**defaults)


class _Recorder:
    """Fake async collaborator: records every call's kwargs and returns a
    fixed canned value (or raises a fixed exception)."""

    def __init__(self, *, result: Any = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


async def _run(
    *,
    collection: BatchReport,
    intelligence: DigestRunReport | None,
    intelligence_limit: int = 5,
) -> tuple[Any, _Recorder, _Recorder]:
    run_collection = _Recorder(result=collection)
    run_intelligence = _Recorder(result=intelligence)
    report = await run_daily_pipeline(
        registry=_REGISTRY,
        session_factory=_unused_session_factory,
        fetcher=object(),  # type: ignore[arg-type]
        clock=_fixed_clock,
        intelligence_limit=intelligence_limit,
        run_collection=run_collection,
        run_intelligence=run_intelligence,
    )
    return report, run_collection, run_intelligence


# -- combined outcome semantics --------------------------------------------


@pytest.mark.asyncio
async def test_successful_collection_then_published_digest() -> None:
    intelligence_report = _digest_report(
        status="published",
        digest_status="published",
        published=True,
        claim_count=2,
        extracted_change_count=2,
    )
    report, _collection_calls, intel_calls = await _run(
        collection=_batch_report(status=BatchStatus.OK),
        intelligence=intelligence_report,
    )

    assert report.status is CombinedPipelineStatus.PUBLISHED
    assert daily_pipeline_exit_code(report) == 0
    assert len(intel_calls.calls) == 1


@pytest.mark.asyncio
async def test_successful_collection_then_clean_zero_change() -> None:
    intelligence_report = _digest_report(status="draft", digest_status="draft", published=False)
    report, _collection, intel_calls = await _run(
        collection=_batch_report(status=BatchStatus.OK),
        intelligence=intelligence_report,
    )

    assert report.status is CombinedPipelineStatus.CLEAN_ZERO_CHANGE
    assert daily_pipeline_exit_code(report) == 0
    assert len(intel_calls.calls) == 1


@pytest.mark.asyncio
async def test_collection_overflow_is_capped_exit_1_not_a_false_healthy_completion() -> None:
    """Collection creates 20 new snapshots while intelligence_limit (chunk
    size) is 5 -- select_snapshots_in_window has no offset/cursor, so
    intelligence only ever sees the oldest 5. The combined pipeline must
    report `capped`/exit 1, never `published`/`clean_zero_change`/exit 0,
    even though the 5 it did see published cleanly."""
    collection = _batch_report(
        status=BatchStatus.OK,
        results=(_source_run_result(created_item_count=20, created_snapshot_count=20),),
    )
    intelligence_report = _digest_report(
        status="published",
        digest_status="published",
        published=True,
        selected_snapshot_count=5,
        processed_snapshot_count=5,
        claim_count=2,
        extracted_change_count=2,
    )
    report, _collection_calls, intel_calls = await _run(
        collection=collection,
        intelligence=intelligence_report,
        intelligence_limit=5,
    )

    assert report.status is CombinedPipelineStatus.CAPPED
    assert daily_pipeline_exit_code(report) == 1
    # Intelligence is still invoked exactly once -- overflow is detected
    # from its result afterwards, never by looping or re-invoking it to
    # "try to get the rest" (there is no cursor to advance with anyway).
    assert len(intel_calls.calls) == 1
    # The window recorded in the report is unchanged by capping -- it is
    # exactly what an operator needs to pass as `--since` to
    # `generate-digest` to durably replay the entire window later with a
    # larger --limit, per docs/DEPLOYMENT.md ("Overflow behaviour").
    assert report.intelligence_window_start == collection.started_at


@pytest.mark.asyncio
async def test_collection_overflow_with_clean_zero_change_is_still_capped() -> None:
    """Overflow must be caught even when the 5 processed snapshots
    happened to produce no changes -- clean_zero_change is also a
    'healthy' exit-0 outcome and must not mask dropped evidence."""
    collection = _batch_report(
        status=BatchStatus.OK,
        results=(_source_run_result(created_item_count=20, created_snapshot_count=20),),
    )
    intelligence_report = _digest_report(
        status="draft",
        digest_status="draft",
        published=False,
        selected_snapshot_count=5,
        processed_snapshot_count=5,
    )
    report, _collection_calls, intel_calls = await _run(
        collection=collection,
        intelligence=intelligence_report,
        intelligence_limit=5,
    )

    assert report.status is CombinedPipelineStatus.CAPPED
    assert daily_pipeline_exit_code(report) == 1
    assert len(intel_calls.calls) == 1


@pytest.mark.asyncio
async def test_collection_exactly_at_the_limit_is_not_capped() -> None:
    """Collection creating exactly `intelligence_limit` new snapshots is
    the normal, fully-handled case -- must not be misclassified as
    overflow."""
    collection = _batch_report(
        status=BatchStatus.OK,
        results=(_source_run_result(created_item_count=5, created_snapshot_count=5),),
    )
    intelligence_report = _digest_report(
        status="published",
        digest_status="published",
        published=True,
        selected_snapshot_count=5,
        processed_snapshot_count=5,
    )
    report, _collection_calls, _intel_calls = await _run(
        collection=collection,
        intelligence=intelligence_report,
        intelligence_limit=5,
    )

    assert report.status is CombinedPipelineStatus.PUBLISHED
    assert daily_pipeline_exit_code(report) == 0


@pytest.mark.asyncio
async def test_successful_collection_with_no_new_snapshots_is_healthy_no_updates() -> None:
    collection = _batch_report(
        status=BatchStatus.OK,
        results=(_source_run_result(created_snapshot_count=0, unchanged_count=5),),
    )
    report, _collection_calls, intel_calls = await _run(collection=collection, intelligence=None)

    assert report.status is CombinedPipelineStatus.NO_UPDATES
    assert daily_pipeline_exit_code(report) == 0
    assert report.intelligence is None
    # Intelligence must never be invoked -- no digest is touched at all.
    assert intel_calls.calls == []


@pytest.mark.asyncio
async def test_partial_collection_with_usable_snapshots_stays_partial_even_if_published() -> None:
    collection = _batch_report(
        status=BatchStatus.PARTIAL,
        results=(
            _source_run_result(source_id="openai_news", status=CollectionStatus.OK),
            _source_run_result(
                source_id="langchain_pypi",
                status=CollectionStatus.FAILED,
                created_snapshot_count=0,
                unchanged_count=0,
                error_category="fetch_failed",
            ),
        ),
    )
    intelligence_report = _digest_report(
        status="published", digest_status="published", published=True
    )
    report, _collection_calls, intel_calls = await _run(
        collection=collection, intelligence=intelligence_report
    )

    # Collection partial must win even though intelligence itself published cleanly.
    assert report.status is CombinedPipelineStatus.PARTIAL
    assert daily_pipeline_exit_code(report) == 1
    assert len(intel_calls.calls) == 1


@pytest.mark.asyncio
async def test_partial_collection_with_no_new_snapshots_is_partial_not_healthy() -> None:
    collection = _batch_report(
        status=BatchStatus.PARTIAL,
        results=(
            _source_run_result(created_snapshot_count=0, unchanged_count=1),
            _source_run_result(
                source_id="langgraph_pypi",
                status=CollectionStatus.FAILED,
                created_snapshot_count=0,
                unchanged_count=0,
                error_category="fetch_failed",
            ),
        ),
    )
    report, _collection_calls, intel_calls = await _run(collection=collection, intelligence=None)

    assert report.status is CombinedPipelineStatus.PARTIAL
    assert daily_pipeline_exit_code(report) == 1
    assert intel_calls.calls == []


@pytest.mark.asyncio
async def test_total_collection_failure_skips_intelligence() -> None:
    collection = _batch_report(
        status=BatchStatus.FAILED,
        results=(
            _source_run_result(
                status=CollectionStatus.FAILED,
                created_snapshot_count=0,
                unchanged_count=0,
                error_category="fetch_failed",
            ),
        ),
    )
    report, _collection_calls, intel_calls = await _run(collection=collection, intelligence=None)

    assert report.status is CombinedPipelineStatus.FAILED
    assert daily_pipeline_exit_code(report) == 2
    assert intel_calls.calls == []


@pytest.mark.asyncio
async def test_intelligence_review_result_is_reported_as_review() -> None:
    intelligence_report = _digest_report(status="review", digest_status="review", claim_count=1)
    report, _collection, _intel = await _run(
        collection=_batch_report(status=BatchStatus.OK), intelligence=intelligence_report
    )

    assert report.status is CombinedPipelineStatus.REVIEW
    assert daily_pipeline_exit_code(report) == 1


@pytest.mark.asyncio
async def test_intelligence_fatal_result_is_reported_as_failed() -> None:
    # A status outside the known vocabulary hits intelligence.run.exit_code_for's
    # fail-closed catch-all (returns 2) -- exactly PR #111's own behaviour.
    intelligence_report = _digest_report(status="failed", digest_status="failed")
    report, _collection, _intel = await _run(
        collection=_batch_report(status=BatchStatus.OK), intelligence=intelligence_report
    )

    assert report.status is CombinedPipelineStatus.FAILED
    assert daily_pipeline_exit_code(report) == 2


# -- window-boundary propagation (the bug this module exists to fix) ------


@pytest.mark.asyncio
async def test_intelligence_receives_the_real_collection_boundary_not_a_fixed_window() -> None:
    collection = _batch_report(
        started_at=datetime(2026, 9, 12, 6, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 9, 12, 6, 0, 47, tzinfo=UTC),
    )
    _report, _collection_calls, intel_calls = await _run(
        collection=collection, intelligence=_digest_report()
    )

    assert len(intel_calls.calls) == 1
    call = intel_calls.calls[0]
    assert call["window_start"] == datetime(2026, 9, 12, 6, 0, 0, tzinfo=UTC)
    # end = collection.finished_at + the 1-second safety margin, never a
    # previous-complete-utc-day or fixed-lookback derived value.
    assert call["window_end"] == datetime(2026, 9, 12, 6, 0, 48, tzinfo=UTC)
    assert call["digest_date"] == _NOW.date()


@pytest.mark.asyncio
async def test_intelligence_limit_defaults_to_five_and_is_overridable() -> None:
    _report, _collection_calls, intel_calls = await _run(
        collection=_batch_report(), intelligence=_digest_report()
    )
    assert intel_calls.calls[0]["limit"] == 5

    _report2, _collection_calls2, intel_calls2 = await _run(
        collection=_batch_report(), intelligence=_digest_report(), intelligence_limit=2
    )
    assert intel_calls2.calls[0]["limit"] == 2


@pytest.mark.asyncio
async def test_rerun_with_nothing_new_is_idempotent_and_healthy() -> None:
    """A rerun where every source finds only already-seen (unchanged)
    entries -- ingestion's own dedup already makes this safe; the
    orchestrator must treat it as a healthy no-op, not an error."""
    rerun_collection = _batch_report(
        status=BatchStatus.OK,
        results=(
            _source_run_result(created_item_count=0, created_snapshot_count=0, unchanged_count=4),
            _source_run_result(
                source_id="langchain_pypi",
                created_item_count=0,
                created_snapshot_count=0,
                unchanged_count=1,
            ),
        ),
    )
    report, _collection_calls, intel_calls = await _run(
        collection=rerun_collection, intelligence=None
    )

    assert report.status is CombinedPipelineStatus.NO_UPDATES
    assert daily_pipeline_exit_code(report) == 0
    assert intel_calls.calls == []


# -- _combine_status: exhaustive, zero-I/O ---------------------------------


def test_combine_status_published() -> None:
    report = _digest_report(status="published", published=True)
    assert (
        _combine_status(BatchStatus.OK, report, new_snapshot_count=report.selected_snapshot_count)
        is CombinedPipelineStatus.PUBLISHED
    )


def test_combine_status_clean_zero_change() -> None:
    report = _digest_report(status="draft")
    assert (
        _combine_status(BatchStatus.OK, report, new_snapshot_count=report.selected_snapshot_count)
        is CombinedPipelineStatus.CLEAN_ZERO_CHANGE
    )


def test_combine_status_review() -> None:
    report = _digest_report(status="review")
    assert (
        _combine_status(BatchStatus.OK, report, new_snapshot_count=report.selected_snapshot_count)
        is CombinedPipelineStatus.REVIEW
    )


def test_combine_status_partial_intelligence() -> None:
    report = _digest_report(status="partial")
    assert (
        _combine_status(BatchStatus.OK, report, new_snapshot_count=report.selected_snapshot_count)
        is CombinedPipelineStatus.PARTIAL
    )


def test_combine_status_collection_partial_overrides_published() -> None:
    published = _digest_report(status="published", published=True)
    assert (
        _combine_status(
            BatchStatus.PARTIAL,
            published,
            new_snapshot_count=published.selected_snapshot_count,
        )
        is CombinedPipelineStatus.PARTIAL
    )


def test_combine_status_intelligence_fatal_overrides_everything() -> None:
    fatal = _digest_report(status="bogus_unknown_status")
    assert (
        _combine_status(
            BatchStatus.PARTIAL, fatal, new_snapshot_count=fatal.selected_snapshot_count
        )
        is CombinedPipelineStatus.FAILED
    )
    assert (
        _combine_status(BatchStatus.OK, fatal, new_snapshot_count=fatal.selected_snapshot_count)
        is CombinedPipelineStatus.FAILED
    )


def test_combine_status_overflow_is_capped_not_healthy() -> None:
    """Collection created more snapshots than intelligence could select
    this run (the SQL LIMIT truncated the candidate list) -- must never
    be reported as a healthy published/clean-zero-change result."""
    published = _digest_report(status="published", published=True, selected_snapshot_count=5)
    assert (
        _combine_status(BatchStatus.OK, published, new_snapshot_count=20)
        is CombinedPipelineStatus.CAPPED
    )

    clean = _digest_report(status="draft", selected_snapshot_count=5, processed_snapshot_count=5)
    assert (
        _combine_status(BatchStatus.OK, clean, new_snapshot_count=20)
        is CombinedPipelineStatus.CAPPED
    )


def test_combine_status_overflow_never_hides_a_more_severe_outcome() -> None:
    """Overflow detection only overrides an otherwise-healthy exit 0 --
    it must not soften a genuinely worse outcome."""
    review = _digest_report(status="review", selected_snapshot_count=5)
    assert (
        _combine_status(BatchStatus.OK, review, new_snapshot_count=20)
        is CombinedPipelineStatus.REVIEW
    )

    fatal = _digest_report(status="bogus_unknown_status", selected_snapshot_count=5)
    assert (
        _combine_status(BatchStatus.PARTIAL, fatal, new_snapshot_count=20)
        is CombinedPipelineStatus.FAILED
    )

    published = _digest_report(status="published", published=True, selected_snapshot_count=5)
    assert (
        _combine_status(BatchStatus.PARTIAL, published, new_snapshot_count=20)
        is CombinedPipelineStatus.PARTIAL
    )


# -- safe final JSON --------------------------------------------------------


@pytest.mark.asyncio
async def test_render_daily_pipeline_report_is_safe_and_round_trips() -> None:
    collection = _batch_report(
        status=BatchStatus.PARTIAL,
        results=(
            _source_run_result(),
            _source_run_result(
                source_id="langgraph_pypi",
                status=CollectionStatus.FAILED,
                created_snapshot_count=0,
                unchanged_count=0,
                error_category="fetch_failed",
            ),
        ),
    )
    intelligence_report = _digest_report(
        status="partial",
        digest_status="draft",
        failed_snapshot_count=1,
        failures=[{"error": "ValueError", "item_id": str(new_id()), "snapshot_id": str(new_id())}],
    )
    report, _collection_calls, _intel = await _run(
        collection=collection, intelligence=intelligence_report
    )

    rendered = render_daily_pipeline_report(report)
    assert "\n" not in rendered
    parsed = json.loads(rendered)

    assert set(parsed) == {
        "job_run_id",
        "started_at",
        "finished_at",
        "status",
        "intelligence_window_start",
        "intelligence_window_end",
        "collection",
        "intelligence",
    }
    assert parsed["status"] == "partial"

    blob = rendered.lower()
    for forbidden in (
        "http://",
        "https://",
        "database_url",
        "postgres://",
        "anthropic_api_key",
        "password",
        "secret",
    ):
        assert forbidden not in blob


def test_render_daily_pipeline_report_handles_a_skipped_intelligence_stage() -> None:
    report = DailyPipelineReport(
        job_run_id=new_id(),
        started_at=_NOW,
        finished_at=_NOW + timedelta(seconds=10),
        status=CombinedPipelineStatus.NO_UPDATES,
        intelligence_window_start=None,
        intelligence_window_end=None,
        collection=_batch_report(),
        intelligence=None,
    )
    parsed = json.loads(render_daily_pipeline_report(report))
    assert parsed["intelligence"] is None
    assert parsed["intelligence_window_start"] is None
    assert parsed["intelligence_window_end"] is None


# -- CLI argument parsing ----------------------------------------------------


def test_parse_args_defaults() -> None:
    args = _parse_args([])
    assert args.source_ids == PRODUCTION_RSS_SOURCE_IDS
    assert args.concurrency == 3
    assert args.intelligence_limit == 5
    assert args.title is None


def test_parse_args_overrides() -> None:
    args = _parse_args(
        [
            "--source-id",
            "openai_news",
            "--concurrency",
            "1",
            "--limit",
            "2",
            "--title",
            "Custom",
        ]
    )
    assert args.source_ids == ("openai_news",)
    assert args.concurrency == 1
    assert args.intelligence_limit == 2
    assert args.title == "Custom"


def test_concurrency_arg_rejects_out_of_range() -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        _concurrency_arg("99")


def test_positive_int_rejects_zero() -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        _positive_int("0")


# -- start-up failure path ----------------------------------------------------


def test_emit_start_failure_prints_safe_json_and_returns_two(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = _emit_start_failure(SourceSelectionError("no source with id 'bogus'"))
    assert exit_code == 2
    printed = json.loads(capsys.readouterr().out)
    assert printed == {"status": "failed", "error": "SourceSelectionError"}


@pytest.mark.asyncio
async def test_real_infrastructure_wrapper_propagates_a_missing_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_missing_database_url() -> None:
        raise ValueError("DATABASE_URL is not set")

    monkeypatch.setattr(
        "ai_daily_digest.daily_pipeline.DatabaseConfig.from_env",
        staticmethod(_raise_missing_database_url),
    )
    with pytest.raises(ValueError, match="DATABASE_URL"):
        await run_daily_pipeline_with_real_infrastructure()


def test_main_reports_start_failure_for_an_unknown_source_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["--source-id", "not_a_real_source"])
    assert exit_code == 2
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "failed"


def test_main_reports_database_failure_without_leaking_the_dsn(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def _raise_db_error(**_kwargs: Any) -> Any:
        raise OSError("could not connect to server: secret-host:5432")

    monkeypatch.setattr(
        "ai_daily_digest.daily_pipeline.run_daily_pipeline_with_real_infrastructure",
        AsyncMock(side_effect=OSError("could not connect to server: secret-host:5432")),
    )
    exit_code = main([])
    assert exit_code == 2
    out = capsys.readouterr().out
    assert "secret-host" not in out
    printed = json.loads(out)
    assert printed == {"status": "failed", "error": "OSError"}
