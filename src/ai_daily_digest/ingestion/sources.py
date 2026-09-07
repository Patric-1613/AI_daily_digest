"""Typed loading and semantic validation of `sources.yaml` -- the
ingestion source registry (`docs/ARCHITECTURE.md`, "Data model / Source").

`sources.yaml` is configuration, not a persisted resource, so its models
live here in the ingestion module rather than `shared/schemas.py` (which
mirrors `docs/API_CONTRACT.md` wire resources only). Secrets never live
in `sources.yaml`; `auth_env` names an environment variable, it does not
carry a value.

`SourceType` is the closed set of source-adapter kinds ADR 0009 section 5
explicitly deferred "until their respective vertical slices are
implemented" -- this OpenAI-RSS slice is the first such implementation,
so the Enum is defined here (its owning domain module, per ADR 0009
section 7), not duplicated.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    field_validator,
    model_validator,
)


def _find_sources_yaml() -> Path:
    """Locate `sources.yaml` at the repository root. Deliberately NOT
    based on this module's own `__file__`: under the non-editable wheel
    install this project's `make`/CI use, this module runs from
    `.venv/.../site-packages/ai_daily_digest/ingestion/` with no
    `sources.yaml` anywhere near it (the wheel ships only
    `src/ai_daily_digest`). `Path.cwd()` does not have that problem --
    `make`, `uv run`, and pytest are always invoked from the repo root.
    Mirrors `intelligence/loaders.py::find_repo_root`'s reasoning; a
    caller outside a checkout passes an explicit `path=` to
    `load_source_registry` instead."""
    for candidate in (Path.cwd(), *Path.cwd().parents):
        registry = candidate / "sources.yaml"
        if registry.exists():
            return registry
    return Path.cwd() / "sources.yaml"


# The Part-A approved priority band: 0 is the highest collection priority,
# 3 the lowest. `sources.yaml` currently uses only 0 and 1; the band is
# stated explicitly so a typo like `priority: 9` fails at load instead of
# silently reordering the schedule.
_MIN_PRIORITY = 0
_MAX_PRIORITY = 3

# An environment-variable *name* only -- `A-Z`, digits and `_`, starting
# with a letter. This rejects a value accidentally pasted where a name
# belongs (a token would contain lowercase/punctuation and fail here).
_EnvVarName = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Z][A-Z0-9_]*$", min_length=1, max_length=128),
]

_NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

# A bare DNS hostname -- lowercase letters, digits, dots and hyphens. No
# scheme, no port, no path, no user-info. Used for the per-source fetch
# allowlist.
_Hostname = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        min_length=1,
        max_length=253,
        # Dotted DNS labels, each 1-63 chars, no leading/trailing hyphen.
        # No look-around: pydantic-core's regex engine (Rust `regex`)
        # does not support it.
        pattern=r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$",
    ),
]


class SourceType(StrEnum):
    """The closed set of source-adapter kinds `sources.yaml` may declare.
    Application behaviour depends directly on this value (which collector
    runs), and the set is deliberately closed -- a new kind is a reviewed
    addition here, not an open string (ADR 0009's governing rule)."""

    RSS = "rss"
    HTML = "html"
    CHANGELOG = "changelog"
    GITHUB_RELEASES_API = "github_releases_api"


class CollectionPolicy(BaseModel):
    """The global collection policy `sources.yaml`'s own comment says the
    first collector must define: one set of bounded network defaults every
    collector shares unless a source overrides a field explicitly.

    The values are deliberately conservative and are justified in
    `sources.yaml`'s `collection_policy:` block; per-source overrides are
    a later change and are not modelled here yet.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # A whole-request ceiling generous enough for a large feed on a slow
    # link (the live OpenAI feed is ~0.7 MB) but far under any scheduler
    # window.
    timeout_seconds: Annotated[float, Field(gt=0, le=120)]
    # Initial attempt plus retries. 3 = initial + 2 retries, the usual
    # ceiling for transient network faults.
    max_attempts: Annotated[int, Field(ge=1, le=6)]
    # Exponential backoff base: attempt N waits roughly
    # `backoff_base_seconds * 2**(N-1)` plus jitter.
    backoff_base_seconds: Annotated[float, Field(gt=0, le=30)]
    # A hard cap on the response body read into memory -- a runaway or
    # malicious response is aborted, not buffered.
    max_response_bytes: Annotated[int, Field(gt=0, le=100 * 1024 * 1024)]
    # A clear, attributable User-Agent with a contact URL (AGENTS.md:
    # "Respect source terms ... and attribution requirements").
    user_agent: _NonEmptyStr


class SourceDefinition(BaseModel):
    """One entry in `sources.yaml`'s `sources:` list. `extra="forbid"`
    so a misspelled or unrecognised key fails loudly at load time rather
    than being silently ignored."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: _NonEmptyStr
    publisher: _NonEmptyStr
    type: SourceType
    url: HttpUrl
    priority: Annotated[int, Field(ge=_MIN_PRIORITY, le=_MAX_PRIORITY)]
    cadence_minutes: Annotated[int, Field(gt=0)]
    # Optional, already-present conventions in the committed file:
    subject: _NonEmptyStr | None = None
    editorial: bool = False
    community_content: bool = False
    # `auth_env` names the environment variable a collector reads a token
    # from -- never the token itself. `auth_optional` says the source is
    # still collectable (rate-limited) without it.
    auth_env: _EnvVarName | None = None
    auth_optional: bool = False
    # Explicit fetch allowlist: the only hosts a collector for this source
    # may request, including after a redirect. Empty means "derive it from
    # `url`'s own host" -- an explicit list is still required for any
    # source whose feed and article links live on different hosts.
    allowed_hosts: tuple[_Hostname, ...] = ()

    @field_validator("url")
    @classmethod
    def _url_must_be_safe(cls, value: HttpUrl) -> HttpUrl:
        if value.scheme != "https":
            raise ValueError(f"source url must be https, got {value.scheme!r}")
        if value.username or value.password:
            raise ValueError("source url must not contain user-info (username:password@)")
        return value

    def host_allowlist(self) -> frozenset[str]:
        """The set of hosts a collector for this source may fetch from.
        `allowed_hosts` when given, otherwise just `url`'s own host."""
        if self.allowed_hosts:
            return frozenset(self.allowed_hosts)
        return frozenset({self.url.host}) if self.url.host else frozenset()

    @model_validator(mode="after")
    def _auth_optional_requires_auth_env(self) -> SourceDefinition:
        if self.auth_optional and self.auth_env is None:
            raise ValueError(
                "auth_optional=true is only meaningful with auth_env set -- "
                "a source with no auth cannot have 'optional' auth"
            )
        return self


class SourceRegistry(BaseModel):
    """The whole `sources.yaml` document: the shared `collection_policy`
    plus every `SourceDefinition`, with source ids proven unique."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    collection_policy: CollectionPolicy
    sources: Annotated[list[SourceDefinition], Field(min_length=1)]

    @model_validator(mode="after")
    def _source_ids_are_unique(self) -> SourceRegistry:
        seen: set[str] = set()
        duplicates: set[str] = set()
        for source in self.sources:
            if source.id in seen:
                duplicates.add(source.id)
            seen.add(source.id)
        if duplicates:
            raise ValueError(f"duplicate source id(s) in sources.yaml: {sorted(duplicates)}")
        return self

    def get(self, source_id: str) -> SourceDefinition:
        """The one `SourceDefinition` with `id == source_id`. Raises
        `KeyError` with a clear message if there is none -- a collector
        naming a source that is not registered is a bug, not an empty
        result."""
        for source in self.sources:
            if source.id == source_id:
                return source
        raise KeyError(f"no source with id {source_id!r} in sources.yaml")


def load_source_registry(path: Path | None = None) -> SourceRegistry:
    """Parse and semantically validate `sources.yaml`. `yaml.safe_load`
    (never `yaml.load`) so the file cannot construct arbitrary Python
    objects. Every structural and semantic rule -- unique ids, supported
    type, https url, non-empty publisher, positive cadence, priority
    band, env-var-name-shaped `auth_env`, correct optional-auth
    representation, and rejection of unknown keys -- is enforced by the
    Pydantic models above; this function only does IO and hands the raw
    mapping to `SourceRegistry`."""
    resolved = path if path is not None else _find_sources_yaml()
    with resolved.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise ValueError(f"{resolved} must contain a top-level mapping, got {type(raw).__name__}")
    return SourceRegistry.model_validate(raw)
