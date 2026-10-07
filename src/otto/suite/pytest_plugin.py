"""The pytest plugin that hands otto's fixtures to every test.

Imported only when a pytest session runs (:mod:`otto.suite.run`), so importing
``otto.suite`` never pulls in pytest.
"""

import contextvars
import logging
from collections.abc import Generator, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from ..errors import EnsureStateError
from .expect import ExpectCollector
from .layout import ArtifactLayout
from .loops import runner_for
from .markers import ENSURE_VERBS, OTTO_MARKERS, ensure_path, ensure_path_problem

if TYPE_CHECKING:
    # Typing only: the converge functions are resolved inside `_converge`
    # (see there), so nothing here needs otto.project at module scope — and
    # `Result` is only ever read, never constructed.
    from ..params import OptionsSource
    from ..result import Result
    from .monitor_fixture import MonitorHandle

_logger = logging.getLogger(__name__)

otto_expect_key: pytest.StashKey[ExpectCollector] = pytest.StashKey()
"""Where the ``expect`` fixture parks a test's collector for the call-phase wrapper."""

otto_iteration_key: pytest.StashKey[int] = pytest.StashKey()
"""The 1-based stability iteration a test's call phase is currently in.

Set by :meth:`~otto.suite.plugin.OttoPlugin.pytest_runtest_protocol` and only
in stability mode (``--iterations`` / ``--duration``); its ABSENCE is what
tells ``test_dir`` a run is not repeating, so a plain run keeps the flat
``<suite>/<node>`` path it has always had.
"""

otto_test_dir_base_key: pytest.StashKey[Path] = pytest.StashKey()
"""The un-suffixed directory the ``test_dir`` fixture resolved from the layout.

Parked so the repeat loop can re-point ``test_dir`` at the next iteration
without a second copy of the sanitize-and-join rule. Present only once a test
has actually REQUESTED ``test_dir`` — that is what keeps the loop from creating
directories for a test that never asked for one.
"""

otto_layout_key: pytest.StashKey[ArtifactLayout] = pytest.StashKey()
"""The run's :class:`~otto.suite.layout.ArtifactLayout`, parked by ``pytest_configure``.

Built once per pytest session (see :mod:`otto.suite.run`) and handed to
``OttoFixturesPlugin`` at construction; the ``module_dir``/``test_dir``
fixtures read it from here rather than from the plugin instance so they stay
plain ``staticmethod``/instance fixtures with no other coupling to it.
"""

otto_fixtures_plugin_key: pytest.StashKey["OttoFixturesPlugin"] = pytest.StashKey()
"""Where ``pytest_configure`` parks the plugin instance, for code holding only a ``Config``.

pytest 9.1 deprecates a class-scoped fixture that is a bound instance method —
of a plugin object as much as of a test class (the check is only "is
``__self__`` a type") — and pytest 10 makes it an error. A wider-scoped fixture
here is therefore a staticmethod, and one that needs plugin state finds the
plugin through ``request.config`` under this key.
"""


def iteration_dir(base: Path, iteration: int | None) -> Path:
    """Where one stability *iteration*'s artifacts go under a test's *base* dir.

    ``None`` — the run is not repeating — is the base itself, unchanged. The
    numbering is 1-based to match the ``--- <test> iteration 1 ---`` banner the
    repeat loop logs, so a reader pairs a directory with a log line by eye.

    Args:
        base: The test's un-suffixed directory, from
            :meth:`~otto.suite.layout.ArtifactLayout.test_dir`.
        iteration: The 1-based iteration number, or ``None`` outside stability mode.

    Returns:
        The directory the current iteration should write into. Not created here.
    """
    return base if iteration is None else base / f"iteration_{iteration}"


def _raise_unless_converged(result: "Result", step: str) -> None:
    """Turn a non-ok converge *result* into an error naming the failing host.

    ``is_ok``, not ``status is Status.Success``, and the difference is the
    common case: a converge with nothing to do reports ``Status.Skipped``
    ("already installed"), which is a pass. Only ``Failed`` / ``Error`` /
    ``NotRun`` reach the raise.
    """
    if not result.is_ok:
        raise EnsureStateError(f"ensure {step} failed: {result.msg}")


async def _converge(step: str, source: "OptionsSource") -> None:
    """Run one ``ensure`` step through the same ``otto.project`` function.

    This is the function ``otto run <name> --ensure`` calls, so a marker and
    the command cannot diverge. *source* is the dispatched verb's parsed
    flags (:meth:`otto.context.OttoContext.verb_option_source`), which each
    repo's install body builds its own options class from by field name, as
    ``otto run`` does: a flag registered only for ``run`` takes its default
    here. The function is looked up on the package at call time (not
    imported at module scope): ``otto.project`` is the seam every other
    caller uses, and resolving late is what lets a test double stand in for
    it.
    """
    from .. import project

    converge = getattr(project, ENSURE_VERBS[step])
    _raise_unless_converged(await converge(source), step)


