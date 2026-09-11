"""Offline tests for the official-article composition root and CLI boundary."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from ai_daily_digest.ingestion.article import run as run_module
from ai_daily_digest.ingestion.article.collector import (
    ArticleCollectionReport,
    ArticleCollectionStatus,
    ArticleFailure,
    ArticleFailureCategory,
)
from ai_daily_digest.ingestion.article.run import (
    ArticleSelectionError,
    exit_code_for,
    main,
    render_report,
    resolve_article_source,
)
from ai_daily_digest.ingestion.sources import load_source_registry

_ANTHROPIC_URL = "https://www.anthropic.com/news/100k-context-windows"


def _report(status: ArticleCollectionStatus) -> ArticleCollectionReport:
    failures = (
        ()
        if status is ArticleCollectionStatus.OK
        else (
            ArticleFailure(
                index=0,
                url=_ANTHROPIC_URL,
                category=ArticleFailureCategory.PARSE_FAILED,
            ),
        )
    )
    return ArticleCollectionReport(
        source_id="anthropic_news",
        started_at=datetime(2026, 9, 11, 12, tzinfo=UTC),
        completed_at=datetime(2026, 9, 11, 12, 0, 2, tzinfo=UTC),
        status=status,
        requested_count=1,
        processed_count=int(not failures),
        created_item_count=int(not failures),
        created_snapshot_count=int(not failures),
        unchanged_count=0,
        failed_item_count=len(failures),
        fetch_attempts=1,
        failures=failures,
    )


def test_resolve_article_source_accepts_html_and_rss_publishers() -> None:
    registry = load_source_registry()

    assert (
        resolve_article_source(registry, "anthropic_news", [_ANTHROPIC_URL]).id == "anthropic_news"
    )
    assert (
        resolve_article_source(
            registry,
            "google_ai_blog",
            ["https://blog.google/innovation-and-ai/example"],
        ).id
        == "google_ai_blog"
    )


def test_resolve_article_source_rejects_url_off_source_allowlist() -> None:
    with pytest.raises(ArticleSelectionError, match="allowlist"):
        resolve_article_source(
            load_source_registry(),
            "anthropic_news",
            ["https://example.com/not-anthropic"],
        )


def test_resolve_article_source_rejects_unknown_source() -> None:
    with pytest.raises(ArticleSelectionError, match="no source"):
        resolve_article_source(load_source_registry(), "unknown", [_ANTHROPIC_URL])


def test_report_is_single_line_json() -> None:
    report = _report(ArticleCollectionStatus.FAILED)

    rendered = render_report(report)

    assert "\n" not in rendered
    assert json.loads(rendered)["failures"][0]["category"] == "parse_failed"


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (ArticleCollectionStatus.OK, 0),
        (ArticleCollectionStatus.PARTIAL, 1),
        (ArticleCollectionStatus.FAILED, 2),
    ],
)
def test_exit_code_for_status(status: ArticleCollectionStatus, code: int) -> None:
    assert exit_code_for(_report(status)) == code


def test_main_rejects_off_allowlist_url_before_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    def _no_engine(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("database must not be touched")

    monkeypatch.setattr(run_module, "build_engine", _no_engine)
    code = main(
        [
            "--source-id",
            "anthropic_news",
            "--url",
            "https://example.com/not-anthropic",
        ]
    )

    assert code == 2
    assert json.loads(capsys.readouterr().out)["error"] == "ArticleSelectionError"
