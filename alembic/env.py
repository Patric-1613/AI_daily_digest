"""Alembic migration environment (docs/adr/0002-postgres-pgvector.md
sections 3, 4, 12.5). Async, bridged through `connection.run_sync(...)`
-- the one genuinely synchronous consumer in an otherwise async-first
persistence layer (section 4: "the one genuinely synchronous consumer is
Alembic, and that is a solved problem").

Sets `target_metadata = shared.db.metadata` and imports every approved
module's ORM model modules so they are all registered against that one
`MetaData` before autogenerate or `upgrade`/`downgrade` runs (section
12.5). A module that adds tables without this import breaks autogenerate
and risks a divergent schema (Consequences: "Shared-kernel registration
discipline" -- a reviewer checklist item for every future migration).
"""

from __future__ import annotations

import asyncio
import logging
from logging.config import fileConfig

from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, async_engine_from_config

import ai_daily_digest.delivery.subscriptions.models

# Import every approved module's ORM model modules here, even though
# this name is otherwise unused in this file -- the import's only job is
# registering each module's tables against shared.db.metadata (below)
# before Alembic compares or applies anything. A future
# intelligence/db/models.py joins this list under its own ADR + PR.
import ai_daily_digest.ingestion.db.models
import ai_daily_digest.intelligence.db.models  # noqa: F401
from ai_daily_digest.shared.config import DatabaseConfig
from ai_daily_digest.shared.db.metadata import metadata as target_metadata
from alembic import context

config = context.config

if config.config_file_name is not None:
    # fileConfig() reconfigures the ROOT logger's handlers/level from
    # alembic.ini's [logger_root] section unconditionally -- passing
    # disable_existing_loggers=False only protects OTHER, unlisted
    # loggers; the root logger is explicitly listed, so its handlers are
    # replaced regardless. Every integration test that runs a migration
    # (test_migrations.py, conftest.py's session fixture) calls into
    # this module through run_alembic() -- observed on CI: the first
    # migration silently stripped pytest's own caplog handler off the
    # root logger, breaking every LATER test's caplog assertions for
    # the rest of that pytest process (6 unrelated failures, none in
    # this PR's own tests, all "caplog.text" empty). Saving and
    # restoring the root logger's handlers/level around the call keeps
    # alembic.ini's [logger_alembic]/[logger_sqlalchemy] formatting for
    # a human running `alembic upgrade` from the CLI, while guaranteeing
    # a host process's own root-logger configuration -- pytest's or
    # anything else's -- always round-trips unchanged.
    _root_logger = logging.getLogger()
    _preserved_handlers = list(_root_logger.handlers)
    _preserved_level = _root_logger.level
    fileConfig(config.config_file_name, disable_existing_loggers=False)
    _root_logger.handlers = _preserved_handlers
    _root_logger.setLevel(_preserved_level)

# DATABASE_URL is read from the environment, never from alembic.ini --
# ADR 0002 section 14: "DATABASE_URL stays an environment setting with
# no committed secret." Alembic reads the exact same variable the
# application does, so there is one source of truth for the connection
# string, never two that can drift.
config.set_main_option("sqlalchemy.url", DatabaseConfig.from_env().database_url)


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live database connection."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations against a live database through the async engine,
    via `connection.run_sync(...)` (section 4)."""
    connectable: AsyncEngine = async_engine_from_config(
        config.get_section(config.config_ini_section, {}) or {},
        prefix="sqlalchemy.",
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
