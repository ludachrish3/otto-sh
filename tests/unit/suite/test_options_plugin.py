"""suite_options: CLI instance when provided, per-class defaults otherwise."""

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# Imported for its SIDE EFFECT on sys.modules, and the ensure_* tests below do
# not work without it. `pytester.runpytest_inprocess` snapshots sys.modules at
# fixture setup and restores it afterwards, which EVICTS every module first
# imported during the test. `otto.project` is one of those (nothing else in
# this file's outer session imports it), and eviction does not clear the
# `project` attribute on the already-imported `otto` package — so the next
# `monkeypatch.setattr("otto.project.ensure_installed", ...)` resolves through
# that stale attribute and patches a dead module object, while the fixture's
# own `from ..project import ...` re-imports a fresh one and calls the REAL
# converge function. Importing it here puts it in the snapshot, so there is
# only ever one module object. Symptom when this regresses: exactly one
# ensure_* test — whichever runs next in the same worker — fails with
# `calls == []`.
import otto.project  # noqa: F401 — see above
from otto.config.lab import Lab
from otto.context import OttoContext, reset_context, set_context
from otto.errors import EnsureStateError
from otto.params import OptionsSource
from otto.result import CommandNotRunError, Result
from otto.suite.plugin import OttoPlugin
from otto.suite.run import ASYNCIO_LOOP_ARGS
from otto.utils import Status

pytest_plugins = ["pytester"]

SUITE_SRC = """\
from typing import Annotated
import typer
from otto import options
from otto.suite import OttoSuite

@options
class _Defaulted:
    retries: Annotated[int, typer.Option(help="n")] = 3

class TestDefaulted(OttoSuite):
    Options = _Defaulted
    def test_gets_defaults(self, suite_options):
        assert suite_options.retries == 3

@options
class _Required:
    firmware: Annotated[str, typer.Option(help="fw")]

class TestRequired(OttoSuite):
    Options = _Required
    def test_never_runs(self, suite_options):
        raise AssertionError("should have failed at fixture setup")
"""


# pytester's runpytest_inprocess spins up a *nested* pytest session inside
# this one. It runs with otto test's own loop-scope args (ASYNCIO_LOOP_ARGS)
# so the loop-identity asserts in ENSURE_SUITE_SRC measure the real contract.
# `-p no:playwright`: pytest-playwright's session-wide soft-assertion hook
# wraps every test call and rejects re-entry ("nested soft assertion scopes
# are not supported"), so it must be disabled for in-process nested sessions —
# same fix as test_otto_suite.py / test_plugin.py / the integration
# passthrough test. These inner runs use no Playwright fixtures.
INNER_ARGS = (
    "-p",
    "no:cacheprovider",
    "-p",
    "no:playwright",
    *ASYNCIO_LOOP_ARGS,
)


@pytest.fixture(autouse=True)
def _otto_context(tmp_path: Path):
    """The suite_dir fixture reads get_context().output_dir — install a stub
    context for the duration of the inner pytest session, mirroring the
    `_run_inner_pytest` helper in tests/unit/suite/test_otto_suite.py.
    """
    ctx = OttoContext(lab=Lab(name="_test_stub"), output_dir=tmp_path)
    token = set_context(ctx)
    try:
        yield
    finally:
        reset_context(token)


# SUITES registry isolation (needed because each pytester run below registers
# TestDefaulted/TestRequired from a fresh temp path) is provided package-wide
# by the autouse ``_isolate_suites`` fixture in ``tests/unit/suite/conftest.py``.


def test_defaulted_options_are_constructed(pytester: pytest.Pytester) -> None:
    from otto.suite.pytest_plugin import OttoOptionsPlugin

    pytester.makepyfile(test_inner=SUITE_SRC)
    result = pytester.runpytest_inprocess(
        "-k",
        "TestDefaulted",
        *INNER_ARGS,
        plugins=[OttoPlugin(), OttoOptionsPlugin(None)],
    )
    assert result.ret == pytest.ExitCode.OK
    # End-to-end guard on OttoPlugin.pytest_report_teststatus: a one-test
    # suite must be counted ONCE. The override used to return the "passed"
    # category for the setup and teardown phase reports too, so this same
    # run reported `3 passed`.
    result.assert_outcomes(passed=1)


