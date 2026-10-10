"""OttoFixturesPlugin: the ``ctx`` fixture, the ``ensure`` marker, and plain tests under it."""

import asyncio
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from typing_extensions import Self

from otto.config.lab import Lab
from otto.context import OttoContext, reset_context, set_context
from otto.errors import EnsureStateError
from otto.invocation import RunPolicy
from otto.params import OptionsSource
from otto.result import CommandNotRunError, Result
from otto.suite.layout import ArtifactLayout
from otto.suite.plugin import OttoPlugin
from otto.suite.run import ASYNCIO_LOOP_ARGS
from otto.utils import Status
from tests._fixtures.paths import PROJECT_ROOT

# None of the tests in this file request module_dir/test_dir — the ensure
# marker and the ctx fixture are what is under test here — so every
# OttoFixturesPlugin() below is built with this placeholder layout, never
# resolved against the filesystem. An absolute path under the real system
# temp dir (not a bare relative "unused"), so a stray resolution wouldn't
# silently write under the repo's own cwd.
_LAYOUT = ArtifactLayout(root=Path(tempfile.gettempdir()) / "otto-fixtures-plugin-unused-layout")
_SRC = PROJECT_ROOT / "src"


class _Runner:
    """The slice of a pytest-asyncio runner ``_otto_ensure`` uses: ``run(coro)`` on one loop."""

    def __enter__(self) -> Self:
        self._loop = asyncio.new_event_loop()
        return self

    def __exit__(self, *exc: object) -> None:
        self._loop.close()

    def run(self, coro: Any, *, context: Any = None) -> Any:
        del context  # the fixture passes a copy of its own context; one loop is all this needs
        return self._loop.run_until_complete(coro)


pytest_plugins = ["pytester"]

# pytester's runpytest_inprocess spins up a *nested* pytest session inside
# this one. It runs with otto test's own loop-scope args (ASYNCIO_LOOP_ARGS)
# so the loop-identity asserts in ENSURE_SUITE_SRC measure the real contract.
# `-p no:playwright`: pytest-playwright's session-wide soft-assertion hook
# wraps every test call and rejects re-entry ("nested soft assertion scopes
# are not supported"), so it must be disabled for in-process nested sessions —
# same fix as test_plugin.py. These inner runs use no Playwright fixtures.
INNER_ARGS = (
    "-p",
    "no:cacheprovider",
    "-p",
    "no:playwright",
    *ASYNCIO_LOOP_ARGS,
)


@pytest.fixture(autouse=True)
def _otto_context(tmp_path: Path):
    """The ``ctx`` fixture reads get_context() — install a stub
    context for the duration of the inner pytest session, as ``otto test``
    has one installed around its own.
    """
    ctx = OttoContext(lab=Lab(name="_test_stub"), policy=RunPolicy(output_dir=tmp_path))
    token = set_context(ctx)
    try:
        yield
    finally:
        reset_context(token)


def test_a_one_test_file_is_counted_once(pytester: pytest.Pytester) -> None:
    """End-to-end guard on ``OttoPlugin.pytest_report_teststatus``.

    The override used to return the "passed" category for the setup and
    teardown phase reports too, so a one-test run reported ``3 passed``.
    """
    from otto.suite.pytest_plugin import OttoFixturesPlugin

    pytester.makepyfile(test_inner="def test_one():\n    pass\n")
    result = pytester.runpytest_inprocess(
        *INNER_ARGS, plugins=[OttoPlugin(), OttoFixturesPlugin(layout=_LAYOUT)]
    )
    assert result.ret == pytest.ExitCode.OK
    result.assert_outcomes(passed=1)


# ── the ctx fixture ──────────────────────────────────────────────────────────


def test_the_ctx_fixture_returns_the_active_context() -> None:
    """The fixture body, driven directly through ``__wrapped__``, returns the active context."""
    from otto.suite.pytest_plugin import OttoFixturesPlugin

    plugin = OttoFixturesPlugin(layout=_LAYOUT)
    ctx = OttoContext(lab=Lab(name="test"))
    token = set_context(ctx)
    try:
        assert OttoFixturesPlugin._ctx.__wrapped__(plugin) is ctx
    finally:
        reset_context(token)


