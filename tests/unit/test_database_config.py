"""Database configuration boundary tests."""

from __future__ import annotations

import pytest

from ai_daily_digest.shared.config import DatabaseConfig


@pytest.mark.parametrize(
    ("provided_scheme", "expected_scheme"),
    [
        ("postgresql+psycopg", "postgresql+psycopg"),
        ("postgresql", "postgresql+psycopg"),
        ("postgres", "postgresql+psycopg"),
        ("POSTGRESQL", "postgresql+psycopg"),
    ],
)
def test_database_config_selects_async_psycopg_for_supported_postgres_urls(
    provided_scheme: str,
    expected_scheme: str,
) -> None:
    config = DatabaseConfig.from_env(
        {"DATABASE_URL": f"{provided_scheme}://user:private-password@db.internal/digest"}
    )

    assert config.database_url == (f"{expected_scheme}://user:private-password@db.internal/digest")


@pytest.mark.parametrize(
    "database_url",
    [
        "mysql://user:private-password@db.internal/digest",
        "sqlite:///tmp/digest.db",
        "not-a-url-private-password",
    ],
)
def test_database_config_rejects_non_postgres_urls_without_echoing_secret(
    database_url: str,
) -> None:
    with pytest.raises(ValueError) as exc_info:
        DatabaseConfig.from_env({"DATABASE_URL": database_url})

    assert "private-password" not in str(exc_info.value)


def test_database_config_repr_redacts_normalized_url() -> None:
    config = DatabaseConfig.from_env(
        {"DATABASE_URL": "postgres://user:private-password@db.internal/digest"}
    )

    assert "private-password" not in repr(config)
    assert "db.internal" not in repr(config)
    assert "database_url='***'" in repr(config)
