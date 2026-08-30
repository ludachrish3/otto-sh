"""suite_options: CLI instance when provided, per-class defaults otherwise."""

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

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
from otto.result import CommandNotRunError, Result
from otto.suite.plugin import OttoPlugin
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

class TestDefaulted(OttoSuite[_Defaulted]):
    Options = _Defaulted
    def test_gets_defaults(self, suite_options):
        assert suite_options.retries == 3

@options
class _Required:
    firmware: Annotated[str, typer.Option(help="fw")]

class TestRequired(OttoSuite[_Required]):
    Options = _Required
    def test_never_runs(self, suite_options):
        raise AssertionError("should have failed at fixture setup")
"""


# pytester's runpytest_inprocess spins up a *nested* pytest session inside
# this one. The outer suite's `filterwarnings = ["error"]` (pyproject.toml)
# turns pytest-asyncio's "asyncio_default_fixture_loop_scope is unset"
# deprecation warning into a fatal INTERNALERROR during inner-session
# configure. tests/unit/suite/test_plugin.py hits the same trap driving
# pytest.main() directly and works around it with this same `-o` override;
# mirrored here for the pytester-based inner runs.
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
    "-o",
    "asyncio_default_fixture_loop_scope=function",
)


@pytest.fixture(autouse=True)
def _otto_context(tmp_path: Path):
    """OttoSuite.setup_class reads get_context().output_dir — install a stub
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


# ── ensure_* converging fixtures ─────────────────────────────────────────────
#
# The fixture bodies are driven DIRECTLY, the way `test_ctx_fixture_returns_
# active_context` (tests/unit/suite/test_register.py) and `_FixtureRunner`
# (tests/unit/suite/test_plugin.py) drive theirs: the decorator stashes the
# original function on ``__wrapped__``, so calling that skips pytest's fixture
# machinery. `test_ensure_fixtures_are_available_to_a_suite` below is the other
# half — it runs a real inner session, which is the only thing that can prove
# these are actually REGISTERED as fixtures at the right scope.

ENSURE_FIXTURES = ("ensure_installed", "ensure_uninstalled", "ensure_clean")

ENSURE_SUITE_SRC = """\
import asyncio

from otto.suite import OttoSuite
from otto.suite.pytest_plugin import OttoOptionsPlugin

class TestEnsures(OttoSuite):
    async def test_requests_the_fixture(self, {fixture}):
        assert {fixture} is None
        # The converge has to run on the loop the TEST runs on: it opens host
        # connections, and a connection bound to a loop that is no longer
        # running is unusable here. `_converge_loop` is stashed by the stub
        # (_stub below); this is what makes the fixture's declared loop scope
        # load-bearing rather than decorative — `loop_scope="class"` puts the
        # converge on a third loop and fails right here.
        assert OttoOptionsPlugin._converge_loop is asyncio.get_running_loop()

async def test_plain_function_requests_the_fixture({fixture}):
    # The other shape `otto test` runs: a module-level async test, no class.
    # A class-scoped loop has nothing to attach to here.
    assert {fixture} is None
    assert OttoOptionsPlugin._converge_loop is asyncio.get_running_loop()
"""


def _stub(calls: list[tuple], name: str, outcome: Any) -> Callable[..., Any]:
    """One converge stand-in: records the CALL and its loop, then returns/raises *outcome*.

    The recorded entry is ``(name, args, kwargs)`` — the arguments included,
    not swallowed by an unexamined ``*args``. The fixtures call their converge
    function with NOTHING, taking the orchestrator's own defaults, and that is
    the documented behaviour of all three (``ensure_installed`` "a PARTIAL lab
    is uninstalled first, then installed fresh" IS ``recover_partial=True``).
    A stub that recorded only the name would let a fixture quietly pass
    ``recover_partial=False`` — which inverts that sentence, installs straight
    over known remnants, and reproduces the PARTIAL state the converge was
    requested to fix.
    """

    async def _fake(*args: Any, **kwargs: Any) -> Any:
        from otto.suite.pytest_plugin import OttoOptionsPlugin

        calls.append((name, args, kwargs))
        OttoOptionsPlugin._converge_loop = asyncio.get_running_loop()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return _fake