def test_a_class_scoped_fixture_may_request_ctx(pytester: pytest.Pytester) -> None:
    """``ctx`` is session-scoped, so a class-wide fixture can take it.

    Red at function scope (``ScopeMismatch`` at setup). The inner test reads the
    lab name of the stub context this file installs.
    """
    from otto.suite.pytest_plugin import OttoFixturesPlugin

    pytester.makepyfile(
        test_inner="""\
import pytest

class TestCtxScope:
    @pytest.fixture(scope="class", autouse=True)
    @classmethod
    def uses_ctx(cls, ctx):
        cls.lab_name = ctx.lab.name

    async def test_ran(self) -> None:
        assert self.lab_name == "_test_stub"
"""
    )
    result = pytester.runpytest_inprocess(
        *INNER_ARGS, plugins=[OttoPlugin(), OttoFixturesPlugin(layout=_LAYOUT)]
    )
    result.assert_outcomes(passed=1)


def test_a_test_modules_logging_propagates_to_a_root_handler(pytester: pytest.Pytester) -> None:
    """A test module's plain ``logging.getLogger(__name__)`` reaches root, with no plugin support.

    The handler stands in for otto's root sinks; the plugin does not touch
    logging at all. A characterisation pin of the surface a deleted
    collection hook used to serve, not a regression guard against its return.
    """
    import logging

    from otto.suite.pytest_plugin import OttoFixturesPlugin

    class _Sink(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.messages: list[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.messages.append(record.getMessage())

    pytester.makepyfile(
        test_logcap="""\
import logging

logger = logging.getLogger(__name__)


class TestLogCap:
    async def test_a(self) -> None:
        logger.info("from the test module")
"""
    )
    sink = _Sink()
    root = logging.getLogger()
    root.addHandler(sink)
    old_level = root.level
    root.setLevel(logging.INFO)
    try:
        result = pytester.runpytest_inprocess(
            *INNER_ARGS, plugins=[OttoPlugin(), OttoFixturesPlugin(layout=_LAYOUT)]
        )
    finally:
        root.removeHandler(sink)
        root.setLevel(old_level)
    result.assert_outcomes(passed=1)
    assert "from the test module" in sink.messages


# ── the ensure marker (spec §4) ──────────────────────────────────────────────
#
# The fixture body is driven DIRECTLY, the way `test_ctx_fixture_returns_
# active_context` (above) and `_FixtureRunner`
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
    argument: the marker hands the converge the ``test`` verb's parsed flags as
    bound on the context, so a stub that recorded only the name would let the
    plugin quietly pass the wrong source — empty kwargs when flags were bound,
    say — with no assertion able to see it.
    """

    async def _fake(source: Any = None) -> Any:
        from otto.suite.pytest_plugin import OttoFixturesPlugin

        calls.append((name, source))
        OttoFixturesPlugin._converge_loop = asyncio.get_running_loop()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return _fake


def _called(*function_names: str, flags: dict[str, Any] | None = None) -> list[tuple]:
    """The expected ``calls`` record: each converge function, in order, with the SAME source.

    *flags* are the ``test`` verb's parsed flags bound on the context (none by
    default); the expectation is built the way the context builds it —
    ``OptionsSource.from_kwargs`` — so a test only has to name the flags.
    """
    return [(name, OptionsSource.from_kwargs(flags or {})) for name in function_names]


def _stub_ensures(monkeypatch: pytest.MonkeyPatch, calls: list[tuple], outcome: Any) -> list[tuple]:
    """Replace ALL THREE converge functions, each recording its own name.

    All three, not just the one under test: that is what lets ``calls`` catch a
    step wired to the WRONG converge function. Each stub sits on the module
    that defines the name, ``otto.project.orchestrator``, so the plugin sees it
    whether it reads the name through the package's lazy ``__getattr__`` or
    straight from the orchestrator. Patching ``otto.project`` instead would
    leave the real function cached in the package ``__dict__`` when the patch
    is undone; the root conftest's lazy-export guard refuses that. Also arms
    the ``_converge_loop`` slot the stubs write to (deleted again on teardown
    via ``raising=False``).
    """
    from otto.suite.pytest_plugin import OttoFixturesPlugin

    monkeypatch.setattr(OttoFixturesPlugin, "_converge_loop", None, raising=False)
    # One otto.project across pytester runs: tests/_fixtures/_pytester_snapshot.py.
    for name in CONVERGE_FUNCTIONS:
        monkeypatch.setattr(f"otto.project.orchestrator.{name}", _stub(calls, name, outcome))
    return calls


def _marker_request(args: tuple | None, *, runner: Any = None) -> MagicMock:
    """A FixtureRequest double whose node carries an ``ensure`` marker with *args* (or none).

    ``getfixturevalue`` is wired explicitly, not left as an auto-mock
    attribute: a request for any pytest-asyncio runner fixture returns
    *runner*, which is where the fixture drives each converge step, and a
    request for anything else fails the test — the converge's options come
    from the context, never from another fixture.
    """
    request = MagicMock()
    request.node.get_closest_marker.return_value = (
        None if args is None else MagicMock(args=args, kwargs={})
    )

    def _getfixturevalue(name: str) -> Any:
        assert name.endswith("_scoped_runner"), f"the ensure fixture requested {name!r}"
        return runner

    request.getfixturevalue.side_effect = _getfixturevalue
    return request


def _run_ensure(args: tuple | None, *, flags: dict[str, Any] | None = None) -> Any:
    """Run ``_otto_ensure``'s body for a marker with *args*, its steps on a throwaway runner.

    Under a fresh context; with *flags*, the ``test`` verb's options are bound
    on it first, as ``run_tests`` binds them before any test runs.
    """
    from otto.suite.pytest_plugin import OttoFixturesPlugin

    ctx = OttoContext(lab=Lab(name="ensure"))
    if flags is not None:
        ctx.bind_verb_options("test", flags)
    token = set_context(ctx)
    plugin = OttoFixturesPlugin(layout=_LAYOUT)
    try:
        with _Runner() as runner:
            request = _marker_request(args, runner=runner)
            return OttoFixturesPlugin._otto_ensure.__wrapped__(plugin, request)
    finally:
        reset_context(token)


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
def test_each_step_awaits_its_own_converge_function_once_with_the_verb_source(
    step: str, function: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub_ensures(monkeypatch, [], Result(Status.Success, msg="converged"))
    _run_ensure((step,))
    assert calls == _called(function)


def test_the_bound_test_verb_flags_become_the_converge_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The converge receives ``OptionsSource(kwargs=<the test verb's flags>)``, read off the
    context the run bound them on — what ``otto run install --ensure`` hands it for the same
    flags.
    """
    from otto import options
    from otto.params import register_options

    @options
    class LabEnv:
        lab_env: str = "staging"

    register_options(LabEnv, verbs=["test"])
    calls = _stub_ensures(monkeypatch, [], Result(Status.Success, msg="converged"))
    _run_ensure(("installed",), flags={"lab_env": "prod"})
    assert calls == _called("ensure_installed", flags={"lab_env": "prod"})


def test_nothing_bound_converges_from_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """A context no verb was dispatched on still converges: the source is empty flags, so every
    install body takes its defaults — not a crash.
    """
    calls = _stub_ensures(monkeypatch, [], Result(Status.Success, msg="converged"))
    _run_ensure(("installed",))
    assert calls == _called("ensure_installed", flags={})


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

from otto.suite.pytest_plugin import OttoFixturesPlugin

pytestmark = pytest.mark.ensure("installed")


@pytest.mark.ensure("clean")
class TestMarked:
    async def test_takes_the_class_path(self):
        # The converge has to run on the loop the TEST runs on: it opens host
        # connections, and a connection bound to another loop is unusable here.
        assert OttoFixturesPlugin._converge_loop is asyncio.get_running_loop()

    @pytest.mark.ensure("none")
    async def test_opts_out(self):
        pass


async def test_plain_function_takes_the_module_path():
    assert OttoFixturesPlugin._converge_loop is asyncio.get_running_loop()
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
    from otto.suite.pytest_plugin import OttoFixturesPlugin

    calls = _stub_ensures(monkeypatch, [], Result(Status.Success, msg="converged"))
    pytester.makepyfile(test_inner=MARKER_SUITE_SRC)
    result = pytester.runpytest_inprocess(
        *INNER_ARGS, plugins=[OttoPlugin(), OttoFixturesPlugin(layout=_LAYOUT)]
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
    from otto.suite.pytest_plugin import OttoFixturesPlugin

    calls = _stub_ensures(monkeypatch, [], Result(Status.Success, msg="converged"))
    pytester.makepyfile(
        test_inner=f"""\
import pytest

class TestBad:
    {marker}
    async def test_never_runs(self):
        raise AssertionError("collection should have refused this")
"""
    )
    result = pytester.runpytest_inprocess(
        *INNER_ARGS, plugins=[OttoPlugin(), OttoFixturesPlugin(layout=_LAYOUT)]
    )
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines([f"*test_inner.py::TestBad::test_never_runs*{fragment}*"])
    assert calls == []


def test_the_old_fixture_names_are_gone(pytester: pytest.Pytester) -> None:
    """Spec §4.2/§10: requesting ``ensure_installed`` by name is a loud fixture-not-found."""
    from otto.suite.pytest_plugin import OttoFixturesPlugin

    pytester.makepyfile(
        test_inner="""\
class TestOld:
    async def test_requests_it(self, ensure_installed):
        pass
"""
    )
    result = pytester.runpytest_inprocess(
        *INNER_ARGS, plugins=[OttoPlugin(), OttoFixturesPlugin(layout=_LAYOUT)]
    )
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*fixture 'ensure_installed' not found*"])


def test_the_marker_is_registered_for_strict_markers(
    pytester: pytest.Pytester, monkeypatch
) -> None:
    """``--strict-markers`` accepts ``ensure`` — it is registered, not merely tolerated."""
    from otto.suite.pytest_plugin import OttoFixturesPlugin

    _stub_ensures(monkeypatch, [], Result(Status.Success, msg="converged"))
    pytester.makepyfile(
        test_inner="""\
import pytest

@pytest.mark.ensure("installed")
class TestStrict:
    async def test_ok(self):
        pass
"""
    )
    result = pytester.runpytest_inprocess(
        "--strict-markers",
        *INNER_ARGS,
        plugins=[OttoPlugin(), OttoFixturesPlugin(layout=_LAYOUT)],
    )
    assert result.ret == pytest.ExitCode.OK


def test_the_dump_records_the_fixtures_plugin_without_a_refusal() -> None:
    """A pytest fixture definition is an unclassifiable member (API dump spec §3.4).

    The plugin is declared at ``otto.suite``, so a fixture defined on a public
    method name would refuse the whole dump. Each fixture is defined on a
    private method and published under its fixture name instead.
    """
    from scripts import api_regen

    report = api_regen.run_child(Path(sys.executable), _SRC, ["otto.suite"])
    assert [r for r in report["refusals"] if r.startswith("otto.suite:OttoFixturesPlugin")] == []


def test_the_five_fixtures_keep_their_names_and_scopes(pytester: pytest.Pytester) -> None:
    """Moving the definitions to private methods must not rename a fixture a test requests."""
    from otto.suite.pytest_plugin import OttoFixturesPlugin

    pytester.makepyfile(test_inner="def test_one():\n    pass\n")
    result = pytester.runpytest_inprocess(
        "--fixtures", *INNER_ARGS, plugins=[OttoPlugin(), OttoFixturesPlugin(layout=_LAYOUT)]
    )
    result.stdout.fnmatch_lines_random(
        [
            "ctx [[]session scope[]] -- *",
            "module_dir [[]module scope[]] -- *",
            "test_dir -- *",
            "expect -- *",
            "monitor -- *",
        ]
    )