def test_required_options_fail_with_suite_hint(pytester: pytest.Pytester) -> None:
    from otto.suite.pytest_plugin import OttoOptionsPlugin

    pytester.makepyfile(test_inner=SUITE_SRC)
    result = pytester.runpytest_inprocess(
        "-k",
        "TestRequired",
        *INNER_ARGS,
        plugins=[OttoPlugin(), OttoOptionsPlugin(None)],
    )
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*required options*otto test TestRequired*"])


def test_explicit_instance_still_wins(pytester: pytest.Pytester) -> None:
    from otto.suite.pytest_plugin import OttoOptionsPlugin

    class _Sentinel:
        retries = 99

    pytester.makepyfile(test_inner=SUITE_SRC.replace("== 3", "== 99"))
    result = pytester.runpytest_inprocess(
        "-k",
        "TestDefaulted",
        *INNER_ARGS,
        plugins=[OttoPlugin(), OttoOptionsPlugin(_Sentinel())],
    )
    assert result.ret == pytest.ExitCode.OK


# ── the ensure marker (spec §4) ──────────────────────────────────────────────
#
# The fixture body is driven DIRECTLY, the way `test_ctx_fixture_returns_
# active_context` (tests/unit/suite/test_register.py) and `_FixtureRunner`
# (tests/unit/suite/test_plugin.py) drive theirs: the decorator stashes the
# original function on ``__wrapped__``, so calling that skips pytest's fixture
# machinery. The pytester tests further down are the other half — they run a
# real inner session, which is the only thing that can prove the marker is
# read from the right node, validated at collection, and REGISTERED.

ENSURE_STEPS = ("installed", "uninstalled", "clean")
CONVERGE_FUNCTIONS = ("ensure_installed", "ensure_uninstalled", "ensure_clean")


def _stub(calls: list[tuple], name: str, outcome: Any) -> Callable[..., Any]:
    """One converge stand-in: records the CALL's source and loop, then returns/raises *outcome*.

    The recorded entry is ``(name, source)`` — the one positional argument the
    marker's converge receives, not swallowed by an unexamined ``*args``. What
    must not silently change is the SOURCE, not the mere presence of an
    argument: the marker builds an ``OptionsSource`` over the suite's options
    and passes it as the converge's sole positional argument, so a stub that
    recorded only the name would let the plugin quietly pass the wrong
    source — an instance source pointed at the wrong object, or one built
    from ``None`` when a real suite instance was available — with no
    assertion able to see it.
    """

    async def _fake(source: Any = None) -> Any:
        from otto.suite.pytest_plugin import OttoOptionsPlugin

        calls.append((name, source))
        OttoOptionsPlugin._converge_loop = asyncio.get_running_loop()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return _fake


def _called(*function_names: str, suite_options: Any = None) -> list[tuple]:
    """The expected ``calls`` record: each converge function, in order, with the SAME source.

    ``suite_options`` is the raw value the ``suite_options`` fixture resolved
    to (or ``None``); the expectation is built the same way the plugin builds
    it — ``OptionsSource.from_instance`` — so a test only has to name the
    instance, not reconstruct the source by hand.
    """
    return [(name, OptionsSource.from_instance(suite_options)) for name in function_names]


def _stub_ensures(monkeypatch: pytest.MonkeyPatch, calls: list[tuple], outcome: Any) -> list[tuple]:
    """Replace ALL THREE converge functions on ``otto.project``, each recording its own name.

    All three, not just the one under test: that is what lets ``calls`` catch a
    step wired to the WRONG converge function. Patched on ``otto.project`` — the
    package object the plugin reads the name off at call time; a plugin that
    reached into ``otto.project.orchestrator`` would see through this stub, and
    should. Also arms the ``_converge_loop`` slot the stubs write to (deleted
    again on teardown via ``raising=False``).
    """
    from otto.suite.pytest_plugin import OttoOptionsPlugin

    monkeypatch.setattr(OttoOptionsPlugin, "_converge_loop", None, raising=False)
    for name in CONVERGE_FUNCTIONS:
        monkeypatch.setattr(f"otto.project.{name}", _stub(calls, name, outcome))
    return calls


