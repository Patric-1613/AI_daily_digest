"""Contract checks for the Render deployment blueprint.

The blueprint declares three Free resources -- one FastAPI web service, one
static frontend, one PostgreSQL database -- plus **one team-approved cron
service** for the first live intelligence-pipeline rehearsal (issue #53).
Render bills cron services by runtime with a minimum of USD 1/month per
service; the team explicitly approved that minimum. The approval, first-run
procedure, and exit-code contract are recorded in ``docs/DEPLOYMENT.md``.

These tests fail if a second cron, a worker, a disk, or any other unrelated
paid resource is introduced, or if either secret value is ever written into
the YAML.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

import yaml
from uvicorn.importer import import_from_string

from ai_daily_digest.delivery.api.production import UVICORN_FACTORY, create_production_app

_API_NAME = "ai-daily-digest-api"
_FRONTEND_NAME = "ai-daily-digest-web"
_CRON_NAME = "ai-daily-digest-intelligence"
_DATABASE_NAME = "ai-daily-digest-db"


def _blueprint() -> dict[str, Any]:
    loaded = yaml.safe_load(Path("render.yaml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _services() -> list[dict[str, Any]]:
    services = _blueprint()["services"]
    assert isinstance(services, list)
    return services


def _service(name: str) -> dict[str, Any]:
    matches = [service for service in _services() if service.get("name") == name]
    assert len(matches) == 1, f"expected exactly one service named {name!r}, got {len(matches)}"
    return matches[0]


def test_blueprint_declares_free_api_static_site_postgres_and_one_approved_cron() -> None:
    services = _services()
    databases = _blueprint()["databases"]

    # Exactly these three services, in this order, and nothing else.
    assert [service["name"] for service in services] == [_API_NAME, _FRONTEND_NAME, _CRON_NAME]
    assert [service["type"] for service in services] == ["web", "web", "cron"]

    api = _service(_API_NAME)
    assert (api["name"], api["type"], api["runtime"], api["plan"]) == (
        _API_NAME,
        "web",
        "python",
        "free",
    )

    frontend = _service(_FRONTEND_NAME)
    assert (frontend["name"], frontend["type"], frontend["runtime"]) == (
        _FRONTEND_NAME,
        "web",
        "static",
    )
    assert "plan" not in frontend

    # One Free PostgreSQL database -- unchanged by the cron addition.
    assert databases == [
        {
            "name": _DATABASE_NAME,
            "plan": "free",
            "databaseName": "ai_daily_digest",
            "user": "ai_daily_digest",
        }
    ]


def test_api_start_script_migrates_before_starting_importable_factory() -> None:
    api = _service(_API_NAME)
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
    api = _service(_API_NAME)
    frontend = _service(_FRONTEND_NAME)
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
            "name": _DATABASE_NAME,
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


def test_intelligence_cron_runs_the_approved_rehearsal_command_on_a_utc_schedule() -> None:
    cron = _service(_CRON_NAME)

    assert cron["type"] == "cron"
    assert cron["runtime"] == "python"
    # Lowest declared Render cron compute plan; its minimum monthly cost is the
    # USD 1/month the team approved on issue #53.
    assert cron["plan"] == "0.5c-512mb"
    # 06:00 UTC daily. Render always interprets cron schedules in UTC.
    assert cron["schedule"] == "0 6 * * *"
    assert cron["buildCommand"] == "pip install uv && uv sync --locked --no-dev --no-editable"
    # Run the already-built production console script from .venv directly -- never
    # `uv run`, which can resynchronize (and pull dev/editable deps) at runtime.
    # --previous-complete-utc-day makes the 06:00 UTC run process the last fully
    # elapsed UTC day ([00:00, 24:00)), not just the hours since midnight.
    assert cron["startCommand"] == (
        ".venv/bin/generate-digest --previous-complete-utc-day --since 24h --limit 5"
    )
    assert not cron["startCommand"].startswith("uv run")
    # The digest date is derived in Python, never pinned to a literal date here.
    assert "--digest-date" not in cron["startCommand"]

    # This foundation adds no pre-deploy hook and no attached disk.
    assert "preDeployCommand" not in cron
    assert "disk" not in cron


def test_intelligence_cron_command_uses_no_shell_date_expression() -> None:
    start_command = _service(_CRON_NAME)["startCommand"]

    # The completed-day date comes from Python (previous_complete_utc_day with an
    # injected clock), never a shell/platform date expression that would be
    # unportable across the Render runtime.
    for forbidden in ("$(", "${", "`", "date +", "date -u", "%Y", "%m", "%d", "yesterday"):
        assert forbidden not in start_command, (
            f"shell date expression {forbidden!r} in startCommand"
        )
    assert "--previous-complete-utc-day" in start_command

    # No command substitution anywhere in the Blueprint's executable fields.
    for service in _services():
        for field in ("buildCommand", "startCommand"):
            value = service.get(field, "")
            assert "$(" not in value and "`" not in value


def test_intelligence_cron_injects_database_by_reference_and_prompts_for_the_api_key() -> None:
    cron = _service(_CRON_NAME)
    env = {item["key"]: item for item in cron["envVars"]}

    # Exactly these two env vars -- nothing more is injected into a paid cron run.
    assert set(env) == {"DATABASE_URL", "ANTHROPIC_API_KEY"}

    # DATABASE_URL comes from Render's internal database reference, never a literal.
    assert env["DATABASE_URL"] == {
        "key": "DATABASE_URL",
        "fromDatabase": {
            "name": _DATABASE_NAME,
            "property": "connectionString",
        },
    }
    # ANTHROPIC_API_KEY is a dashboard-only prompt (sync: false).
    assert env["ANTHROPIC_API_KEY"] == {"key": "ANTHROPIC_API_KEY", "sync": False}

    # No cron env var carries an inline value -- each is a reference or a prompt.
    assert all("value" not in item for item in cron["envVars"])


def test_blueprint_introduces_no_second_cron_worker_disk_or_unrelated_paid_resource() -> None:
    blueprint = _blueprint()
    services = _services()

    assert sum(1 for service in services if service["type"] == "cron") == 1
    assert not any(
        service["type"] in {"worker", "pserv", "keyvalue", "redis"} for service in services
    )
    assert not any("disk" in service for service in services)

    # The cron is the only resource permitted a non-Free plan, and only at the
    # approved compute size.
    priced = {
        service["name"]: service.get("plan")
        for service in services
        if service.get("plan") not in (None, "free")
    }
    assert priced == {_CRON_NAME: "0.5c-512mb"}

    # No extra top-level Blueprint sections (envVarGroups, previews, ...).
    assert set(blueprint) == {"services", "databases"}
    assert all(database["plan"] == "free" for database in blueprint["databases"])


def test_blueprint_text_contains_no_inline_anthropic_secret() -> None:
    raw = Path("render.yaml").read_text(encoding="utf-8")

    assert "sk-ant" not in raw
    # ANTHROPIC_API_KEY is named exactly once, as an env-var key, with no value.
    assert raw.count("ANTHROPIC_API_KEY") == 1
    assert "ANTHROPIC_API_KEY:" not in raw
