"""Tripwire on timeout *wiring* — not a test of pytest-timeout itself.

This does not validate the third-party library; it asserts that *our* suite
still aborts a hung test. That contract silently broke once: otto used to
enforce ``@pytest.mark.timeout(N)`` with an in-house autouse ``pytest_asyncio``
fixture doing ``loop.call_later(N, current_task().cancel)``, which did nothing
— the autouse fixture runs in a *different* asyncio task than the test body, so
it cancelled its own task and never the test. A test marked ``timeout(2)``
that slept 30s passed after a full 30s, and nothing caught it. Enforcement is
now ``pytest-timeout`` (configured in ``pyproject.toml``).

This fails loudly if the wiring regresses for any reason that isn't the
library's fault — pytest-timeout dropped from deps, a conflicting timeout
fixture reintroduced, ``timeout_method``/``addopts`` overridden, or a
pytest/pytest-asyncio/xdist bump that changes when the signal fires:

- timeout fires (correct): the body never returns, the test "fails" with a
  Timeout, and ``xfail(strict=True)`` records it as XFAIL (a pass).
- timeout does NOT fire (regression): the sleep completes, the body returns,
  the test passes, and strict xfail turns that into a hard XPASS failure.

Sub-second timeout keeps the cost negligible even under ``--count`` stress
repetition; a sleeping body always exceeds it, so it never flakes.
"""

import time

import pytest


@pytest.mark.xfail(reason="pytest-timeout must abort this hung test", strict=True)
@pytest.mark.timeout(0.25)
def test_marked_timeout_actually_aborts():
    time.sleep(30)
