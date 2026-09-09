"""Regression tests for the `tests/live/` network guard (`conftest.py`).

These are ordinary fast unit tests -- **not** marked `live` -- so they
run in every suite (`pytest`, `make check`, `make ci`) and make no
network calls. They exercise the guard with controlled mark-expression
strings and fake collection items, never a real pytest sub-run against
a real feed.

Context: PR #96 added `tests/live/test_verified_rss_feeds_live.py` but
nothing stopped a plain `pytest` / `pytest tests/live` from collecting
and running it against the real internet. `conftest.py` now skips live
items unless `-m live` is explicitly selected; this file locks that in.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from tests.live.conftest import (
    _SKIP_REASON,
    _live_execution_selected,
    pytest_collection_modifyitems,
)


class _FakeItem:
    """Stand-in for a collected pytest item: only the two hooks the guard
    calls -- `get_closest_marker` and `add_marker`."""

    def __init__(self, *, live: bool) -> None:
        self._live = live
        self.added_markers: list[pytest.MarkDecorator] = []

    def get_closest_marker(self, name: str) -> pytest.Mark | None:
        if name == "live" and self._live:
            return pytest.mark.live.mark
        return None

    def add_marker(self, marker: pytest.MarkDecorator) -> None:
        self.added_markers.append(marker)


def _run_guard(markexpr: str, items: list[_FakeItem]) -> None:
    """Invoke the real collection hook with a fake config and fake items."""
    config = SimpleNamespace(option=SimpleNamespace(markexpr=markexpr))
    pytest_collection_modifyitems(
        cast(pytest.Config, config),
        cast("list[pytest.Item]", items),
    )


# --- the selection predicate ---------------------------------------------------


@pytest.mark.parametrize(
    "markexpr",
    [
        "",  # bare `pytest` / `pytest tests/live`
        "not live",  # `pytest -m "not live"`
        "not integration and not e2e and not live",  # `make check`
        "integration",
        "integration or e2e",
        "not slow",
    ],
)
def test_live_execution_not_selected(markexpr: str) -> None:
    assert _live_execution_selected(markexpr) is False


@pytest.mark.parametrize(
    "markexpr",
    [
        "live",  # `pytest -m live`
        "live and not slow",
        "e2e or live",
        "not slow and live",
    ],
)
def test_live_execution_selected(markexpr: str) -> None:
    assert _live_execution_selected(markexpr) is True


# --- the collection hook -----------------------------------------------------


def test_no_marker_expression_skips_live_items() -> None:
    live_item = _FakeItem(live=True)
    _run_guard("", [live_item])

    assert len(live_item.added_markers) == 1
    marker = live_item.added_markers[0]
    assert marker.name == "skip"
    assert marker.kwargs["reason"] == _SKIP_REASON


def test_not_live_expression_still_skips_live_items() -> None:
    live_item = _FakeItem(live=True)
    _run_guard("not live", [live_item])

    assert [m.name for m in live_item.added_markers] == ["skip"]


def test_explicit_dash_m_live_permits_execution() -> None:
    live_item = _FakeItem(live=True)
    _run_guard("live", [live_item])

    assert live_item.added_markers == []


def test_ordinary_tests_are_untouched() -> None:
    unit_item = _FakeItem(live=False)
    contract_item = _FakeItem(live=False)
    for markexpr in ("", "not live", "live"):
        _run_guard(markexpr, [unit_item, contract_item])

    assert unit_item.added_markers == []
    assert contract_item.added_markers == []
