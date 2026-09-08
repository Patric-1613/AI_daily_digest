"""The RSS batch runner core / CLI (`ingestion/rss/batch.py`) -- offline.
Every test injects a fake fetcher and an in-memory session factory;
`main()`'s real engine/HTTP wiring is covered by the integration suite."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ai_daily_digest.ingestion.persistence import IngestionWriteRepository
from ai_daily_digest.ingestion.rss import batch as batch_module
from ai_daily_digest.ingestion.rss.batch import (
    _VERIFIED_RSS_SOURCE_IDS,
    BatchReport,
    BatchStatus,
    SourceRunResult,
    batch_exit_code,
    batch_status,
    error_category_for,
    main,
    render_batch_report,
    resolve_batch_sources,
    run_rss_batch,
)
from ai_daily_digest.ingestion.rss.collector import CollectionReport, CollectionStatus, EntryFailure
from ai_daily_digest.ingestion.rss.run import SourceSelectionError
from ai_daily_digest.ingestion.rss.transport import HttpResponse, PermanentTransportError
from ai_daily_digest.ingestion.sources import SourceType, load_source_registry
from tests.unit.ingestion.fake_repository import InMemorySourceItemRepository
from tests.unit.ingestion.rss_helpers import (
    ConcurrencyProbeFetcher,
    MappingFetcher,
    RecordingSleep,
    load_fixture,
    noop_session_factory,
    stepping_clock,
)

_REGISTRY = load_source_registry()
_T0_CLOCK_START = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)

_FEED_URL = {
    "openai_news": "https://openai.com/news/rss.xml",
    "langchain_pypi": "https://pypi.org/rss/project/langchain/releases.xml",
    "langgraph_pypi": "https://pypi.org/rss/project/langgraph/releases.xml",
}
_FEED_FIXTURE = {
    "openai_news": "openai_news_sample.xml",
    "langchain_pypi": "pypi_langchain_sample.xml",
    "langgraph_pypi": "pypi_langgraph_sample.xml",
}
_EXPECTED_ENTRIES = {"openai_news": 4, "langchain_pypi": 3, "langgraph_pypi": 3}


def _repo_factory(
    repository: IngestionWriteRepository,
) -> Callable[[AsyncSession], IngestionWriteRepository]:
    return lambda _session: repository


def _ok_fetcher(*source_ids: str) -> MappingFetcher:
    return MappingFetcher(
        {
            _FEED_URL[source_id]: HttpResponse(
                status_code=200, body=load_fixture(_FEED_FIXTURE[source_id])
            )
            for source_id in source_ids
        }
    )


async def _run(
    *,
    source_ids: tuple[str, ...],
    fetcher: object,
    repository: InMemorySourceItemRepository | None = None,
    concurrency: int = 3,
    job_run_id: uuid.UUID | None = None,
) -> BatchReport:
    return await run_rss_batch(
        registry=_REGISTRY,
        source_ids=source_ids,
        fetcher=fetcher,  # type: ignore[arg-type]
        session_factory=noop_session_factory(),  # type: ignore[arg-type]
        repository_factory=_repo_factory(repository or InMemorySourceItemRepository()),
        concurrency=concurrency,
        sleep=RecordingSleep(),
        clock=stepping_clock(start=_T0_CLOCK_START),
        job_run_id=job_run_id,
    )


# -- selection --------------------------------------------------------


def test_verified_source_ids_are_the_three_documented_rss_sources() -> None:
    assert _VERIFIED_RSS_SOURCE_IDS == ("openai_news", "langchain_pypi", "langgraph_pypi")
    for source_id in _VERIFIED_RSS_SOURCE_IDS:
        assert _REGISTRY.get(source_id).type is SourceType.RSS


def test_resolve_batch_sources_rejects_an_empty_selection() -> None:
    with pytest.raises(SourceSelectionError, match="no sources selected"):
        resolve_batch_sources(_REGISTRY, [])


def test_resolve_batch_sources_rejects_an_unknown_id() -> None:
    with pytest.raises(SourceSelectionError, match="no source with id"):
        resolve_batch_sources(_REGISTRY, ["openai_news", "not_a_real_source"])


def test_resolve_batch_sources_rejects_a_non_rss_id() -> None:
    # anthropic_news is type: html in sources.yaml
    with pytest.raises(SourceSelectionError, match="not 'rss'"):
        resolve_batch_sources(_REGISTRY, ["anthropic_news"])


def test_resolve_batch_sources_dedupes_preserving_order() -> None:
    resolved = resolve_batch_sources(_REGISTRY, ["langgraph_pypi", "openai_news", "langgraph_pypi"])
    assert [s.id for s in resolved] == ["langgraph_pypi", "openai_news"]


# -- run_rss_batch: happy path --------------------------------------


@pytest.mark.asyncio
async def test_default_verified_set_runs_only_verified_sources() -> None:
    fetcher = _ok_fetcher(*_VERIFIED_RSS_SOURCE_IDS)
    report = await _run(source_ids=_VERIFIED_RSS_SOURCE_IDS, fetcher=fetcher)

    assert report.requested_source_ids == _VERIFIED_RSS_SOURCE_IDS
    assert {r.source_id for r in report.results} == set(_VERIFIED_RSS_SOURCE_IDS)
    # only the three verified feed URLs were fetched, nothing else
    assert set(fetcher.received_urls) == set(_FEED_URL.values())


@pytest.mark.asyncio
async def test_all_verified_sources_succeed() -> None:
    report = await _run(
        source_ids=_VERIFIED_RSS_SOURCE_IDS, fetcher=_ok_fetcher(*_VERIFIED_RSS_SOURCE_IDS)
    )

    assert report.status is BatchStatus.OK
    assert batch_exit_code(report) == 0
    by_id = {r.source_id: r for r in report.results}
    for source_id, expected in _EXPECTED_ENTRIES.items():
        assert by_id[source_id].status is CollectionStatus.OK
        assert by_id[source_id].created_item_count == expected
        assert by_id[source_id].error_category is None

    payload = json.loads(render_batch_report(report))
    assert payload["totals"]["succeeded"] == 3
    assert payload["totals"]["new_snapshots"] == sum(_EXPECTED_ENTRIES.values())
    assert payload["totals"]["items_processed"] == sum(_EXPECTED_ENTRIES.values())


@pytest.mark.asyncio
async def test_one_source_fails_others_still_complete() -> None:
    fetcher = MappingFetcher(
        {
            _FEED_URL["langchain_pypi"]: PermanentTransportError("HTTP 500 from feed"),
            _FEED_URL["openai_news"]: HttpResponse(
                status_code=200, body=load_fixture("openai_news_sample.xml")
            ),
            _FEED_URL["langgraph_pypi"]: HttpResponse(
                status_code=200, body=load_fixture("pypi_langgraph_sample.xml")
            ),
        }
    )
    report = await _run(source_ids=_VERIFIED_RSS_SOURCE_IDS, fetcher=fetcher)

    by_id = {r.source_id: r for r in report.results}
    assert by_id["langchain_pypi"].status is CollectionStatus.FAILED
    assert by_id["langchain_pypi"].error_category == "fetch_failed"
    assert by_id["openai_news"].status is CollectionStatus.OK
    assert by_id["langgraph_pypi"].status is CollectionStatus.OK
    assert report.status is BatchStatus.PARTIAL
    assert batch_exit_code(report) == 1


@pytest.mark.asyncio
async def test_every_source_failing_is_exit_2() -> None:
    fetcher = MappingFetcher(
        {url: PermanentTransportError("HTTP 500") for url in _FEED_URL.values()}
    )
    report = await _run(source_ids=_VERIFIED_RSS_SOURCE_IDS, fetcher=fetcher)

    assert report.status is BatchStatus.FAILED
    assert batch_exit_code(report) == 2
    assert all(r.status is CollectionStatus.FAILED for r in report.results)


@pytest.mark.asyncio
async def test_unknown_selection_raises_before_touching_the_fetcher() -> None:
    fetcher = MappingFetcher({})  # any fetch -> AssertionError

    with pytest.raises(SourceSelectionError):
        await _run(source_ids=("openai_news", "bogus"), fetcher=fetcher)

    assert fetcher.received_urls == []


@pytest.mark.asyncio
async def test_non_rss_selection_raises_before_touching_the_fetcher() -> None:
    fetcher = MappingFetcher({})
    with pytest.raises(SourceSelectionError):
        await _run(source_ids=("anthropic_news",), fetcher=fetcher)
    assert fetcher.received_urls == []


@pytest.mark.asyncio
async def test_duplicate_source_ids_run_once() -> None:
    fetcher = _ok_fetcher("openai_news")
    report = await _run(source_ids=("openai_news", "openai_news"), fetcher=fetcher)

    assert report.requested_source_ids == ("openai_news",)
    assert len(report.results) == 1
    assert fetcher.call_count == 1


@pytest.mark.asyncio
async def test_idempotent_rerun_reports_unchanged() -> None:
    repository = InMemorySourceItemRepository()
    first = await _run(
        source_ids=_VERIFIED_RSS_SOURCE_IDS,
        fetcher=_ok_fetcher(*_VERIFIED_RSS_SOURCE_IDS),
        repository=repository,
    )
    assert sum(r.created_item_count for r in first.results) == sum(_EXPECTED_ENTRIES.values())

    second = await _run(
        source_ids=_VERIFIED_RSS_SOURCE_IDS,
        fetcher=_ok_fetcher(*_VERIFIED_RSS_SOURCE_IDS),
        repository=repository,
    )
    assert second.status is BatchStatus.OK
    by_id = {r.source_id: r for r in second.results}
    for source_id, expected in _EXPECTED_ENTRIES.items():
        assert by_id[source_id].created_item_count == 0
        assert by_id[source_id].created_snapshot_count == 0
        assert by_id[source_id].unchanged_count == expected


@pytest.mark.asyncio
async def test_changed_content_rerun_counts_the_new_snapshot_as_processed() -> None:
    """Regression: a changed-content rerun creates a new DocumentSnapshot
    on an *existing* SourceItem, so `created_item_count` is 0 while
    `created_snapshot_count` is 1. `totals.items_processed` must count the
    snapshot, not the item -- otherwise the changed entry is silently
    dropped from the batch's processed count.

    Exercises the real batch path: first run seeds the four openai_news
    items, second run replays `openai_news_changed.xml` (one entry's
    content changed, three unchanged) through the same in-memory
    repository."""
    repository = InMemorySourceItemRepository()
    feed_url = _FEED_URL["openai_news"]

    await _run(
        source_ids=("openai_news",), fetcher=_ok_fetcher("openai_news"), repository=repository
    )

    changed = await _run(
        source_ids=("openai_news",),
        fetcher=MappingFetcher(
            {feed_url: HttpResponse(status_code=200, body=load_fixture("openai_news_changed.xml"))}
        ),
        repository=repository,
    )

    result = changed.results[0]
    assert result.fetched_entry_count == 4
    assert result.created_item_count == 0
    assert result.created_snapshot_count == 1
    assert result.unchanged_count == 3
    assert result.failed_item_count == 0

    totals = json.loads(render_batch_report(changed))["totals"]
    assert totals["items_processed"] == 4
    assert totals["new_snapshots"] == 1


# -- bounded concurrency ---------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [1, 2, 3])
async def test_concurrency_is_bounded(concurrency: int) -> None:
    fetcher = ConcurrencyProbeFetcher(load_fixture("openai_news_sample.xml"))
    report = await run_rss_batch(
        registry=_REGISTRY,
        source_ids=_VERIFIED_RSS_SOURCE_IDS,
        fetcher=fetcher,
        session_factory=noop_session_factory(),  # type: ignore[arg-type]
        repository_factory=_repo_factory(InMemorySourceItemRepository()),
        concurrency=concurrency,
        sleep=RecordingSleep(),
        clock=stepping_clock(start=_T0_CLOCK_START),
    )

    assert fetcher.max_in_flight <= concurrency
    # with room to overlap, the bound is actually reached (proves it is
    # concurrent, not accidentally serial)
    if concurrency > 1:
        assert fetcher.max_in_flight == concurrency
    assert len(report.results) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [0, 9, -1, 100])
async def test_concurrency_outside_one_to_eight_is_rejected_by_the_core(concurrency: int) -> None:
    """The 1..8 bound is enforced in `run_rss_batch` itself, not only in
    the CLI's argparse layer -- a direct Python caller cannot exceed the
    courteous-crawler ceiling either."""
    fetcher = _ok_fetcher("openai_news")
    with pytest.raises(ValueError, match="concurrency must be between 1 and 8"):
        await _run(source_ids=("openai_news",), fetcher=fetcher, concurrency=concurrency)
    assert fetcher.received_urls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [1, 8])
async def test_concurrency_boundary_values_are_accepted(concurrency: int) -> None:
    report = await _run(
        source_ids=_VERIFIED_RSS_SOURCE_IDS,
        fetcher=_ok_fetcher(*_VERIFIED_RSS_SOURCE_IDS),
        concurrency=concurrency,
    )
    assert report.concurrency == concurrency
    assert report.status is BatchStatus.OK


@pytest.mark.asyncio
async def test_an_unexpected_source_exception_is_isolated_and_not_leaked(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`collect_rss_source` captures every collection failure in its
    report; a raw exception escaping it is unexpected. The batch turns it
    into a `collection_failed` result for that source only, keeps the
    others running, and logs / reports nothing but the exception type."""

    class _RaisingFetcher:
        def __init__(self) -> None:
            self._ok = _ok_fetcher("openai_news", "langgraph_pypi")

        async def fetch(
            self,
            url: str,
            *,
            headers: object,
            timeout_seconds: float,
            max_response_bytes: int,
            allowed_hosts: frozenset[str],
        ) -> HttpResponse:
            if url == _FEED_URL["langchain_pypi"]:
                raise RuntimeError("kaboom with scraped <secret>abc123SECRET</secret>")
            return await self._ok.fetch(
                url,
                headers={},
                timeout_seconds=timeout_seconds,
                max_response_bytes=max_response_bytes,
                allowed_hosts=allowed_hosts,
            )

    fetcher = _RaisingFetcher()

    with caplog.at_level("ERROR"):
        report = await _run(source_ids=_VERIFIED_RSS_SOURCE_IDS, fetcher=fetcher)

    by_id = {r.source_id: r for r in report.results}
    assert by_id["langchain_pypi"].status is CollectionStatus.FAILED
    assert by_id["langchain_pypi"].error_category == "collection_failed"
    assert by_id["openai_news"].status is CollectionStatus.OK
    assert by_id["langgraph_pypi"].status is CollectionStatus.OK
    assert report.status is BatchStatus.PARTIAL
    rendered = render_batch_report(report)
    assert "abc123SECRET" not in rendered
    assert "abc123SECRET" not in caplog.text
    assert "kaboom" not in caplog.text


