"""Two root-conftest guards, exercised through a real inner pytest session.

``tests/conftest.py``'s ``_reset_otto_context`` puts the OttoContext state back
after every test: the two ContextVars (``_active``, ``_variant``). Its
``pytest_runtest_teardown`` wrapper runs the lazy-export leak check
(``tests/_fixtures/_lazy_exports.py``) after every fixture has finalized, and
evicts a leak even when the teardown failed first (an inner teardown raised,
or the loop reaper refused a leaked running loop), so the leak cannot cascade
onto the next test.

``tests/unit/test_lazy_packages.py`` tests the check's functions directly;
what this module pins is the WIRING. Each test runs an inner session with the
repo's root conftest registered as a plugin
(``tests/_fixtures/root_conftest_pytester.py``): one probe test leaves the
state behind, and the next probe test asserts it was put back. Deleting the
restore line, the check call, or the eviction on the failure path, or moving
the loop reaper out of the guarded block, turns this red.
"""

import re

import pytest

from tests._fixtures.root_conftest_pytester import INNER_ARGS, root_conftest_session

pytest_plugins = ["pytester"]

# `-p no:randomly`: each probe module is a sequence of leakers, each followed
# by its checker. `-rE` ends the report with the summary the section parser
# stops at.
PROBE_ARGS = [*INNER_ARGS, "-p", "no:randomly", "-rE"]

PROBE_CONTEXT = """\
from otto import context

_SENTINEL = object()
_BEFORE = (context._active.get(), context._variant.get())


def test_leaves_the_context_state_behind():
    context._active.set(_SENTINEL)
    context._variant.set("field" if _BEFORE[1] != "field" else "debug")


def test_starts_from_the_state_the_leaker_found():
    after = (context._active.get(), context._variant.get())
    names = ["_active", "_variant"]
    leaked = [n for n, b, a in zip(names, _BEFORE, after) if b is not a]
    assert not leaked, f"not restored: {leaked}"
"""

PROBE_LAZY_EXPORTS = """\
import pytest

import otto.session


def _leaked():
    return "build_lab" in vars(otto.session)


def test_a_leaks_a_lazy_export(monkeypatch):
    # Patching the PACKAGE leaves the real object cached there on undo.
    monkeypatch.setattr("otto.session.build_lab", lambda: None)


def test_b_finds_the_leak_evicted():
    assert not _leaked()


@pytest.fixture
def _fails_on_teardown():
    yield
    raise RuntimeError("inner teardown failed")


# monkeypatch is set up first, so it is undone (creating the leak) AFTER the
# failing fixture's teardown has raised.
def test_c_leaks_while_another_teardown_raises(monkeypatch, _fails_on_teardown):
    monkeypatch.setattr("otto.session.build_lab", lambda: None)


def test_d_finds_the_leak_evicted_after_a_failing_teardown():
    assert not _leaked()


_PARKED = []


# The running-loop leak of tests/unit/test_running_loop_leak_guard.py: a
# greenlet parked inside run_until_complete. The reaper refuses it at teardown,
# before the lazy-export check would have run.
def test_e_leaks_while_a_running_loop_is_refused(monkeypatch):
    import asyncio

    import greenlet

    monkeypatch.setattr("otto.session.build_lab", lambda: None)
    loop = asyncio.new_event_loop()
    caller = greenlet.getcurrent()

    async def _park():
        caller.switch()

    parked = greenlet.greenlet(lambda: loop.run_until_complete(_park()))
    _PARKED.append((parked, loop))
    parked.switch()
    assert asyncio._get_running_loop() is loop


def test_f_finds_the_leak_evicted_after_a_refused_running_loop():
    try:
        assert not _leaked()
    finally:
        # Resume the parked coroutine so run_until_complete returns and
        # unregisters the loop: the reaper would refuse it again here.
        parked, loop = _PARKED.pop()
        parked.switch()
        loop.close()
"""