class OttoFixturesPlugin:
    """Pytest plugin that gives every test, class or plain function, otto's fixtures.

    It provides the ``ctx``, ``module_dir``, ``test_dir``, ``expect`` and
    ``monitor`` fixtures, plus the ``ensure`` hook (the autouse converge that
    honors ``@pytest.mark.ensure``) and the ``expect`` hook (the call-phase
    wrapper that fails a test whose soft checks recorded failures)::

        async def test_something(ctx, test_dir, expect) -> None:
            opts = ctx.options(DeviceOptions)
            expect(opts.device_type == "router")

    Each fixture is defined on a private method (like ``_otto_ensure``) and
    published under its fixture name (``@pytest.fixture(name=...)``): a
    fixture is pytest's to call, never a method of this class, and the API
    dump records only public members, none of which may be a fixture
    definition (API dump spec §3.4).
    """

    __name__ = "otto-fixtures"

    def __init__(self, *, layout: ArtifactLayout) -> None:
        self._layout = layout

    @pytest.fixture(scope="session", name="ctx")
    def _ctx(self) -> Any:
        """Return the active OttoContext for this invocation.

        One object for the whole run — session-scoped so suite-wide fixtures
        may request it.
        """
        from ..context import get_context

        return get_context()

    # ── artifact directories (spec §5.3) ─────────────────────────────────────

    @pytest.fixture(scope="module", name="module_dir")
    @staticmethod
    def _module_dir(request: pytest.FixtureRequest) -> Path:
        """Return this module's artifact directory, shared by every test in it. Created on request.

        ``<run output dir>/<module's path relative to its repo's test root,
        suffix dropped>`` — see
        :meth:`~otto.suite.layout.ArtifactLayout.module_dir` for the exact
        rule (it disambiguates two same-named modules living in different
        packaged test directories). A plain test function and every class in
        the same file share this one directory; a class that wants its own
        shared space makes a subdirectory of it.

        Module-scoped: pytest resolves a class-scoped fixture outside a class
        per function anyway, and the module is the layout's own unit. A
        ``staticmethod`` because it holds no plugin state (the layout comes off
        ``request.config``), not out of necessity — the pytest-9
        class-scoped-instance-method deprecation that
        ``otto_fixtures_plugin_key``'s docstring explains doesn't reach a
        module-scoped fixture at all.
        """
        path = request.config.stash[otto_layout_key].module_dir(request.path)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @pytest.fixture(name="test_dir")
    def _test_dir(self, request: pytest.FixtureRequest) -> Path:
        """Return this test's artifact directory; one ``iteration_N`` level more in stability runs.

        ``module_dir/<Class>/.../<sanitized test name>`` — see
        :meth:`~otto.suite.layout.ArtifactLayout.test_dir`. Parametrized tests
        keep unique names (``test_foo[a]`` → ``test_foo_a_``).

        In stability mode (``--iterations`` / ``--duration``) one more level
        follows — ``.../iteration_1``, ``iteration_2``, … — so each repeat of
        the call phase keeps its own logs and artifacts instead of overwriting
        the previous one's. The fixture resolves once, at setup; the repeat
        loop re-points it per iteration from the base path stashed here.
        """
        base = request.config.stash[otto_layout_key].test_dir(request.node)
        request.node.stash[otto_test_dir_base_key] = base
        path = iteration_dir(base, request.node.stash.get(otto_iteration_key, None))
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ── expect (spec §5.4) ───────────────────────────────────────────────────

    @pytest.fixture(name="expect")
    def _expect(self, request: pytest.FixtureRequest) -> ExpectCollector:
        """Return a callable :class:`~otto.suite.expect.ExpectCollector` for non-fatal checks.

        ``expect(condition, msg)`` records a failure and keeps the test running;
        every failure is logged as it happens and the test FAILS — in the call
        phase, as one combined report — once the body returns. A hard ``assert``
        in the body still wins. ``expect.failures`` is inspectable.
        """
        collector = ExpectCollector(logger=_logger)
        request.node.stash[otto_expect_key] = collector
        return collector

    # ── monitor ──────────────────────────────────────────────────────────────

    @pytest.fixture(name="monitor")
    def _monitor(self, request: pytest.FixtureRequest) -> "Iterator[MonitorHandle]":
        """Start a per-test metrics monitor on demand; stopped for you at teardown.

        ``runner_for(request)`` is resolved here, before the ``yield`` — as a
        same-setup-time dependency, not only at teardown — so pytest tears it
        down LIFO *after* this fixture's own finalizer runs. Deferring the
        lookup to teardown (as this used to) raced a ``loop_scope="function"``
        test's own runner: pytest-asyncio's ``_function_scoped_runner`` is
        function-scoped too, and without this fixture depending on it at
        setup, nothing pins their teardown order — the runner fixture could
        finalize (closing its loop) before ``monitor``'s finalizer asks for
        it, raising at teardown and leaving ``stop()`` never called (the
        archive's ``end`` stays unstamped).
        """
        from .monitor_fixture import MonitorHandle
        from .plugin import otto_plugin_key

        runner = runner_for(request)
        handle = MonitorHandle(plugin=request.config.stash.get(otto_plugin_key, None))
        yield handle
        if handle.started:
            runner.run(handle.stop(), context=contextvars.copy_context())

    @pytest.hookimpl(wrapper=True)
    def pytest_pyfunc_call(self, pyfuncitem: pytest.Function) -> Generator[None, object, object]:
        """Fail the CALL phase when the body returned normally with soft failures recorded.

        ``pytest_pyfunc_call`` rather than ``pytest_runtest_call`` on purpose:
        ``@pytest.mark.retry`` and ``--iterations`` re-run the body through
        ``item.runtest()``, which re-enters this hook — so each attempt starts
        from a reset collector and is judged on its own. A body that raised
        keeps its exception (the soft failures were already logged).
        """
        collector = pyfuncitem.stash.get(otto_expect_key, None)
        if collector is not None:
            collector.reset()
        result = yield
        if collector is not None and collector.failures:
            summary = "\n\n".join(collector.failures)
            pytest.fail(
                f"{len(collector.failures)} expectation(s) failed:\n\n{summary}", pytrace=False
            )
        return result

    # ── the ensure marker ────────────────────────────────────────────────────

    def pytest_configure(self, config: pytest.Config) -> None:
        """Register the built-in markers and park this plugin and its layout.

        ``--strict-markers`` runs accept ``ensure``/``retry`` because of the
        first; code holding only the ``Config`` finds this plugin, and
        ``module_dir``/``test_dir`` find the run's
        :class:`~otto.suite.layout.ArtifactLayout`, because of the second and
        third.
        """
        for line in OTTO_MARKERS.values():
            config.addinivalue_line("markers", line)
        config.stash[otto_fixtures_plugin_key] = self
        config.stash[otto_layout_key] = self._layout

    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        """One duty once collection is complete.

        Refuse an invalid ``ensure`` path before any test runs (spec §4.2).
        Raised as a ``UsageError`` so the session exits with the usage-error
        code and the message names the node, the step and the vocabulary — a
        typo in a marker is a defect in the suite, not a test outcome.

        This hook used to also register the collected modules' logger names
        with otto's capture allowlist. It does not any more: otto configures
        the ROOT logger, so a suite module's ``logging.getLogger(__name__)``
        reaches otto's sinks by propagation, with no plugin support at all.
        """
        for item in items:
            marker = item.get_closest_marker("ensure")
            if marker is None:
                continue
            problem = ensure_path_problem(marker.args)
            if problem is not None:
                raise pytest.UsageError(
                    f"{item.nodeid}: @pytest.mark.ensure{marker.args!r}: {problem}"
                )

    @pytest.fixture(autouse=True)
    def _otto_ensure(self, request: pytest.FixtureRequest) -> None:
        """Converge the lab along the closest ``ensure`` marker's path, on the test's own loop.

        Sync on purpose. A converge opens host connections, and a connection
        belongs to the loop that opened it, so each step must run on the loop
        the test body runs on. An async fixture would run on a loop chosen by
        its own loop scope instead, which is the session loop under
        ``otto test`` even for a test pinned to its class's, module's or own
        loop. So this fixture looks up the runner of the test's own loop
        (:func:`~otto.suite.loops.runner_for`) and drives each step on it.

        Function-scoped: the guarantee is per test CASE, and when the state
        already holds the cost is one status sweep. ``get_closest_marker`` is
        what makes the closest node win outright (test, then class, then
        module); nothing merges.

        The converge builds each install body's options from the ``test``
        verb's parsed flags, as bound on the run's context
        (:meth:`~otto.context.OttoContext.verb_option_source`); with nothing
        bound, every body takes its defaults.
        """
        from ..context import get_context

        marker = request.node.get_closest_marker("ensure")
        if marker is None:
            return
        source = get_context().verb_option_source()
        runner = runner_for(request)
        for step in ensure_path(marker.args):
            runner.run(_converge(step, source), context=contextvars.copy_context())