def _called(fixture_name: str, times: int = 1) -> list[tuple]:
    """The expected ``calls`` record: *fixture_name*'s converge, with no arguments."""
    return [(fixture_name, (), {})] * times


def _stub_ensures(monkeypatch: pytest.MonkeyPatch, calls: list[tuple], outcome: Any) -> list[tuple]:
    """Replace ALL THREE converge functions, each recording its own name.

    All three, not just the one under test: that is what makes ``calls ==
    [fixture_name]`` able to catch a fixture wired to the WRONG converge
    function. Patching only one would leave the wrong call hitting the real
    orchestrator, which fails for unrelated reasons (no lab in the ambient
    context) and reads as a confusing error rather than a clean red.

    Patched on ``otto.project`` — the package object the fixture bodies read
    the name off at call time. A fixture that reached into
    ``otto.project.orchestrator`` instead would see through this stub, and
    should: the re-export is the seam the rest of otto calls.

    Also arms the ``_converge_loop`` slot the stubs write the running loop to
    — through ``monkeypatch`` with ``raising=False``, so the attribute is
    deleted again on teardown and the plugin class leaves the test as it
    arrived.
    """
    from otto.suite.pytest_plugin import OttoOptionsPlugin

    monkeypatch.setattr(OttoOptionsPlugin, "_converge_loop", None, raising=False)
    for name in ENSURE_FIXTURES:
        monkeypatch.setattr(f"otto.project.{name}", _stub(calls, name, outcome))
    return calls


def _run_fixture(fixture_name: str) -> Any:
    """Call the plugin's async fixture body on a throwaway loop."""
    from otto.suite.pytest_plugin import OttoOptionsPlugin

    plugin = OttoOptionsPlugin(None)
    return asyncio.run(getattr(plugin, fixture_name).__wrapped__(plugin))


def _drive_and_catch(fixture_name: str) -> BaseException | None:
    """Run the fixture; return whatever it raised (``None`` if it returned).

    ``BaseException``, not ``Exception``, and that is the whole point: the
    outcome this file has to be able to distinguish from an error is
    ``pytest.skip()``, whose ``Skipped`` is rooted at ``BaseException``. A
    ``pytest.raises(Exception)`` would let a skipping fixture straight through
    and the case would report SKIPPED — which is not red. Returning the object
    lets the caller assert on its TYPE.
    """
    try:
        _run_fixture(fixture_name)
    except BaseException as exc:  # noqa: BLE001 — the caught type IS the assertion; see docstring
        return exc
    return None


