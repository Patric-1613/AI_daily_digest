"""Shared test doubles for the OpenAI RSS collector unit tests -- no
network, no database, no wall clock."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ai_daily_digest.ingestion.rss.transport import HttpResponse, TransportError
from ai_daily_digest.ingestion.sources import CollectionPolicy

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "rss"

# The allowlist the committed `openai_news` source declares -- the value
# every transport/collector test passes so a fetch is not rejected for an
# unrelated allowlist reason.
TEST_ALLOWED_HOSTS = frozenset({"openai.com", "www.openai.com"})


def load_fixture(name: str) -> bytes:
    """Read one saved RSS fixture. All collector tests parse a saved
    file, never a live feed (`docs/ENGINEERING_STANDARDS.md`)."""
    return (FIXTURES_DIR / name).read_bytes()


def build_policy(**overrides: object) -> CollectionPolicy:
    """A `CollectionPolicy` with small, test-friendly values. Overridable
    per test (e.g. `max_attempts=2`)."""
    values: dict[str, object] = {
        "timeout_seconds": 5.0,
        "max_attempts": 3,
        "backoff_base_seconds": 0.01,
        "max_response_bytes": 1_000_000,
        "user_agent": "ai-daily-digest-test/0.0",
    }
    values.update(overrides)
    return CollectionPolicy.model_validate(values)


class FakeFetcher:
    """An `HttpFetcher` that replays a scripted sequence of outcomes.

    Each element of `outcomes` is either an `HttpResponse` (returned) or
    a `TransportError` instance (raised). The last outcome repeats if
    `fetch` is called more times than the script has entries.
    """

    def __init__(self, *outcomes: HttpResponse | TransportError) -> None:
        if not outcomes:
            raise ValueError("FakeFetcher needs at least one scripted outcome")
        self._script = list(outcomes)
        self._calls: list[str] = []
        self.received_headers: list[Mapping[str, str]] = []
        self.received_allowed_hosts: list[frozenset[str]] = []

    @property
    def call_count(self) -> int:
        return len(self._calls)

    async def fetch(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int,
        allowed_hosts: frozenset[str],
    ) -> HttpResponse:
        self._calls.append(url)
        self.received_headers.append(dict(headers))
        self.received_allowed_hosts.append(allowed_hosts)
        index = min(len(self._calls) - 1, len(self._script) - 1)
        outcome = self._script[index]
        if isinstance(outcome, TransportError):
            raise outcome
        return outcome


class RecordingSleep:
    """A drop-in for `asyncio.sleep` that records every requested delay
    and never actually waits -- retry-schedule assertions with no test
    latency."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


class _NoOpSession:
    """The minimal surface `ingestion.service.ingest_document` touches on
    a session: `commit`, `rollback`, and the async-context-manager
    protocol. The in-memory repository does the real bookkeeping."""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def __aenter__(self) -> _NoOpSession:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


def noop_session_factory() -> Callable[[], AbstractAsyncContextManager[_NoOpSession]]:
    """A `session_factory` for the collector: a fresh no-op session per
    entry, matching the real 'one transaction per item' shape. Pass it
    with `# type: ignore[arg-type]` -- `_NoOpSession` is deliberately not
    a real `AsyncSession`."""

    @asynccontextmanager
    async def _open() -> AsyncIterator[_NoOpSession]:
        yield _NoOpSession()

    return _open


def stepping_clock(start: datetime | None = None, step_seconds: int = 1) -> Callable[[], datetime]:
    """A deterministic clock callable: each call returns a timestamp
    `step_seconds` after the previous one, so a `CollectionReport`'s two
    timestamps are fixed and comparable."""
    base = start or datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)
    counter = {"n": 0}

    def _tick() -> datetime:
        current = base + timedelta(seconds=step_seconds * counter["n"])
        counter["n"] += 1
        return current

    return _tick
