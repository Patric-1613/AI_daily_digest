"""Network guard for `tests/live/`: real external services are contacted
only when the caller *explicitly* asks for them with `pytest -m live`.

`tests/README.md`: "opt-in source smoke tests; never part of the normal
local or PR suite." The `pytestmark = pytest.mark.live` on each module
here already lets `-m "not live"` deselect them, but the plain
invocations below carry no mark expression at all and would otherwise
collect and *run* -- making real internet requests -- as an accident:

    pytest
    pytest tests/live
    pytest -m "not live"
    make check   # -m "not integration and not e2e and not live"
    make ci      # includes the -m "not live" suite

This conftest closes that gap: unless live execution was explicitly
selected, every live-marked item in this package is turned into a
`skip` with a clear reason, so none of the commands above can reach the
network.

The selection test is pytest's own `-m` grammar, not a substring match:
`not live` contains the word "live" but must *not* enable live tests
(issue on PR #96, requirement 4). See `_live_execution_selected`.
"""

from __future__ import annotations

import re

import pytest

_LIVE_TOKEN = re.compile(r"(?<![\w-])live(?![\w-])")

_SKIP_REASON = (
    "live external-service tests run only when explicitly selected with "
    "`pytest -m live ...`; this run did not select them, so no network "
    "request is made"
)


def _only_the_live_mark(name: str, /, **_kwargs: str | int | bool | None) -> bool:
    """A hypothetical item whose sole keyword is `"live"` -- the exact
    `ExpressionMatcher` shape `Expression.evaluate` expects."""
    return name == "live"


def _live_execution_selected(markexpr: str) -> bool:
    """Whether `-m <markexpr>` *explicitly selects* live tests.

    Two conditions, both required:

    1. **`live` appears as a literal term in the expression.** A bare
       `pytest` run (empty expression) never selects live tests, and
       `make check` / `make ci` name `live` only to *exclude* it.
    2. **The expression, evaluated with only `live` true, is true.**
       This is what separates `live` / `live and not slow` (select)
       from `not live` (exclude) -- requirement 4: the mere presence of
       the word `live` in `not live` is not permission to run.
    """
    if not markexpr or not _LIVE_TOKEN.search(markexpr):
        return False
    try:
        # pytest's own mark-expression evaluator: not a public API, but
        # the one implementation of pytest's `-m` grammar (parentheses,
        # and/or/not). Reimplementing that grammar here would risk a
        # second parser drifting from pytest's actual semantics. The
        # integration conftest guards `-m integration` the same way.
        from _pytest.mark.expression import Expression

        expression = Expression.compile(markexpr)
    except Exception:  # pylint: disable=broad-exception-caught
        # An unparsable expression is pytest's own error to report; this
        # guard only ever *adds* a skip, so failing closed (treat as not
        # selected) keeps the network safe and lets pytest surface the
        # real usage error.
        return False
    return bool(expression.evaluate(_only_the_live_mark))


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip every live-marked item unless `-m live` was explicitly
    selected. Runs after pytest's own `-m` deselection; any item left
    here that still carries the `live` keyword is one the caller did not
    deliberately ask to run against the network."""
    if _live_execution_selected(config.option.markexpr or ""):
        return
    skip_live = pytest.mark.skip(reason=_SKIP_REASON)
    for item in items:
        # `get_closest_marker`, not `"live" in item.keywords`: only a real
        # `@pytest.mark.live` / module `pytestmark` counts. `keywords`
        # also contains parametrize ids and node-name fragments, so a
        # test merely *named* for the word `live` (this package's own
        # guard tests) must not be swept up.
        if item.get_closest_marker("live") is not None:
            item.add_marker(skip_live)