def _marker_request(
    args: tuple | None,
    suite_options: Any = None,
    *,
    lookup_error: bool = False,
    fail_error: bool = False,
) -> MagicMock:
    """A FixtureRequest double whose node carries an ``ensure`` marker with *args* (or none).

    ``getfixturevalue("suite_options")`` is wired explicitly, not left as an
    auto-mock attribute — an unwired ``MagicMock`` would return another
    ``MagicMock``, which happens to satisfy ``isinstance`` checks and hides
    both real branches of the caller's handling. By default it returns
    *suite_options*; ``lookup_error=True`` makes it raise
    ``pytest.FixtureLookupError`` (the shape of a non-suite, ``ensure``-marked
    test with no ``suite_options`` fixture in scope); ``fail_error=True``
    makes it raise ``pytest.fail.Exception`` (the shape of a suite whose
    ``Options`` class has required fields — see ``OttoOptionsPlugin.
    suite_options``'s own ``pytest.fail`` call).
    """
    request = MagicMock()
    request.node.get_closest_marker.return_value = (
        None if args is None else MagicMock(args=args, kwargs={})
    )

    def _getfixturevalue(name: str) -> Any:
        assert name == "suite_options"
        if lookup_error:
            raise pytest.FixtureLookupError(name, request)
        if fail_error:
            pytest.fail("suite has required options", pytrace=False)
        return suite_options

    request.getfixturevalue.side_effect = _getfixturevalue
    return request


def _run_ensure(
    args: tuple | None,
    suite_options: Any = None,
    *,
    lookup_error: bool = False,
    fail_error: bool = False,
) -> Any:
    """Drive ``_otto_ensure``'s body on a throwaway loop for a marker with *args*."""
    from otto.suite.pytest_plugin import OttoOptionsPlugin

    plugin = OttoOptionsPlugin(None)
    request = _marker_request(args, suite_options, lookup_error=lookup_error, fail_error=fail_error)
    return asyncio.run(OttoOptionsPlugin._otto_ensure.__wrapped__(plugin, request))


_OUTCOMES = (EnsureStateError, CommandNotRunError, pytest.skip.Exception)


def _drive_and_catch(args: tuple | None) -> BaseException | None:
    """Run the fixture; return whatever it raised (``None`` if it returned).

    The outcomes this file has to tell apart are ``EnsureStateError`` (a
    convergence failure), ``CommandNotRunError`` (a dry-run refusal), and
    ``pytest.skip()``'s ``Skipped`` — rooted at ``BaseException``, which is the
    outcome a wrong implementation would produce (e.g. relabelling a failure
    as a skip). Catching exactly these three, rather than ``BaseException``
    broadly, means anything else propagates and errors the test — the louder,
    correct behaviour for an outcome this file was not built to expect.
    """
    try:
        _run_ensure(args)
    except _OUTCOMES as exc:
        return exc
    return None


