"""The root conftest's guards, each exercised through a real inner pytest session.

``tests/conftest.py``'s ``_reset_otto_context`` puts the OttoContext state back
after every test: the three ContextVars (the context's ``_active``, and the
``otto.invocation`` leaf's ``_POLICY`` and ``_RESOLVER``), and
``_reset_otto_context_per_module`` puts them back after every module, which
undoes what a module-scoped fixture installed. The root conftest's
``pytest_runtest_teardown`` wrapper runs the lazy-export leak check
(``tests/_fixtures/_lazy_exports.py``) after every fixture has finalized, and
evicts a leak even when the teardown failed first (an inner teardown raised,
or the loop reaper refused a leaked running loop), so the leak cannot cascade
onto the next test. The same wrapper fails a test that leaves hosts registered
on an open event loop no cleanup boundary holds (checked before the reaper
closes the loop, and a live wider-scoped runner loop is not exempt), and
``pytest_sessionfinish`` fails a session whose class, module, package or
session runner loop closed with hosts still registered, even when a later
registration has since forgotten that loop's registry.

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
from otto import context, invocation

_SENTINEL = object()
_BEFORE = (context._active.get(), invocation._POLICY.get(), invocation._RESOLVER.get())


def test_leaves_the_context_state_behind():
    context._active.set(_SENTINEL)
    invocation._POLICY.set(invocation.RunPolicy(variant="field"))
    invocation._RESOLVER.set(_SENTINEL)


def test_starts_from_the_state_the_leaker_found():
    after = (context._active.get(), invocation._POLICY.get(), invocation._RESOLVER.get())
    names = ["_active", "_POLICY", "_RESOLVER"]
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


PROBE_ORPHAN = """\
import asyncio

import pytest

from otto import invocation
from tests._fixtures.registry import register_duck

_KEPT = []


class _Double:
    id = "left-behind"

    async def close(self):
        return None


def test_a_leaves_a_host_on_a_loop_it_keeps_open():
    loop = asyncio.new_event_loop()
    _KEPT.append(loop)
    register_duck(_Double(), loop)


@pytest.mark.asyncio
async def test_b_registers_on_its_own_loop_which_its_fixture_closes():
    register_duck(_Double(), asyncio.get_running_loop())


def test_c_starts_clean():
    invocation.forget_closed_loops()
    assert invocation.open_registrations() == []
"""

PROBE_HELD = """\
# A module loop whose host a boundary holds: no error.
import asyncio

import pytest
import pytest_asyncio

from otto import invocation
from tests._fixtures.registry import register_duck

pytestmark = pytest.mark.asyncio(loop_scope="module")


class _Double:
    id = "held-host"

    async def close(self):
        return None


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def held_host():
    loop = asyncio.get_running_loop()
    boundary = invocation.acquire_boundary(loop, deadline=5)
    register_duck(_Double(), loop)
    yield
    await boundary.release(label="the probe module")


async def test_held_a(held_host):
    pass


async def test_held_b(held_host):
    pass
"""

PROBE_UNHELD = """\
# The same host with no boundary: an orphan at the first teardown.
import asyncio

import pytest
import pytest_asyncio

from tests._fixtures.registry import register_duck

pytestmark = pytest.mark.asyncio(loop_scope="module")


class _Double:
    id = "unheld-host"

    async def close(self):
        return None


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def unheld_host():
    register_duck(_Double(), asyncio.get_running_loop())
    yield


async def test_unheld_a(unheld_host):
    pass


async def test_unheld_b(unheld_host):
    pass
"""

PROBE_SESSION_END = """\
# A session loop that closes with a held host still registered: the session fails.
import asyncio

import pytest
import pytest_asyncio

from otto import invocation
from tests._fixtures.registry import register_duck

pytestmark = pytest.mark.asyncio(loop_scope="session")


