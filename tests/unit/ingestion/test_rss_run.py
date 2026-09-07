"""The RSS composition root (`ingestion/rss/run.py`) -- offline. Every
test here injects a fake fetcher and an in-memory session factory;
`main()`'s real engine/HTTP wiring is covered by the integration suite."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import SQLAlchemyError

from ai_daily_digest.ingestion.rss import run as run_module
from ai_daily_digest.ingestion.rss.collector import CollectionReport, CollectionStatus, EntryFailure
from ai_daily_digest.ingestion.rss.run import exit_code_for, main, render_report, run_collection
from ai_daily_digest.ingestion.rss.transport import HttpResponse
from tests.unit.ingestion.fake_repository import InMemorySourceItemRepository
from tests.unit.ingestion.rss_helpers import FakeFetcher, load_fixture, noop_session_factory


def _report(
    status: CollectionStatus, *, failures: tuple[EntryFailure, ...] = ()
) -> CollectionReport:
    return CollectionReport(
        source_id="openai_news",
        started_at=datetime(2026, 9, 5, 12, 0, tzinfo=UTC),
        completed_at=datetime(2026, 9, 5, 12, 0, 3, tzinfo=UTC),
        status=status,
        fetched_entry_count=4,
        created_item_count=4,
        created_snapshot_count=4,
        unchanged_count=0,
        failed_item_count=len(failures),
        fetch_attempts=1,
        failures=failures,
    )


def test_render_report_is_single_line_sorted_json() -> None:
    failure = EntryFailure(raw_index=2, guid=None, link="https://openai.com/x", reason="boom")
    rendered = render_report(_report(CollectionStatus.PARTIAL, failures=(failure,)))

    assert "\n" not in rendered
    payload = json.loads(rendered)
    assert payload["source_id"] == "openai_news"
    assert payload["status"] == "partial"
    assert payload["started_at"] == "2026-09-05T12:00:00+00:00"
    assert payload["failures"] == [
        {"raw_index": 2, "guid": None, "link": "https://openai.com/x", "reason": "boom"}
    ]
    assert list(payload) == sorted(payload)


@pytest.mark.parametrize(
    ("status", "code"),
    [(CollectionStatus.OK, 0), (CollectionStatus.PARTIAL, 1), (CollectionStatus.FAILED, 2)],
)
def test_exit_code_maps_status(status: CollectionStatus, code: int) -> None:
    assert exit_code_for(_report(status)) == code


@pytest.mark.asyncio
async def test_run_collection_selects_openai_news_and_reports_offline() -> None:
    repository = InMemorySourceItemRepository()
    report = await run_collection(
        fetcher=FakeFetcher(
            HttpResponse(status_code=200, body=load_fixture("openai_news_sample.xml"))
        ),
        session_factory=noop_session_factory(),  # type: ignore[arg-type]
        repository_factory=lambda _session: repository,
    )

    assert report.source_id == "openai_news"
    assert report.status is CollectionStatus.OK
    assert report.created_item_count == 4


@pytest.mark.asyncio
async def test_run_collection_reports_failed_for_a_zero_item_feed() -> None:
    repository = InMemorySourceItemRepository()
    report = await run_collection(
        fetcher=FakeFetcher(
            HttpResponse(status_code=200, body=load_fixture("openai_news_empty.xml"))
        ),
        session_factory=noop_session_factory(),  # type: ignore[arg-type]
        repository_factory=lambda _session: repository,
    )

    assert report.status is CollectionStatus.FAILED
    assert exit_code_for(report) == 2


def test_main_reports_failed_on_stdout_when_database_url_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`main()` never raises: a missing `DATABASE_URL` is reported as JSON
    on stdout with exit 2. No engine is built, no socket opened."""
    monkeypatch.delenv("DATABASE_URL", raising=False)

    code = main()

    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"source_id": "openai_news", "status": "failed", "error": "ValueError"}


def test_main_reports_failed_without_leaking_the_dsn_on_a_database_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A preflight `SQLAlchemyError` (e.g. database unreachable) is
    caught: exit 2, JSON on stdout, and only the exception TYPE -- never
    the DSN the error message may contain."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@secret-host:5432/db")

    async def _boom(_engine: object) -> None:
        raise SQLAlchemyError('connection to server at "secret-host" (1.2.3.4) failed')

    monkeypatch.setattr(run_module, "_preflight", _boom)

    code = main()

    captured = capsys.readouterr().out
    assert code == 2
    assert json.loads(captured) == {
        "source_id": "openai_news",
        "status": "failed",
        "error": "SQLAlchemyError",
    }
    assert "secret-host" not in captured
    assert "pw" not in captured