@pytest.mark.parametrize(
    ("step", "function"), list(zip(ENSURE_STEPS, CONVERGE_FUNCTIONS, strict=True))
)
def test_each_step_awaits_its_own_converge_function_once_with_the_suite_options_source(
    step: str, function: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub_ensures(monkeypatch, [], Result(Status.Success, msg="converged"))
    _run_ensure((step,))
    assert calls == _called(function)


def test_the_suite_options_fixture_value_becomes_the_converge_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``suite_options`` resolves, the converge receives ``OptionsSource(instance=<that
    value>)`` — the real instance, not a stand-in ``MagicMock`` an unwired double would hand it.
    """
    calls = _stub_ensures(monkeypatch, [], Result(Status.Success, msg="converged"))
    marker = object()
    _run_ensure(("installed",), marker)
    assert calls == _called("ensure_installed", suite_options=marker)


def test_a_missing_suite_options_fixture_converges_with_no_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-suite ``ensure``-marked test has no ``suite_options`` fixture in scope — that is a
    ``pytest.FixtureLookupError``, not an error condition — and the converge still runs, sourced
    from ``OptionsSource(instance=None)``: defaults, not a crash.
    """
    calls = _stub_ensures(monkeypatch, [], Result(Status.Success, msg="converged"))
    _run_ensure(("installed",), lookup_error=True)
    assert calls == _called("ensure_installed", suite_options=None)


def test_a_failed_suite_options_build_is_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A suite whose ``Options`` class has REQUIRED fields fails ``suite_options`` at setup with
    ``pytest.fail`` (see ``OttoOptionsPlugin.suite_options``), not ``FixtureLookupError``.
    ``_otto_ensure`` must not catch this: converging from defaults instead would silently install
    the wrong thing under a selection run that never instantiates the suite. The failure must
    propagate unchanged, and the converge must never run.
    """
    calls = _stub_ensures(monkeypatch, [], Result(Status.Success, msg="converged"))
    with pytest.raises(pytest.fail.Exception):
        _run_ensure(("installed",), fail_error=True)
    assert calls == []


def test_a_path_runs_its_steps_in_the_written_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec §4.1: ``ensure("clean", "installed")`` cleans, THEN installs — a fresh install."""
    calls = _stub_ensures(monkeypatch, [], Result(Status.Success, msg="converged"))
    _run_ensure(("clean", "installed"))
    assert calls == _called("ensure_clean", "ensure_installed")


@pytest.mark.parametrize("args", [None, ("none",)])
def test_no_marker_and_none_converge_nothing(args, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_ensures(monkeypatch, [], Result(Status.Success, msg="converged"))
    _run_ensure(args)
    assert calls == []


@pytest.mark.parametrize("step", ENSURE_STEPS)
def test_a_failed_converge_errors_not_skips_and_names_the_step_and_host(
    step: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """House rule: a host that cannot converge FAILS the test with its name.

    The raised object must be an ``EnsureStateError`` — a ``Skipped`` fails the
    isinstance — and the message carries THIS step's name, so a copy-paste that
    labels a clean failure "installed" is caught.
    """
    _stub_ensures(monkeypatch, [], Result(Status.Failed, msg="host test1: unreachable"))
    raised = _drive_and_catch((step,))
    assert isinstance(raised, EnsureStateError), (
        f"convergence failure must ERROR the test, never skip it; got {raised!r}"
    )
    assert str(raised) == f"ensure {step} failed: host test1: unreachable"


@pytest.mark.parametrize("step", ENSURE_STEPS)
def test_a_skipped_no_op_converge_is_ok(step: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """``Status.Skipped`` ("already installed") has ``is_ok`` True — the common case."""
    calls = _stub_ensures(monkeypatch, [], Result(Status.Skipped, msg="already installed"))
    assert _drive_and_catch((step,)) is None
    assert len(calls) == 1


@pytest.mark.parametrize("step", ENSURE_STEPS)
def test_a_dry_run_refusal_propagates_unchanged(step: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Under ``--dry-run`` converge paths RAISE ``CommandNotRunError``; it must not be
    relabelled as a failure to converge (kills a ``try/except Exception`` wrapper)."""
    _stub_ensures(monkeypatch, [], CommandNotRunError("rpm -q otto-agent", "test1"))
    raised = _drive_and_catch((step,))
    assert isinstance(raised, CommandNotRunError), (
        f"a dry-run refusal must reach the test unchanged; got {raised!r}"
    )


def test_a_failed_first_step_stops_the_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """``("clean", "installed")`` with a failing clean never reaches installed."""
    calls = _stub_ensures(monkeypatch, [], Result(Status.Failed, msg="host test1: unreachable"))
    raised = _drive_and_catch(("clean", "installed"))
    assert isinstance(raised, EnsureStateError)
    assert calls == _called("ensure_clean")


# ── real inner sessions: marker resolution, validation, registration ─────────

MARKER_SUITE_SRC = """\
import asyncio

import pytest

from otto.suite import OttoSuite
from otto.suite.pytest_plugin import OttoOptionsPlugin

pytestmark = pytest.mark.ensure("installed")


@pytest.mark.ensure("clean")
class TestMarked(OttoSuite):
    async def test_takes_the_class_path(self):
        # The converge has to run on the loop the TEST runs on: it opens host
        # connections, and a connection bound to another loop is unusable here.
        assert OttoOptionsPlugin._converge_loop is asyncio.get_running_loop()

    @pytest.mark.ensure("none")
    async def test_opts_out(self):
        pass


async def test_plain_function_takes_the_module_path():
    assert OttoOptionsPlugin._converge_loop is asyncio.get_running_loop()
"""


def test_closest_marker_replaces_the_whole_path(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §4.1: module says installed, class says clean, one test says none.

    Per-test converge calls: the class test → [clean]; the opted-out test → [];
    the plain function → [installed] (the module marker). Red if the plugin
    MERGES paths (the class test would also see installed) or reads the wrong
    node. Order-independent: the inner session inherits pytest-randomly.
    """
    from otto.suite.pytest_plugin import OttoOptionsPlugin

    calls = _stub_ensures(monkeypatch, [], Result(Status.Success, msg="converged"))
    pytester.makepyfile(test_inner=MARKER_SUITE_SRC)
    result = pytester.runpytest_inprocess(
        *INNER_ARGS, plugins=[OttoPlugin(), OttoOptionsPlugin(None)]
    )
    assert result.ret == pytest.ExitCode.OK
    result.assert_outcomes(passed=3)
    assert sorted(name for name, _ in calls) == ["ensure_clean", "ensure_installed"]


@pytest.mark.parametrize(
    ("marker", "fragment"),
    [
        ('@pytest.mark.ensure("bogus")', "unknown step 'bogus'"),
        ('@pytest.mark.ensure("none", "installed")', "'none' is a complete path"),
        ("@pytest.mark.ensure()", "at least one step"),
    ],
)
def test_an_invalid_path_errors_the_run_at_collection(
    marker: str, fragment: str, pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §4.2: the run stops before any test executes, naming node, verb, vocabulary."""
    from otto.suite.pytest_plugin import OttoOptionsPlugin

    calls = _stub_ensures(monkeypatch, [], Result(Status.Success, msg="converged"))
    pytester.makepyfile(
        test_inner=f"""\
import pytest
from otto.suite import OttoSuite

class TestBad(OttoSuite):
    {marker}
    async def test_never_runs(self):
        raise AssertionError("collection should have refused this")
"""
    )
    result = pytester.runpytest_inprocess(
        *INNER_ARGS, plugins=[OttoPlugin(), OttoOptionsPlugin(None)]
    )
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines([f"*test_inner.py::TestBad::test_never_runs*{fragment}*"])
    assert calls == []


def test_the_old_fixture_names_are_gone(pytester: pytest.Pytester) -> None:
    """Spec §4.2/§10: requesting ``ensure_installed`` by name is a loud fixture-not-found."""
    from otto.suite.pytest_plugin import OttoOptionsPlugin

    pytester.makepyfile(
        test_inner="""\
from otto.suite import OttoSuite

class TestOld(OttoSuite):
    async def test_requests_it(self, ensure_installed):
        pass
"""
    )
    result = pytester.runpytest_inprocess(
        *INNER_ARGS, plugins=[OttoPlugin(), OttoOptionsPlugin(None)]
    )
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*fixture 'ensure_installed' not found*"])


def test_the_marker_is_registered_for_strict_markers(
    pytester: pytest.Pytester, monkeypatch
) -> None:
    """``--strict-markers`` accepts ``ensure`` — it is registered, not merely tolerated."""
    from otto.suite.pytest_plugin import OttoOptionsPlugin

    _stub_ensures(monkeypatch, [], Result(Status.Success, msg="converged"))
    pytester.makepyfile(
        test_inner="""\
import pytest
from otto.suite import OttoSuite

@pytest.mark.ensure("installed")
class TestStrict(OttoSuite):
    async def test_ok(self):
        pass
"""
    )
    result = pytester.runpytest_inprocess(
        "--strict-markers", *INNER_ARGS, plugins=[OttoPlugin(), OttoOptionsPlugin(None)]
    )
    assert result.ret == pytest.ExitCode.OK