class _Double:
    id = "left-at-session-end"

    async def close(self):
        return None


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def never_released():
    loop = asyncio.get_running_loop()
    invocation.acquire_boundary(loop, deadline=5)  # held, and never released
    register_duck(_Double(), loop)
    yield


async def test_one(never_released):
    pass


async def test_two(never_released):
    pass
"""

PROBE_M1_LEAKS = """\
# A module loop that closes with a held host still registered.
import asyncio

import pytest
import pytest_asyncio

from otto import invocation
from tests._fixtures.registry import register_duck

pytestmark = pytest.mark.asyncio(loop_scope="module")


class _Double:
    id = "module-leak"

    async def close(self):
        return None


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def never_released():
    loop = asyncio.get_running_loop()
    invocation.acquire_boundary(loop, deadline=5)  # held, and never released
    register_duck(_Double(), loop)
    yield


async def test_leaks(never_released):
    pass
"""

PROBE_M2_REGISTERS = """\
# A later registration, which prunes every closed loop's registry.
import asyncio

from otto import invocation
from tests._fixtures.registry import register_duck


class _Double:
    id = "later"

    async def close(self):
        return None


def test_registers_and_unregisters():
    loop = asyncio.new_event_loop()
    try:
        double = _Double()
        register_duck(double, loop)
        invocation.unregister(loop, id(double), 0)
    finally:
        loop.close()
"""

PROBE_LEAK_THEN_SESSION_FINALIZER = """\
# A module loop closes with a held host registered, then a session finalizer registers
# in the same teardown, before the guard runs: its pruning must not erase the leak.
import asyncio

import pytest
import pytest_asyncio

from otto import invocation
from tests._fixtures.registry import register_duck

pytestmark = pytest.mark.asyncio(loop_scope="module")


class _Leak:
    id = "module-leak"

    async def close(self):
        return None


class _Late:
    id = "late"

    async def close(self):
        return None


@pytest.fixture(scope="session")
def registers_at_session_end():
    yield
    loop = asyncio.new_event_loop()
    try:
        double = _Late()
        register_duck(double, loop)
        invocation.unregister(loop, id(double), 0)
    finally:
        loop.close()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def never_released():
    loop = asyncio.get_running_loop()
    invocation.acquire_boundary(loop, deadline=5)  # held, and never released
    register_duck(_Leak(), loop)
    yield


# Requested first, so it is finalized last: after the module runner closed its loop.
async def test_leaks(registers_at_session_end, never_released):
    pass
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


PROBE_MODULE_INSTALL = """\
# A module fixture installs a context and undoes only the context's own variable,
# leaving the policy and resolver set_context also installed.
import pytest

from otto import context, invocation
from otto.config.lab import Lab
from otto.context import OttoContext, set_context


@pytest.fixture(autouse=True, scope="module")
def _load_lab():
    snapshot = context._active.get()
    set_context(OttoContext(lab=Lab(name="module-lab")))
    yield
    context._active.set(snapshot)


def test_runs_under_the_module_lab():
    assert invocation.installed_resolver() is not None
"""

PROBE_AFTER_THE_MODULE = """\
from otto import context, invocation


def test_starts_with_nothing_installed():
    state = (context._active.get(), invocation._POLICY.get(), invocation._RESOLVER.get())
    assert state == (None, None, None)
"""


def test_a_module_fixtures_install_ends_with_its_module(inner: pytest.Pytester) -> None:
    # The per-test restore cannot see a module fixture's install: it snapshots the
    # state with that install already in place. The module-scoped restore can.
    (inner.path / "test_probe_a_module_install.py").write_text(PROBE_MODULE_INSTALL)
    (inner.path / "test_probe_b_after_the_module.py").write_text(PROBE_AFTER_THE_MODULE)
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


_RAISED_REGISTRATION_LEAK = "tests._fixtures._loop_reaper.LeakedRegistrationError: "
_OUTLIVED = "registrations outlived the session"


