"""Source-registry loading and semantic validation of `sources.yaml`."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from ai_daily_digest.ingestion.sources import (
    SourceType,
    load_source_registry,
)

_VALID_SOURCE = {
    "id": "openai_news",
    "publisher": "OpenAI",
    "type": "rss",
    "url": "https://openai.com/news/rss.xml",
    "priority": 0,
    "cadence_minutes": 360,
}
_VALID_POLICY = {
    "timeout_seconds": 15,
    "max_attempts": 3,
    "backoff_base_seconds": 0.5,
    "max_response_bytes": 5_242_880,
    "user_agent": "ai-daily-digest/0.1 (+https://example.invalid)",
}


def _write_registry(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "sources.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def test_committed_sources_yaml_loads_and_validates() -> None:
    registry = load_source_registry()
    assert registry.get("openai_news").type is SourceType.RSS
    assert registry.get("openai_news").url.scheme == "https"
    # The global policy the first collector must define is present.
    assert registry.collection_policy.timeout_seconds > 0
    assert registry.collection_policy.max_attempts >= 1
    assert registry.collection_policy.max_response_bytes > 0
    assert registry.collection_policy.user_agent.strip()


def test_duplicate_source_ids_are_rejected(tmp_path: Path) -> None:
    path = _write_registry(
        tmp_path,
        {
            "collection_policy": _VALID_POLICY,
            "sources": [_VALID_SOURCE, {**_VALID_SOURCE, "url": "https://openai.com/other.xml"}],
        },
    )
    with pytest.raises(ValidationError, match="duplicate source id"):
        load_source_registry(path)


def test_unsupported_source_type_is_rejected(tmp_path: Path) -> None:
    path = _write_registry(
        tmp_path,
        {"collection_policy": _VALID_POLICY, "sources": [{**_VALID_SOURCE, "type": "gopher"}]},
    )
    with pytest.raises(ValidationError):
        load_source_registry(path)


def test_non_https_url_is_rejected(tmp_path: Path) -> None:
    path = _write_registry(
        tmp_path,
        {
            "collection_policy": _VALID_POLICY,
            "sources": [{**_VALID_SOURCE, "url": "http://openai.com/news/rss.xml"}],
        },
    )
    with pytest.raises(ValidationError, match="https"):
        load_source_registry(path)


def test_url_with_userinfo_is_rejected(tmp_path: Path) -> None:
    path = _write_registry(
        tmp_path,
        {
            "collection_policy": _VALID_POLICY,
            "sources": [{**_VALID_SOURCE, "url": "https://user:pw@openai.com/news/rss.xml"}],
        },
    )
    with pytest.raises(ValidationError, match="user-info"):
        load_source_registry(path)


def test_allowed_hosts_shape_is_validated(tmp_path: Path) -> None:
    path = _write_registry(
        tmp_path,
        {
            "collection_policy": _VALID_POLICY,
            "sources": [{**_VALID_SOURCE, "allowed_hosts": ["openai.com/path"]}],
        },
    )
    with pytest.raises(ValidationError):
        load_source_registry(path)


def test_host_allowlist_falls_back_to_the_url_host(tmp_path: Path) -> None:
    path = _write_registry(
        tmp_path,
        {"collection_policy": _VALID_POLICY, "sources": [_VALID_SOURCE]},
    )
    source = load_source_registry(path).get("openai_news")
    assert source.host_allowlist() == frozenset({"openai.com"})


def test_committed_openai_news_declares_an_explicit_allowlist() -> None:
    source = load_source_registry().get("openai_news")
    assert source.host_allowlist() == frozenset({"openai.com", "www.openai.com"})


def test_empty_publisher_is_rejected(tmp_path: Path) -> None:
    path = _write_registry(
        tmp_path,
        {"collection_policy": _VALID_POLICY, "sources": [{**_VALID_SOURCE, "publisher": "  "}]},
    )
    with pytest.raises(ValidationError):
        load_source_registry(path)


@pytest.mark.parametrize("cadence", [0, -30])
def test_non_positive_cadence_is_rejected(tmp_path: Path, cadence: int) -> None:
    path = _write_registry(
        tmp_path,
        {
            "collection_policy": _VALID_POLICY,
            "sources": [{**_VALID_SOURCE, "cadence_minutes": cadence}],
        },
    )
    with pytest.raises(ValidationError):
        load_source_registry(path)


@pytest.mark.parametrize("priority", [-1, 4, 99])
def test_priority_out_of_band_is_rejected(tmp_path: Path, priority: int) -> None:
    path = _write_registry(
        tmp_path,
        {"collection_policy": _VALID_POLICY, "sources": [{**_VALID_SOURCE, "priority": priority}]},
    )
    with pytest.raises(ValidationError):
        load_source_registry(path)


@pytest.mark.parametrize("auth_env", ["not-a-name", "lowercase_token", "HAS SPACE", "sk-secret"])
def test_auth_env_must_be_an_env_var_name(tmp_path: Path, auth_env: str) -> None:
    path = _write_registry(
        tmp_path,
        {"collection_policy": _VALID_POLICY, "sources": [{**_VALID_SOURCE, "auth_env": auth_env}]},
    )
    with pytest.raises(ValidationError):
        load_source_registry(path)


def test_auth_optional_without_auth_env_is_rejected(tmp_path: Path) -> None:
    path = _write_registry(
        tmp_path,
        {
            "collection_policy": _VALID_POLICY,
            "sources": [{**_VALID_SOURCE, "auth_optional": True}],
        },
    )
    with pytest.raises(ValidationError, match="auth_optional"):
        load_source_registry(path)


def test_optional_authentication_is_represented(tmp_path: Path) -> None:
    path = _write_registry(
        tmp_path,
        {
            "collection_policy": _VALID_POLICY,
            "sources": [
                {**_VALID_SOURCE, "auth_env": "GITHUB_TOKEN", "auth_optional": True},
            ],
        },
    )
    source = load_source_registry(path).get("openai_news")
    assert source.auth_env == "GITHUB_TOKEN"
    assert source.auth_optional is True


def test_unknown_field_fails_clearly(tmp_path: Path) -> None:
    path = _write_registry(
        tmp_path,
        {
            "collection_policy": _VALID_POLICY,
            "sources": [{**_VALID_SOURCE, "colour": "blue"}],
        },
    )
    with pytest.raises(ValidationError, match="colour"):
        load_source_registry(path)


def test_missing_collection_policy_is_rejected(tmp_path: Path) -> None:
    path = _write_registry(tmp_path, {"sources": [_VALID_SOURCE]})
    with pytest.raises(ValidationError, match="collection_policy"):
        load_source_registry(path)
