"""Typed configuration boundaries, read from the environment exactly
once by the process composition root (docs/adr/0002-postgres-pgvector.md
section 12.6, section 14). No module reads `os.environ` directly for a
value this file already validates -- that keeps "what a setting means"
and "how it is parsed" in one place, and keeps secrets out of ad hoc
`os.environ[...]` call sites scattered through the codebase.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

_DEFAULT_POOL_SIZE = 5
_DEFAULT_MAX_OVERFLOW = 5
_DEFAULT_POOL_PRE_PING = True
_DEFAULT_POOL_RECYCLE_SECONDS = 1800
_DEFAULT_CONNECT_TIMEOUT_SECONDS = 5
_DEFAULT_STATEMENT_TIMEOUT_MS = 10_000
_DEFAULT_READINESS_TIMEOUT_SECONDS = 2.0
_ASYNC_POSTGRES_SCHEME = "postgresql+psycopg"
_PROVIDER_POSTGRES_SCHEMES = frozenset({"postgres", "postgresql"})


@dataclass(frozen=True)
class DatabaseConfig:  # pylint: disable=too-many-instance-attributes
    """Validated PostgreSQL connection configuration.

    Built once, by `from_env()`, at the process composition root -- the
    API's ASGI entrypoint or the worker's `async def main()` (ADR 0002
    section 12.6) -- then passed down to `shared/db/engine.py`. Nothing
    else constructs one from raw environment variables.

    Pool and timeout fields have Phase-1 bounded defaults (ADR 0002
    section 14) and are exposed here as typed overrides only so a
    deployment can tune them without a code edit; `.env.example` is
    deliberately unchanged by this ADR's implementation PR (section 16)
    -- these overrides are optional operator knobs, not a required
    Phase-1 setting.
    """

    database_url: str
    pool_size: int = _DEFAULT_POOL_SIZE
    max_overflow: int = _DEFAULT_MAX_OVERFLOW
    pool_pre_ping: bool = _DEFAULT_POOL_PRE_PING
    pool_recycle_seconds: int = _DEFAULT_POOL_RECYCLE_SECONDS
    connect_timeout_seconds: int = _DEFAULT_CONNECT_TIMEOUT_SECONDS
    statement_timeout_ms: int = _DEFAULT_STATEMENT_TIMEOUT_MS
    readiness_timeout_seconds: float = _DEFAULT_READINESS_TIMEOUT_SECONDS

    def __repr__(self) -> str:
        # `database_url` carries the host, port, database name, and
        # credentials (ADR 0002 section 14: "never logs it or any
        # component of it"). Overriding __repr__ (not just relying on
        # callers to remember not to log the field) means an accidental
        # `logger.info("%s", config)` or an uncaught-exception traceback
        # that happens to print this object still cannot leak it.
        return (
            f"{type(self).__name__}(database_url='***', "
            f"pool_size={self.pool_size}, max_overflow={self.max_overflow}, "
            f"pool_pre_ping={self.pool_pre_ping}, "
            f"pool_recycle_seconds={self.pool_recycle_seconds}, "
            f"connect_timeout_seconds={self.connect_timeout_seconds}, "
            f"statement_timeout_ms={self.statement_timeout_ms}, "
            f"readiness_timeout_seconds={self.readiness_timeout_seconds})"
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> DatabaseConfig:
        """Read and validate `DATABASE_URL` plus the optional bounded
        pool/timeout overrides. `env` defaults to `os.environ`; tests
        pass a plain `dict` instead of monkeypatching global process
        state.

        Raises `ValueError` -- never returns a config with a blank DSN --
        so a database-backed feature that is misconfigured fails loudly
        at startup, per this ADR's "Naming a dependency as required
        without providing its probe ... must prevent normal startup"
        sibling rule (ADR 0010) applied to configuration itself.
        """
        source = env if env is not None else os.environ
        database_url = source.get("DATABASE_URL", "").strip()
        if not database_url:
            raise ValueError(
                "DATABASE_URL is not set. Copy .env.example to .env and set a "
                "postgresql+psycopg://USER:PASSWORD@HOST:5432/DBNAME connection string."
            )
        return cls(
            database_url=_normalize_database_url(database_url),
            pool_size=_int_override(source, "DATABASE_POOL_SIZE", _DEFAULT_POOL_SIZE),
            max_overflow=_int_override(source, "DATABASE_MAX_OVERFLOW", _DEFAULT_MAX_OVERFLOW),
            pool_pre_ping=_bool_override(source, "DATABASE_POOL_PRE_PING", _DEFAULT_POOL_PRE_PING),
            pool_recycle_seconds=_int_override(
                source, "DATABASE_POOL_RECYCLE_SECONDS", _DEFAULT_POOL_RECYCLE_SECONDS
            ),
            connect_timeout_seconds=_int_override(
                source, "DATABASE_CONNECT_TIMEOUT_SECONDS", _DEFAULT_CONNECT_TIMEOUT_SECONDS
            ),
            statement_timeout_ms=_int_override(
                source, "DATABASE_STATEMENT_TIMEOUT_MS", _DEFAULT_STATEMENT_TIMEOUT_MS
            ),
            readiness_timeout_seconds=_float_override(
                source,
                "DATABASE_READINESS_TIMEOUT_SECONDS",
                _DEFAULT_READINESS_TIMEOUT_SECONDS,
            ),
        )


def _normalize_database_url(database_url: str) -> str:
    """Return a psycopg-explicit URL without exposing any URL component.

    Managed PostgreSQL providers, including Render, expose connection
    strings with a driver-neutral ``postgres://`` or ``postgresql://``
    scheme. SQLAlchemy's async engine must be told to select psycopg 3
    explicitly. Local and CI callers may continue to provide the canonical
    ``postgresql+psycopg://`` form directly.
    """
    scheme, separator, remainder = database_url.partition("://")
    normalized_scheme = scheme.lower()
    if not separator or not remainder:
        raise ValueError("DATABASE_URL must be a complete PostgreSQL connection string")
    if normalized_scheme == _ASYNC_POSTGRES_SCHEME:
        return f"{_ASYNC_POSTGRES_SCHEME}://{remainder}"
    if normalized_scheme in _PROVIDER_POSTGRES_SCHEMES:
        return f"{_ASYNC_POSTGRES_SCHEME}://{remainder}"
    raise ValueError("DATABASE_URL must use a PostgreSQL connection-string scheme")


def _int_override(source: Mapping[str, str], name: str, default: int) -> int:
    raw = source.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _float_override(source: Mapping[str, str], name: str, default: float) -> float:
    raw = source.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


def _bool_override(source: Mapping[str, str], name: str, default: bool) -> bool:
    raw = source.get(name)
    if raw is None or raw.strip() == "":
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean (true/false), got {raw!r}")
