"""Contract checks for the zero-cost Render deployment blueprint."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

import yaml
from uvicorn.importer import import_from_string

from ai_daily_digest.delivery.api.production import UVICORN_FACTORY, create_production_app


def _blueprint() -> dict[str, Any]:
    loaded = yaml.safe_load(Path("render.yaml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_blueprint_contains_only_free_api_static_site_and_postgres() -> None:
    services = _blueprint()["services"]
    databases = _blueprint()["databases"]

    assert [(service["name"], service["runtime"], service["plan"]) for service in services] == [
        ("ai-daily-digest-api", "python", "free"),
        ("ai-daily-digest-web", "static", "free"),
    ]
    assert all(service["type"] == "web" for service in services)
    assert not any(service.get("runtime") in {"cron", "postgres"} for service in services)
    assert databases == [
        {
            "name": "ai-daily-digest-db",
            "plan": "free",
            "databaseName": "ai_daily_digest",
            "user": "ai_daily_digest",
        }
    ]


def test_api_start_script_migrates_before_starting_importable_factory() -> None:
    api = _blueprint()["services"][0]
    command = shlex.split(api["startCommand"])
    start_script = Path(command[0])
    script = start_script.read_text(encoding="utf-8")

    assert command == ["./scripts/start_render.sh"]
    assert start_script.stat().st_mode & 0o111
    assert script.index(".venv/bin/alembic upgrade head") < script.index("exec .venv/bin/uvicorn")
    assert UVICORN_FACTORY in script
    assert '--factory --host 0.0.0.0 --port "${PORT}"' in script
    assert import_from_string(UVICORN_FACTORY) is create_production_app
    assert api["healthCheckPath"] == "/v1/health/live"
    assert api["buildCommand"] == "pip install uv && uv sync --locked --no-dev --no-editable"
    assert "preDeployCommand" not in api


def test_blueprint_prompts_for_public_origins_and_generates_no_committed_secret() -> None:
    api, frontend = _blueprint()["services"]
    api_env = {item["key"]: item for item in api["envVars"]}
    frontend_env = {item["key"]: item for item in frontend["envVars"]}

    assert api_env["FRONTEND_ORIGIN"] == {"key": "FRONTEND_ORIGIN", "sync": False}
    assert api_env["PAGINATION_CURSOR_SECRET"] == {
        "key": "PAGINATION_CURSOR_SECRET",
        "generateValue": True,
    }
    assert api_env["DATABASE_URL"] == {
        "key": "DATABASE_URL",
        "fromDatabase": {
            "name": "ai-daily-digest-db",
            "property": "connectionString",
        },
    }
    assert frontend_env["VITE_API_BASE_URL"] == {
        "key": "VITE_API_BASE_URL",
        "sync": False,
    }
    assert frontend["rootDir"] == "frontend"
    assert frontend["buildCommand"] == "npm ci && npm run build"
    assert frontend["staticPublishPath"] == "./dist"
