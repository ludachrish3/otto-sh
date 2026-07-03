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

# Every browser-marked test in either suite carries exactly these markers
# (mirror of the ``pytestmark`` list atop each browser test module). The
# synthetic item below must carry all of them, not just ``browser``: an ``-m``
# expression can select on any one — e.g. a positive ``-m hostless`` picks
# these tests up too — and keying only off ``browser`` would wrongly evaluate
# such an expression to "deselected" and stay silent, letting the browser
# tests run straight into N missing-dist fixture errors (the exact noise this
# guard exists to replace).
BROWSER_TEST_MARKERS = frozenset({"browser", "hostless", "xdist_group"})


def browser_tests_could_run(config: pytest.Config) -> bool:
    """Would a browser-marked item survive this session's ``-m`` filter?

    ``pytest_configure`` fires before collection, so there's no item list to
    consult yet — evaluate the compiled ``-m`` expression (the same
    ``_pytest.mark.expression.Expression`` pytest itself uses for
    ``-m``/``-k``) directly against a synthetic item carrying exactly the
    markers every test that needs the real build also carries
    (``BROWSER_TEST_MARKERS``). An empty expression means nothing is
    filtered, so browser tests trivially survive.
    """
    markexpr = config.option.markexpr
    if not markexpr:
        return True

    def matches(name: str, **_kwargs: object) -> bool:
        return name in BROWSER_TEST_MARKERS

    return Expression.compile(markexpr).evaluate(matches)
