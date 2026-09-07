"""The RSS composition root / CLI (`ingestion/rss/run.py`) -- offline.
Every test injects a fake fetcher and an in-memory session factory;
`main()`'s real engine/HTTP wiring is covered by the integration suite."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import SQLAlchemyError

from ai_daily_digest.ingestion.rss import run as run_module
from ai_daily_digest.ingestion.rss.collector import CollectionReport, CollectionStatus, EntryFailure
from ai_daily_digest.ingestion.rss.run import (
    SourceSelectionError,
    exit_code_for,
    main,
    main_openai_news,
    render_report,
    resolve_rss_source,
    run_collection,
)
from ai_daily_digest.ingestion.rss.transport import HttpResponse
from ai_daily_digest.ingestion.sources import load_source_registry
from tests.unit.ingestion.fake_repository import InMemorySourceItemRepository
from tests.unit.ingestion.rss_helpers import FakeFetcher, load_fixture, noop_session_factory

_RSS_SOURCE_IDS = ("openai_news", "langchain_pypi", "langgraph_pypi")

_SAMPLE_FIXTURE = {
    "openai_news": "openai_news_sample.xml",
    "langchain_pypi": "pypi_langchain_sample.xml",
    "langgraph_pypi": "pypi_langgraph_sample.xml",
}


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


# -- resolve_rss_source ------------------------------------------------


@pytest.mark.parametrize("source_id", _RSS_SOURCE_IDS)
def test_resolve_rss_source_accepts_every_configured_rss_source(source_id: str) -> None:
    source = resolve_rss_source(load_source_registry(), source_id)
    assert source.id == source_id
    assert source.type.value == "rss"


def test_resolve_rss_source_rejects_an_unknown_source_id() -> None:
    with pytest.raises(SourceSelectionError, match="no source with id"):
        resolve_rss_source(load_source_registry(), "not_a_real_source")


def test_resolve_rss_source_rejects_a_non_rss_source_id() -> None:
    # `anthropic_news` is type: html in sources.yaml -- no RSS collector applies.
    with pytest.raises(SourceSelectionError, match="not 'rss'"):
        resolve_rss_source(load_source_registry(), "anthropic_news")


# -- run_collection: the offline core, driven by every source id --------


@pytest.mark.asyncio
@pytest.mark.parametrize("source_id", _RSS_SOURCE_IDS)
async def test_run_collection_drives_the_generic_adapter_for_each_source(source_id: str) -> None:
    repository = InMemorySourceItemRepository()
    report = await run_collection(
        source_id=source_id,
        fetcher=FakeFetcher(
            HttpResponse(status_code=200, body=load_fixture(_SAMPLE_FIXTURE[source_id]))
        ),
        session_factory=noop_session_factory(),  # type: ignore[arg-type]
        repository_factory=lambda _session: repository,
    )

    assert report.source_id == source_id
    assert report.status is CollectionStatus.OK
    assert report.created_item_count == report.fetched_entry_count > 0


@pytest.mark.asyncio
async def test_run_collection_reports_failed_for_a_zero_item_feed() -> None:
    repository = InMemorySourceItemRepository()
    report = await run_collection(
        source_id="openai_news",
        fetcher=FakeFetcher(
            HttpResponse(status_code=200, body=load_fixture("openai_news_empty.xml"))
        ),
        session_factory=noop_session_factory(),  # type: ignore[arg-type]
        repository_factory=lambda _session: repository,
    )

    assert report.status is CollectionStatus.FAILED
    assert exit_code_for(report) == 2


@pytest.mark.asyncio
async def test_run_collection_rejects_an_unknown_source_id() -> None:
    with pytest.raises(SourceSelectionError):
        await run_collection(
            source_id="nope",
            fetcher=FakeFetcher(HttpResponse(status_code=200, body=b"<rss/>")),
            session_factory=noop_session_factory(),  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_run_collection_rejects_a_non_rss_source_id() -> None:
    with pytest.raises(SourceSelectionError):
        await run_collection(
            source_id="anthropic_news",
            fetcher=FakeFetcher(HttpResponse(status_code=200, body=b"<rss/>")),
            session_factory=noop_session_factory(),  # type: ignore[arg-type]
        )


# -- main() CLI --------------------------------------------------------


def test_main_requires_a_source_id_argument(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2
    assert "--source-id" in capsys.readouterr().err


def test_main_reports_failed_for_an_unknown_source_id_without_touching_the_db(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    def _no_engine(*_a: object, **_k: object) -> object:
        raise AssertionError("build_engine must not be reached for an unknown source id")

    monkeypatch.setattr(run_module, "build_engine", _no_engine)

    code = main(["--source-id", "totally_unknown"])

    assert code == 2
    assert json.loads(capsys.readouterr().out) == {
        "source_id": "totally_unknown",
        "status": "failed",
        "error": "SourceSelectionError",
    }


def test_main_reports_failed_for_a_non_rss_source_id(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # `anthropic_news` is declared with type "html" in sources.yaml.
    code = main(["--source-id", "anthropic_news"])
    assert code == 2
    assert json.loads(capsys.readouterr().out)["error"] == "SourceSelectionError"


def test_main_reports_failed_on_stdout_when_database_url_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A valid RSS source id but no `DATABASE_URL`: JSON on stdout, exit 2,
    no engine built, no socket opened."""
    monkeypatch.delenv("DATABASE_URL", raising=False)

    code = main(["--source-id", "langchain_pypi"])

    assert code == 2
    assert json.loads(capsys.readouterr().out) == {
        "source_id": "langchain_pypi",
        "status": "failed",
        "error": "ValueError",
    }


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

    code = main(["--source-id", "openai_news"])

    captured = capsys.readouterr().out
    assert code == 2
    assert json.loads(captured) == {
        "source_id": "openai_news",
        "status": "failed",
        "error": "SQLAlchemyError",
    }
    assert "secret-host" not in captured
    assert "pw" not in captured


def test_main_openai_news_selects_openai_news_with_no_arguments(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`collect-openai-rss` with no extra argv runs the generic path for
    `openai_news`."""
    monkeypatch.delenv("DATABASE_URL", raising=False)

    code = main_openai_news([])

    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["source_id"] == "openai_news"


def test_main_openai_news_rejects_source_id_before_any_db_or_network(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`collect-openai-rss --source-id langchain_pypi` exits with argparse
    code 2, before the registry, the database, or the network is touched --
    the compatibility command can never be redirected to another source."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h:5432/d")

    def _no_registry(*_a: object, **_k: object) -> object:
        raise AssertionError("load_source_registry must not run for a rejected argument")

    def _no_engine(*_a: object, **_k: object) -> object:
        raise AssertionError("build_engine must not run for a rejected argument")

    monkeypatch.setattr(run_module, "load_source_registry", _no_registry)
    monkeypatch.setattr(run_module, "build_engine", _no_engine)

    with pytest.raises(SystemExit) as excinfo:
        main_openai_news(["--source-id", "langchain_pypi"])
    assert excinfo.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


@pytest.mark.parametrize("extra", [["langchain_pypi"], ["--nope"], ["--source-id"], ["-x", "y"]])
def test_main_openai_news_rejects_any_extra_argument(
    extra: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main_openai_news(extra)
    assert excinfo.value.code == 2
    assert capsys.readouterr().err  # argparse wrote a usage/error message