# -- report: deterministic + secret-safe ----------------------------


@pytest.mark.asyncio
async def test_report_json_is_deterministic_and_leaks_no_transport_detail() -> None:
    secret_url = "https://pypi.org/rss/project/langchain/releases.xml?token=abc123SECRET"
    fetcher = MappingFetcher(
        {
            _FEED_URL["langchain_pypi"]: PermanentTransportError(
                f"boom fetching {secret_url} Authorization: Bearer abc123SECRET"
            ),
            _FEED_URL["openai_news"]: HttpResponse(
                status_code=200, body=load_fixture("openai_news_sample.xml")
            ),
            _FEED_URL["langgraph_pypi"]: HttpResponse(
                status_code=200, body=load_fixture("pypi_langgraph_sample.xml")
            ),
        }
    )
    report = await _run(source_ids=_VERIFIED_RSS_SOURCE_IDS, fetcher=fetcher)
    rendered = render_batch_report(report)

    assert "\n" not in rendered
    payload = json.loads(rendered)
    assert list(payload) == sorted(payload)
    assert payload["status"] == "partial"
    assert [s["source_id"] for s in payload["sources"]] == list(_VERIFIED_RSS_SOURCE_IDS)
    assert "failures" not in payload["sources"][0]
    for needle in ("abc123SECRET", "token=", "Authorization", "?"):
        assert needle not in rendered