def test_teardown_hook_raises_on_hosts_left_on_an_open_loop(inner: pytest.Pytester) -> None:
    (inner.path / "test_probe_orphan.py").write_text(PROBE_ORPHAN)
    result = inner.runpytest_subprocess(*PROBE_ARGS, timeout=180)
    combined = str(result.stdout) + str(result.stderr)

    # a leaves a host on a loop it keeps open; the guard runs before the reaper
    # closes that loop, names it, and drops the registry, so c starts clean. b's
    # loop closes with its fixture, so its host is no orphan.
    assert _outcomes(result) == {"passed": 3, "errors": 1}, combined
    errors = _teardown_error_sections(str(result.stdout))
    assert sorted(errors) == ["test_a_leaves_a_host_on_a_loop_it_keeps_open"], combined
    report = errors["test_a_leaves_a_host_on_a_loop_it_keeps_open"]
    assert _RAISED_REGISTRATION_LEAK in report, combined
    assert "left-behind" in report, combined


def test_only_a_held_boundary_exempts_a_wider_runner_loop(inner: pytest.Pytester) -> None:
    (inner.path / "test_probe_held.py").write_text(PROBE_HELD)
    (inner.path / "test_probe_unheld.py").write_text(PROBE_UNHELD)
    result = inner.runpytest_subprocess(*PROBE_ARGS, timeout=180)
    combined = str(result.stdout) + str(result.stderr)

    # A live module runner is inspected like any other loop: the held module
    # passes clean, and the unheld one errors once, at its first teardown.
    assert _outcomes(result) == {"passed": 4, "errors": 1}, combined
    errors = _teardown_error_sections(str(result.stdout))
    assert sorted(errors) == ["test_unheld_a"], combined
    assert _RAISED_REGISTRATION_LEAK in errors["test_unheld_a"], combined
    assert "unheld-host" in errors["test_unheld_a"], combined


# Both legs: single-process, and under xdist (the repo default), where the
# worker's own exit status and terminal never reach the user, so the worker
# hands its leftovers to the controller.
@pytest.mark.parametrize("workers", [[], ["-n", "1"]], ids=["single-process", "xdist"])
def test_a_session_loop_closed_with_hosts_fails_the_session(
    inner: pytest.Pytester, workers: list[str]
) -> None:
    (inner.path / "test_probe_session_end.py").write_text(PROBE_SESSION_END)
    result = inner.runpytest_subprocess(*PROBE_ARGS, *workers, timeout=180)
    combined = str(result.stdout) + str(result.stderr)

    assert _outcomes(result) == {"passed": 2}, combined
    assert result.ret == 1, combined
    assert _OUTLIVED in combined, combined
    assert "left-at-session-end" in combined, combined


def test_a_later_registration_does_not_erase_a_closed_module_loops_leak(
    inner: pytest.Pytester,
) -> None:
    (inner.path / "test_probe_m1_leaks.py").write_text(PROBE_M1_LEAKS)
    (inner.path / "test_probe_m2_registers.py").write_text(PROBE_M2_REGISTERS)
    result = inner.runpytest_subprocess(*PROBE_ARGS, timeout=180)
    combined = str(result.stdout) + str(result.stderr)

    assert _outcomes(result) == {"passed": 2}, combined
    assert result.ret == 1, combined
    assert _OUTLIVED in combined, combined
    assert "module-leak" in combined, combined


def test_a_session_finalizer_registering_in_the_same_teardown_does_not_erase_the_leak(
    inner: pytest.Pytester,
) -> None:
    (inner.path / "test_probe_session_finalizer.py").write_text(PROBE_LEAK_THEN_SESSION_FINALIZER)
    result = inner.runpytest_subprocess(*PROBE_ARGS, timeout=180)
    combined = str(result.stdout) + str(result.stderr)

    assert _outcomes(result) == {"passed": 1}, combined
    assert result.ret == 1, combined
    assert _OUTLIVED in combined, combined
    assert "module-leak" in combined, combined


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
