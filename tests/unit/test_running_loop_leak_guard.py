"""The running-loop leak guard must fail the test that LEFT the loop running.

Issue #381: asyncio's running-loop slot is thread-local, so a test that returns
with ``asyncio._get_running_loop()`` still set poisons every later test on that
worker which drives a loop of its own (``RuntimeError: Cannot run the event
loop while another loop is running``) — attributed to the victim, on unchanged
code. ``tests/conftest.py``'s boundary hook raises
:class:`~tests._fixtures._loop_reaper.LeakedRunningLoopError` instead, naming
the leaker.

The pure decision lives in ``running_loop_leak_reason`` and is unit-tested in
``test_loop_reaper.py``; what this module pins is the WIRING — that the hook is
still installed and still reaches that decision. So it runs a real inner pytest
session (the repo's root conftest registered as a plugin, exactly as an xdist
worker gets it) over a test that parks a ``run_until_complete`` in a greenlet —
Playwright's mechanism, reproduced without Playwright. Deleting the hook from
``tests/conftest.py`` turns this red.
"""

from pathlib import Path

import pytest

from tests._fixtures.paths import PROJECT_ROOT

pytest_plugins = ["pytester"]

# An inert rootdir config: the inner session's behaviour must be the probe's
# and the root conftest's, never this repo's addopts.
PROBE_INI = """\
[pytest]
"""

# The root conftest is registered as a PLUGIN rather than copied: this test is
# worthless unless it exercises the real hook.
PROBE_CONFTEST = """\
import tests.conftest as otto_root_conftest


def pytest_configure(config):
    config.pluginmanager.register(otto_root_conftest, name="otto-root-conftest")
"""

# The leak, without Playwright: a greenlet parked inside run_until_complete
# leaves the loop registered as running on this thread, because the greenlet
# switch does not unwind ``run_forever``'s finally. The greenlet is kept alive
# on purpose — dropping the last reference throws GreenletExit into it, which
# unwinds the loop and un-leaks it.
PROBE_TEST = """\
import asyncio

import greenlet

_PARKED = []


def test_parks_a_running_loop_in_a_greenlet():
    loop = asyncio.new_event_loop()
    caller = greenlet.getcurrent()

    async def _park():
        caller.switch()

    parked = greenlet.greenlet(lambda: loop.run_until_complete(_park()))
    _PARKED.append(parked)
    parked.switch()
    assert asyncio._get_running_loop() is loop
"""

# `-p no:tach`: tach's pytest11 plugin panics on repeated in-tree sessions
# (issue #193), the same guard the repo addopts use.
INNER_ARGS = ("-p", "no:tach", "-p", "no:cacheprovider")


def test_a_test_that_leaves_a_running_loop_fails_by_its_own_name(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    for var in (
        "COV_CORE_SOURCE",
        "COV_CORE_CONFIG",
        "COV_CORE_DATAFILE",
        "COV_CORE_CONTEXT",
        "PYTEST_XDIST_WORKER",
        "PYTEST_XDIST_WORKER_COUNT",
        "PYTEST_XDIST_TESTRUNUID",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PYTHONPATH", str(PROJECT_ROOT))

    pytester.makeini(PROBE_INI)
    root: Path = pytester.path
    (root / "conftest.py").write_text(PROBE_CONFTEST)
    (root / "test_probe_leaks_a_running_loop.py").write_text(PROBE_TEST)

    result = pytester.runpytest_subprocess(*INNER_ARGS, timeout=180)
    combined = str(result.stdout) + str(result.stderr)

    # The test body itself passes — the leak is only decidable at the boundary,
    # which is the whole point: teardown errors it.
    result.assert_outcomes(passed=1, errors=1)
    assert "LeakedRunningLoopError" in combined, combined
    assert "test_parks_a_running_loop_in_a_greenlet" in combined, combined
    assert "registered as RUNNING" in combined, combined
