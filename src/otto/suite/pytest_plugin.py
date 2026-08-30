"""Pytest plugin objects for otto suites.

Imported only when running a suite — kept out of register.py so importing the
registry never pulls in pytest.
"""

import logging
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

from ..errors import EnsureStateError
from .expect import ExpectCollector
from .markers import ENSURE_VERBS, OTTO_MARKERS, ensure_path, ensure_path_problem
from .suite import _sanitize_node_name

if TYPE_CHECKING:
    # Typing only: the converge functions are resolved inside `_converge`
    # (see there), so nothing here needs otto.project at module scope — and
    # `Result` is only ever read, never constructed.
    from ..result import Result

_logger = logging.getLogger(__name__)

otto_expect_key: pytest.StashKey[ExpectCollector] = pytest.StashKey()
"""Where the ``expect`` fixture parks a test's collector for the call-phase wrapper."""


def _raise_unless_converged(result: "Result", step: str) -> None:
    """Turn a non-ok converge *result* into an error naming the failing host.

    ``is_ok``, not ``status is Status.Success``, and the difference is the
    common case: a converge with nothing to do reports ``Status.Skipped``
    ("already installed"), which is a pass. Only ``Failed`` / ``Error`` /
    ``NotRun`` reach the raise.
    """
    if not result.is_ok:
        raise EnsureStateError(f"ensure {step} failed: {result.msg}")


async def _converge(step: str) -> None:
    """Run one ``ensure`` step through the same ``otto.project`` function.

    This is the function ``otto run <verb> --ensure`` calls, so a marker and
    the command cannot diverge. The function is looked up on the package at
    call time (not imported at module scope): ``otto.project`` is the seam
    every other caller uses, and resolving late is what lets a test double
    stand in for it.
    """
    from .. import project

    converge = getattr(project, ENSURE_VERBS[step])
    _raise_unless_converged(await converge(), step)


class OttoOptionsPlugin:
    """Pytest plugin that provides the suite Options instance as a fixture.

    Tests request the ``suite_options`` fixture as a parameter::

        async def test_something(self, suite_options) -> None:
            assert suite_options.device_type == "router"
    """

    __name__ = "otto-options"

    def __init__(self, options: Any | None) -> None:
        self.options = options

    @pytest.fixture(scope="class")
    def suite_options(self, request: pytest.FixtureRequest) -> Any:
        """Return the suite's Options instance.

        Single-suite runs (``otto test <SuiteName> --flags``) pass the
        CLI-built instance in — returned as-is. Selection runs
        (``otto test --tests ...`` / ``-m ...``) span suites, so each suite's
        ``Options`` is default-constructed once per class; required fields
        make the suite's tests fail with a pointer at the single-suite form.
        """
        if self.options is not None:
            return self.options
        cls = getattr(request, "cls", None)
        if cls is None:
            return None
        opts_cls = cls.Options
        if opts_cls is None:
            return None
        try:
            return opts_cls()
        except Exception as exc:  # noqa: BLE001 — opts_cls() may raise pydantic ValidationError, TypeError, or any other construction error; all are reported as a missing-options hint
            pytest.fail(
                f"suite {cls.__name__!r} has required options — "
                f"run `otto test {cls.__name__} ...` to pass them ({exc})",
                pytrace=False,
            )

    @pytest.fixture(scope="session")
    def ctx(self) -> Any:
        """Return the active OttoContext for this invocation.

        One object for the whole run — session-scoped so suite-wide fixtures
        may request it.
        """
        from ..context import get_context

        return get_context()

    # ── artifact directories (spec §5.3) ─────────────────────────────────────

    @pytest.fixture(scope="class")
    @staticmethod
    def suite_dir(request: pytest.FixtureRequest) -> Path:
        """Return this suite's artifact directory: ``<run output dir>/<ClassName>``.

        A plain test function (no class) gets its module's stem instead;
        pytest resolves a class-scoped fixture outside a class per function,
        and the ``mkdir`` is idempotent. Created when requested — like
        ``tmp_path`` — so a test that never names it leaves nothing behind.

        A ``staticmethod`` because pytest 9 deprecates every class-scoped
        fixture whose function is bound to an *instance* — a plugin object's
        included (``resolve_fixture_function`` only checks ``__self__`` is a
        type). Nothing here reads plugin state, so the binding is free to go.
        """
        from ..context import get_context

        output_dir = get_context().output_dir
        if output_dir is None:
            raise RuntimeError("output_dir is not set; create_output_dir must run before suite")
        cls = getattr(request, "cls", None)
        name = cls.__name__ if cls is not None else Path(str(request.module.__file__)).stem
        path = output_dir / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    @pytest.fixture
    def test_dir(self, request: pytest.FixtureRequest, suite_dir: Path) -> Path:
        """This test's artifact directory: ``suite_dir/<sanitized node name>``.

        Parametrized tests keep unique names (``test_foo[a]`` → ``test_foo_a_``).
        """
        path = suite_dir / _sanitize_node_name(request.node.name)
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ── expect (spec §5.4) ───────────────────────────────────────────────────

    @pytest.fixture
    def expect(self, request: pytest.FixtureRequest) -> ExpectCollector:
        """Return a callable :class:`~otto.suite.expect.ExpectCollector` for non-fatal checks.

        ``expect(condition, msg)`` records a failure and keeps the test running;
        every failure is logged as it happens and the test FAILS — in the call
        phase, as one combined report — once the body returns. A hard ``assert``
        in the body still wins. ``expect.failures`` is inspectable.
        """
        collector = ExpectCollector(logger=_logger)
        request.node.stash[otto_expect_key] = collector
        return collector

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
        """Register otto's built-in markers so ``--strict-markers`` runs accept them."""
        for line in OTTO_MARKERS.values():
            config.addinivalue_line("markers", line)

    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        """Two duties once collection is complete.

        First: refuse an invalid ``ensure`` path before any test runs (spec
        §4.2). Raised as a ``UsageError`` so the session exits with the
        usage-error code and the message names the node, the step and the
        vocabulary — a typo in a marker is a defect in the suite, not a test
        outcome.

        Second: hand the collected suite modules' logger names to otto's log
        capture (spec §5.5), so a suite that logs with a module-level
        ``logging.getLogger(__name__)`` reaches otto's sinks.
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
        # Spec §5.5: a suite module is imported by pytest, not by a repo's
        # `init`, so otto's name-prefix logger capture has never seen it. Hand
        # each collected module's top-level name to the same capture the init
        # modules get; a no-op when no output dir is wired (library runs).
        from ..logger.management import capture_external_loggers

        modules = (getattr(item, "module", None) for item in items)
        prefixes = {mod.__name__.split(".", 1)[0] for mod in modules if mod is not None}
        if prefixes:
            capture_external_loggers(prefixes)

    @pytest_asyncio.fixture(autouse=True)
    async def _otto_ensure(self, request: pytest.FixtureRequest) -> None:
        """Converge the lab along the closest ``ensure`` marker's path before the body.

        Function-scoped: the guarantee is per test CASE, and when the state
        already holds the cost is one status sweep. No ``loop_scope``: under
        ``otto test``'s ``ASYNCIO_LOOP_ARGS`` an unpinned function fixture runs
        on the loop the requesting test runs on, which matters because a
        converge opens host connections and a connection is bound to the loop
        that opened it. ``get_closest_marker`` is what makes the closest node
        win outright (test, then class, then module); nothing merges.
        """
        marker = request.node.get_closest_marker("ensure")
        if marker is None:
            return
        for step in ensure_path(marker.args):
            await _converge(step)