@pytest.fixture
def inner(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> pytest.Pytester:
    """A pytester rootdir carrying the root conftest and none of the outer run's settings."""
    return root_conftest_session(pytester, monkeypatch)


# The raised exceptions as a section's ``E`` line spells them. The bare class
# names are not enough: a traceback through the teardown hook quotes its
# docstring, which names both.
_RAISED_LAZY_LEAK = "tests._fixtures._lazy_exports.LeakedLazyExportError: "
_RAISED_LOOP_LEAK = "tests._fixtures._loop_reaper.LeakedRunningLoopError: "


def _outcomes(result: pytest.RunResult) -> dict[str, int]:
    """The inner run's outcome counts, bar warnings, which no probe is about."""
    return {k: v for k, v in result.parseoutcomes().items() if k != "warnings"}


def test_reset_otto_context_restores_both_vars(inner: pytest.Pytester) -> None:
    (inner.path / "test_probe_context.py").write_text(PROBE_CONTEXT)
    result = inner.runpytest_subprocess(*PROBE_ARGS, timeout=180)
    combined = str(result.stdout) + str(result.stderr)
    assert _outcomes(result) == {"passed": 2}, combined


def test_teardown_hook_runs_the_lazy_export_check_and_evicts_on_failure(
    inner: pytest.Pytester,
) -> None:
    (inner.path / "test_probe_lazy_exports.py").write_text(PROBE_LAZY_EXPORTS)
    result = inner.runpytest_subprocess(*PROBE_ARGS, timeout=180)
    combined = str(result.stdout) + str(result.stderr)

    # Every probe body passes; the three leakers error at teardown, each with
    # its own first failure: the leak check for a, the inner teardown for c,
    # the loop reaper for e. The checkers b, d and f passing is the eviction.
    assert _outcomes(result) == {"passed": 6, "errors": 3}, combined
    errors = _teardown_error_sections(str(result.stdout))
    assert sorted(errors) == [
        "test_a_leaks_a_lazy_export",
        "test_c_leaks_while_another_teardown_raises",
        "test_e_leaks_while_a_running_loop_is_refused",
    ], combined
    leak_report = errors["test_a_leaks_a_lazy_export"]
    assert _RAISED_LAZY_LEAK in leak_report, combined
    assert "otto.session.build_lab -> patch otto.session.lab.build_lab" in leak_report, combined
    first_failure = errors["test_c_leaks_while_another_teardown_raises"]
    assert "RuntimeError: inner teardown failed" in first_failure, combined
    assert _RAISED_LAZY_LEAK not in first_failure, combined
    refused_loop = errors["test_e_leaks_while_a_running_loop_is_refused"]
    assert _RAISED_LOOP_LEAK in refused_loop, combined
    assert _RAISED_LAZY_LEAK not in refused_loop, combined


# A section header: a test's, or an ``ERROR at teardown of`` one. Its rule is
# as many underscores as fit the width, down to one, so the count is not
# matched; the ``_ _ _`` separator inside a traceback is excluded by its
# second character instead, so a section keeps its last frame.
_SECTION = re.compile(r"^_+ (?:ERROR at teardown of (\w+)|[^_ ].*?) _+$", re.MULTILINE)


def _teardown_error_sections(stdout: str) -> dict[str, str]:
    """Each ``ERROR at teardown of <test>`` section of a pytest report, by test name."""
    headers = list(_SECTION.finditer(stdout))
    # No header at all means the report's shape changed under the parser; say
    # so with the report, rather than as a zip() length mismatch.
    assert headers, stdout
    # The last section ends at the summary; a missing summary is the same
    # shape change, so it fails here rather than as a -1 slice end.
    end_of_last = stdout.find("short test summary info")
    assert end_of_last != -1, stdout
    ends = [h.start() for h in headers[1:]] + [end_of_last]
    return {
        header.group(1): stdout[header.end() : end]
        for header, end in zip(headers, ends, strict=True)
        if header.group(1)
    }