@pytest.mark.asyncio
async def test_job_run_id_is_uuid_v7_and_timestamps_are_timezone_aware() -> None:
    report = await _run(
        source_ids=_VERIFIED_RSS_SOURCE_IDS, fetcher=_ok_fetcher(*_VERIFIED_RSS_SOURCE_IDS)
    )
    assert isinstance(report.job_run_id, uuid.UUID)
    assert report.job_run_id.version == 7
    assert report.started_at.tzinfo is not None
    assert report.finished_at.tzinfo is not None
    assert report.finished_at >= report.started_at


@pytest.mark.asyncio
async def test_explicit_job_run_id_is_used_verbatim() -> None:
    fixed = uuid.UUID("018f00d0-0000-7000-8000-0000000000aa")
    report = await _run(
        source_ids=("openai_news",), fetcher=_ok_fetcher("openai_news"), job_run_id=fixed
    )
    assert report.job_run_id == fixed
    assert json.loads(render_batch_report(report))["job_run_id"] == str(fixed)


def _collection_report(status: CollectionStatus, reason: str) -> CollectionReport:
    return CollectionReport(
        source_id="s",
        started_at=_T0_CLOCK_START,
        completed_at=_T0_CLOCK_START,
        status=status,
        fetched_entry_count=0,
        created_item_count=0,
        created_snapshot_count=0,
        unchanged_count=0,
        failed_item_count=1,
        fetch_attempts=1,
        failures=(EntryFailure(raw_index=-1, guid=None, link=None, reason=reason),),
    )


