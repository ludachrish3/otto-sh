"""Shared session guard for browser-marked suites.

Both browser suites (monitor dashboard, coverage report) need the same
pytest_configure-time question answered: "could a browser-marked item
survive this session's -m filter?" — evaluated from config alone, before
collection exists, with pytest's own expression engine. See the dashboard
conftest for the full design rationale (xdist constraints, historic-hook
semantics); it moved here unchanged when the coverage-report suite arrived.
"""

import pytest
from _pytest.mark.expression import Expression

BROWSER_TEST_MARKERS = frozenset({"browser", "hostless", "xdist_group"})


def browser_tests_could_run(config: pytest.Config) -> bool:
    """Would a browser-marked item survive this session's ``-m`` filter?"""
    markexpr = config.option.markexpr
    if not markexpr:
        return True

    def matches(name: str, **_kwargs: object) -> bool:
        return name in BROWSER_TEST_MARKERS

    return Expression.compile(markexpr).evaluate(matches)