@pytest.mark.parametrize("fixture_name", ENSURE_FIXTURES)
def test_ensure_fixture_awaits_its_converge_function(
    fixture_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each fixture awaits ITS OWN converge function, exactly once, with no arguments.

    Kills a fixture that requests the wrong converge function, one that returns
    the coroutine without awaiting it (silently a no-op), and one that passes
    the converge layer a flag of its own — ``recover_partial=False`` above all,
    which is a real behaviour change (install over known remnants) that no
    assertion on the NAME alone can see.
    """
    calls = _stub_ensures(monkeypatch, [], Result(Status.Success, msg="converged"))
    _run_fixture(fixture_name)
    assert calls == _called(fixture_name)


@pytest.mark.parametrize("fixture_name", ENSURE_FIXTURES)
def test_ensure_fixture_failure_errors_not_skips(
    fixture_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """House rule: a host that cannot converge FAILS the test with its name.

    Kills ``pytest.skip`` on failure — the raised object must be an
    ``EnsureStateError``, so a ``Skipped`` fails the isinstance assertion
    instead of quietly marking the case skipped. Also kills a fixture that
    ignores ``is_ok`` and returns anyway (``raised is None``).

    THE VERB LABEL IS PART OF THE MESSAGE, and pinned per fixture. The three
    fixtures pass their own name to one shared ``_raise_unless_converged``, so
    the label is a hand-written string argument at each call site: the
    copy-paste that gives ``ensure_clean`` the message ``ensure_installed
    failed: …`` is a one-word slip that leaves the reader debugging the wrong
    converge entirely, and every other assertion here is blind to it.
    """
    _stub_ensures(monkeypatch, [], Result(Status.Failed, msg="host test1: unreachable"))
    raised = _drive_and_catch(fixture_name)
    assert isinstance(raised, EnsureStateError), (
        f"convergence failure must ERROR the test, never skip it; got {raised!r}"
    )
    assert str(raised) == f"{fixture_name} failed: host test1: unreachable", (
        "the message must name THIS fixture's verb and the failing host"
    )


@pytest.mark.parametrize("fixture_name", ENSURE_FIXTURES)
def test_ensure_fixture_accepts_a_skipped_no_op(
    fixture_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A converge that had nothing to do returns ``Status.Skipped``, and that is ok.

    The no-op arms of ``otto.project``'s ensure layer ("already installed",
    "already clean") report ``Skipped``, whose ``is_ok`` is True. Kills a
    fixture that gates on ``status is Status.Success`` and so errors every
    test in an already-converged lab — the common case.
    """
    calls = _stub_ensures(monkeypatch, [], Result(Status.Skipped, msg="already installed"))
    assert _drive_and_catch(fixture_name) is None
    assert calls == _called(fixture_name)


@pytest.mark.parametrize("fixture_name", ENSURE_FIXTURES)
def test_ensure_fixture_propagates_a_dry_run_refusal(
    fixture_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dry run's ``CommandNotRunError`` propagates unchanged, not reframed.

    Under ``--dry-run`` several converge paths RAISE rather than return: the
    state was never established, so there is nothing to report about it. A
    fixture that caught it — even to re-raise as ``EnsureStateError`` — would
    relabel "otto declined to issue the command" as "the lab failed to
    converge". Kills a ``try/except Exception`` wrapper around the await.
    """
    _stub_ensures(monkeypatch, [], CommandNotRunError("rpm -q otto-agent", "test1"))
    raised = _drive_and_catch(fixture_name)
    assert isinstance(raised, CommandNotRunError), (
        f"a dry-run refusal must reach the test unchanged; got {raised!r}"
    )


@pytest.mark.parametrize("fixture_name", ENSURE_FIXTURES)
def test_ensure_fixtures_are_available_to_a_suite(
    fixture_name: str, pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both shapes ``otto test`` runs can REQUEST each of the three.

    The direct-call tests above exercise the fixture BODIES; only a real inner
    session proves the registration works — right name, resolvable from an
    OttoSuite method AND from a plain module-level async test, and awaited on
    the same event loop the requesting test runs on (the inner asserts).

    That last one is not a formality: with ``loop_scope="class"`` — the shape
    this task was briefed with — pytest-asyncio hands the fixture a loop that
    NO test runs on, and the converge's host connections would be stranded on
    it. This test fails in that configuration.

    PARAMETRIZED OVER ALL THREE, because registration and loop scope are
    per-fixture declarations: they are three separate decorators, so one of
    them carrying the wrong scope (or not being a ``pytest_asyncio`` fixture at
    all, which makes it resolve to an un-awaited coroutine) is invisible to a
    test that only ever requests ``ensure_installed``. The direct-call tests
    are parametrized precisely because the three bodies drift independently;
    the declarations above them drift the same way.
    """
    from otto.suite.pytest_plugin import OttoOptionsPlugin

    calls = _stub_ensures(monkeypatch, [], Result(Status.Success, msg="converged"))
    pytester.makepyfile(test_inner=ENSURE_SUITE_SRC.format(fixture=fixture_name))
    result = pytester.runpytest_inprocess(
        "-k",
        "requests_the_fixture",
        *INNER_ARGS,
        plugins=[OttoPlugin(), OttoOptionsPlugin(None)],
    )
    assert result.ret == pytest.ExitCode.OK
    assert calls == _called(fixture_name, times=2)  # the class test and the plain one