@pytest.mark.parametrize(
    ("status", "reason", "expected"),
    [
        (CollectionStatus.OK, "", None),
        (CollectionStatus.PARTIAL, "", "partial_entry_failures"),
        (CollectionStatus.FAILED, "fetch failed: boom", "fetch_failed"),
        (CollectionStatus.FAILED, "parse failed: boom", "parse_failed"),
        (CollectionStatus.FAILED, "feed anomaly: zero items", "empty_feed"),
        (CollectionStatus.FAILED, "something else entirely", "collection_failed"),
    ],
)
def test_error_category_vocabulary_is_fixed(
    status: CollectionStatus, reason: str, expected: str | None
) -> None:
    assert error_category_for(_collection_report(status, reason)) == expected


def test_batch_status_of_no_results_is_failed() -> None:
    assert batch_status(()) is BatchStatus.FAILED


# -- status / exit-code table --------------------------------------


@pytest.mark.parametrize(
    ("statuses", "expected_status", "expected_code"),
    [
        ([CollectionStatus.OK, CollectionStatus.OK], BatchStatus.OK, 0),
        ([CollectionStatus.OK, CollectionStatus.PARTIAL], BatchStatus.PARTIAL, 1),
        ([CollectionStatus.OK, CollectionStatus.FAILED], BatchStatus.PARTIAL, 1),
        ([CollectionStatus.PARTIAL, CollectionStatus.PARTIAL], BatchStatus.PARTIAL, 1),
        ([CollectionStatus.FAILED, CollectionStatus.FAILED], BatchStatus.FAILED, 2),
    ],
)
def test_status_and_exit_code_table(
    statuses: list[CollectionStatus],
    expected_status: BatchStatus,
    expected_code: int,
) -> None:
    results = tuple(
        SourceRunResult(
            source_id=f"s{i}",
            status=status,
            fetched_entry_count=1,
            created_item_count=1,
            created_snapshot_count=1,
            unchanged_count=0,
            failed_item_count=0,
            fetch_attempts=1,
            error_category=None,
        )
        for i, status in enumerate(statuses)
    )
    assert batch_status(results) is expected_status
    report = BatchReport(
        job_run_id=uuid.uuid4(),
        started_at=_T0_CLOCK_START,
        finished_at=_T0_CLOCK_START,
        requested_source_ids=tuple(r.source_id for r in results),
        concurrency=3,
        status=batch_status(results),
        results=results,
    )
    assert batch_exit_code(report) == expected_code


# -- main() CLI ---------------------------------------------------


def test_main_rejects_an_unknown_source_before_any_db_work(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    def _no_engine(*_a: object, **_k: object) -> object:
        raise AssertionError("build_engine must not be reached for an unknown source id")

    monkeypatch.setattr(batch_module, "build_engine", _no_engine)

    code = main(["--source-id", "totally_unknown"])

    assert code == 2
    assert json.loads(capsys.readouterr().out) == {
        "error": "SourceSelectionError",
        "status": "failed",
    }


def test_main_reports_failed_when_database_url_is_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    code = main([])  # default verified set, valid selection, but no DB

    assert code == 2
    assert json.loads(capsys.readouterr().out) == {"error": "ValueError", "status": "failed"}


@pytest.mark.parametrize(
    ("value", "fragment"),
    [("0", "between"), ("99", "between"), ("abc", "must be an integer")],
)
def test_main_rejects_bad_concurrency_via_argparse(
    value: str, fragment: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--concurrency", value])
    assert excinfo.value.code == 2
    assert fragment in capsys.readouterr().err


def test_main_does_not_leak_the_dsn_on_a_database_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@secret-host:5432/db")

    async def _boom(_engine: object) -> None:
        raise SQLAlchemyError('connection to server at "secret-host" (1.2.3.4) failed')

    monkeypatch.setattr(batch_module, "_preflight", _boom)

    code = main(["--source-id", "openai_news"])

    captured = capsys.readouterr().out
    assert code == 2
    assert json.loads(captured) == {"error": "SQLAlchemyError", "status": "failed"}
    assert "secret-host" not in captured
    assert "pw" not in captured
