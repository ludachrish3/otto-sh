"""
OttoPlugin — internal pytest plugin registered when otto invokes pytest.main().

Provides the ``pytest_runtest_makereport`` hook that attaches the per-phase
test report to each item (as ``item.rep_setup``, ``item.rep_call``,
``item.rep_teardown``). This makes pass/fail status available to fixtures
(including ``OttoSuite._test_lifecycle``) during the teardown phase.

When ``sut_test_dirs`` is supplied, the ``pytest_ignore_collect`` hook
restricts collection to only those directories and their descendants,
ensuring that only tests defined in ``OTTO_SUT_DIRS`` repos are run.

Additional hooks:

``pytest_runtest_protocol``
    Implements stability testing (``--iterations`` / ``--duration``).
    Repeats each test item within a single setup/teardown cycle,
    stopping when the iteration or time limit is reached.

``pytest_runtest_call``
    Implements ``@pytest.mark.retry(n)`` — retries the test body up to *n*
    times on failure, stopping on the first success.

``pytest_runtest_logreport``
    In stability mode, accumulates per-test pass/fail counts into the
    ``StabilityCollector`` attached to the plugin instance.
"""

import asyncio
import re
import time
from collections.abc import AsyncGenerator, Generator
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio
from _pytest.runner import call_and_report, show_test_item

from ..logger import get_otto_logger

logger = get_otto_logger()

#: Stash key indicating that ``--cov`` was passed to ``otto test``.
#: Fixtures can read this to decide whether to preserve ``.gcda`` files
#: on remote hosts for post-run collection.
otto_cov_key: pytest.StashKey[bool] = pytest.StashKey()


class StabilityCollector:
    """Accumulates per-test pass/fail counts across multiple stability runs."""

    def __init__(self) -> None:
        # Maps test node id → (passed_count, total_count)
        self.results: dict[str, tuple[int, int]] = {}

    def record(self, nodeid: str, passed: bool) -> None:
        prev_passed, prev_total = self.results.get(nodeid, (0, 0))
        self.results[nodeid] = (
            prev_passed + (1 if passed else 0),
            prev_total + 1,
        )


class OttoPlugin:
    """Internal pytest plugin used by ``otto test`` to instrument test runs.

    Parameters
    ----------
    sut_test_dirs :
        Resolved test directories from all configured ``OTTO_SUT_DIRS`` repos
        (i.e. the union of ``Repo.tests`` for every repo). When provided,
        collection is restricted to these directories. Pass an empty list or
        omit to disable filtering.
    stability_collector :
        When running in stability mode, pass a ``StabilityCollector`` instance
        here to accumulate pass/fail counts across repeated runs.
    """

    def __init__(
        self,
        sut_test_dirs: list[Path] | None = None,
        stability_collector: StabilityCollector | None = None,
        cov: bool = False,
        iterations: int = 0,
        duration: int = 0,
        monitor: bool = False,
        monitor_interval: float = 5.0,
        monitor_output: Path | None = None,
        monitor_hosts: str | None = None,
    ) -> None:
        self._sut_test_dirs = sut_test_dirs or []
        self._stability_collector = stability_collector
        self._cov = cov
        self._iterations = iterations
        self._duration = duration
        self._monitor = monitor
        self._monitor_interval = monitor_interval
        self._monitor_output = monitor_output
        self._monitor_hosts = monitor_hosts

    def pytest_configure(self, config: pytest.Config) -> None:
        """Enforce auto asyncio mode for OttoSuites.

        OttoSuites always run with ``asyncio_mode=auto`` so that async
        fixtures and test methods work without explicit ``@pytest.mark.asyncio``
        markers.  This is distinct from otto's own unit tests which use
        ``asyncio_mode=strict`` (set in ``pyproject.toml``).

        Per-test timeouts are handled by ``pytest-timeout`` (a runtime
        dependency), which honors ``@pytest.mark.timeout(seconds)`` natively.
        """
        config.option.asyncio_mode = "auto"
        config.stash[otto_cov_key] = self._cov

    def pytest_sessionstart(self, session: pytest.Session) -> None:
        r"""Quiet down pytest's terminal reporter output.

        Two adjustments, both because otto streams its own Rich log output
        and pytest's terse terminal chatter just collides with it. Done here
        rather than in ``pytest_configure`` because the terminalreporter
        isn't registered yet at configure time.

        ``showfspath = False``: in non-verbose mode pytest writes the test
        file path with no trailing newline (``write_fspath_result``),
        expecting per-test progress letters to follow. otto suppresses those
        letters (see :meth:`pytest_report_teststatus`), so the bare path
        would collide with the first log line. otto's ``_otto_log_test_start``
        fixture already logs each test start, making the header redundant.

        ``report_collect``: the "collected N items" line has no granular
        suppression flag — only quiet mode (``verbose < 0``) hides it, which
        would strip other output too. The ``pytest_collection`` hook writes a
        bare, un-terminated ``collecting ...`` prefix that ``report_collect``
        normally rewrites in place into ``collected N items\\n``; simply
        no-oping it would leave that prefix dangling. Instead override it to
        erase the line on the final call and park the cursor at column 0 for
        the next writer. Collection counts are tracked separately and stay
        intact.
        """
        tr = session.config.pluginmanager.get_plugin("terminalreporter")
        if tr is not None:
            tr.showfspath = False

            def _erase_collect_line(final: bool = False) -> None:
                if final and tr.isatty():
                    tr.rewrite("", erase=True)
                    tr.write("\r")

            tr.report_collect = _erase_collect_line

    def pytest_ignore_collect(
        self,
        collection_path: Path,
        config: pytest.Config,  # noqa: ARG002 — required by pytest hook signature
    ) -> bool | None:
        """Ignore any path not under a configured SUT test directory.

        Returns ``True`` (ignore) for paths outside all SUT test dirs.
        Returns ``None`` (collect normally) for paths inside a SUT test dir
        or for ancestor directories that need to be traversed to reach one.
        When no SUT test dirs are configured, all paths are collected normally.
        """
        if not self._sut_test_dirs:
            return None
        for sut_dir in self._sut_test_dirs:
            if collection_path == sut_dir or collection_path.is_relative_to(sut_dir):
                return None
            if sut_dir.is_relative_to(collection_path):
                return None
        return True

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_protocol(
        self, item: pytest.Item, nextitem: pytest.Item | None
    ) -> bool | None:
        """Repeat each test item when stability mode is active.

        When ``--iterations`` or ``--duration`` (or both) are specified,
        each collected test is executed multiple times within a single
        pytest session.  Class-scoped fixtures (``setup_class`` /
        ``teardown_class``) remain cached by pytest for the lifetime of
        the class and fire only once.  Method-scoped fixtures fire on
        every iteration.

        Unlike calling ``runtestprotocol`` in a loop (which tears down
        *all* fixtures including class-scoped ones after each call), this
        hook runs setup once, repeats the call phase N times, then runs
        teardown once.  This keeps class-scoped resources (SSH
        connections, deployed artifacts, etc.) alive across iterations.

        Returns ``True`` to signal that this hook handled the item,
        or ``None`` to fall through to default behaviour.
        """
        if self._iterations <= 0 and self._duration <= 0:
            return None

        max_iters = self._iterations if self._iterations > 0 else float("inf")
        deadline = (time.monotonic() + self._duration) if self._duration > 0 else float("inf")

        # _request, _initrequest, funcargs live on pytest.Function (private
        # API not surfaced on pytest.Item). Duck-type via hasattr and route
        # all access through an Any-cast alias so ty stays out of the way.
        item_any = cast("Any", item)
        hasrequest = hasattr(item, "_request")
        if hasrequest and not item_any._request:  # noqa: SLF001 — deliberate access to pytest.Function._request (private pytest API, cast to Any)
            item_any._initrequest()  # noqa: SLF001 — deliberate access to pytest.Function._initrequest (private pytest API, cast to Any)

        # ── Setup (once) ──────────────────────────────────────────────
        setup_report = call_and_report(item, "setup", log=True)
        if not setup_report.passed:
            # Teardown even on setup failure, then exit
            call_and_report(item, "teardown", log=True, nextitem=nextitem)
            if hasrequest:
                item_any._request = False  # noqa: SLF001 — deliberate access to pytest.Function._request (private pytest API, cast to Any)
                item_any.funcargs = None
            return True

        if item.config.getoption("setupshow", False):
            show_test_item(item, add_space=False)

        # ── Call (repeated) ───────────────────────────────────────────
        iteration = 0
        is_stability = self._iterations > 1 or self._duration > 0
        while iteration < max_iters and time.monotonic() < deadline:
            if is_stability:
                logger.info(f"[bold cyan]--- {item.name} iteration {iteration + 1} ---[/bold cyan]")
            call_and_report(item, "call", log=True)
            iteration += 1

        # ── Teardown (once) ───────────────────────────────────────────
        call_and_report(item, "teardown", log=True, nextitem=nextitem)
        if hasrequest:
            item_any._request = False  # noqa: SLF001 — deliberate access to pytest.Function._request (private pytest API, cast to Any)
            item_any.funcargs = None

        return True

    def pytest_runtest_call(self, item: pytest.Item) -> None:
        """Implement ``@pytest.mark.retry(n)`` — retry the test body up to n times.

        Stops on the first success.  Re-raises the last exception if all
        attempts fail.  Each failed attempt is logged at WARNING level.
        """
        retry_marker = item.get_closest_marker("retry")
        if retry_marker is None:
            return  # normal execution via other hooks

        n: int = int(retry_marker.args[0]) if retry_marker.args else 1
        last_exc: BaseException | None = None
        for attempt in range(n):
            try:
                item.runtest()
            except Exception as exc:  # noqa: PERF203,BLE001 — per-item resilience, retry must catch test exceptions
                last_exc = exc
                logger.warning(f"retry: {item.nodeid} attempt {attempt + 1}/{n} failed: {exc}")
            else:
                return  # success — stop retrying
        if last_exc is not None:
            raise last_exc

    def pytest_report_teststatus(
        self,
        report: pytest.TestReport,
        config: pytest.Config,  # noqa: ARG002 — required by pytest hook signature
    ) -> tuple[str, str, str] | None:
        """Suppress pytest's per-test progress characters.

        otto's RichHandler streams log output to the console in real time,
        so pytest's dot/``F``/``E`` column adds no information and races
        with log records when capture is disabled. Returning an empty
        short-letter keeps the category and verbose word intact (so failure
        summaries and the final pass/fail counts still render) while
        stopping the terminal reporter from writing anything per test.
        """
        if report.passed:
            return ("passed", "", "PASSED")
        if report.failed:
            return ("failed", "", "FAILED")
        if report.skipped:
            return ("skipped", "", "SKIPPED")
        return None

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        """In stability mode, accumulate per-test pass/fail counts."""
        if self._stability_collector is None:
            return
        if report.when != "call":
            return
        self._stability_collector.record(report.nodeid, passed=report.passed)

    @pytest.hookimpl(tryfirst=True, hookwrapper=True)
    def pytest_runtest_makereport(
        self,
        item: pytest.Item,
        call: pytest.CallInfo[None],  # noqa: ARG002 — required by pytest hookwrapper signature
    ) -> Generator[None, None, None]:
        outcome = yield
        # hookwrapper=True: yield returns a pluggy Result whose
        # get_result() surfaces the TestReport; the pytest stubs type it
        # as None, so cast to access the runtime API.
        rep = cast("Any", outcome).get_result()
        setattr(item, f"rep_{rep.when}", rep)

    @pytest_asyncio.fixture(
        scope="session",
        loop_scope="session",
        autouse=True,
    )
    async def _otto_session_monitor(self) -> AsyncGenerator[None, None]:
        """Build the session-scoped :class:`MetricCollector` when ``--monitor`` is set.

        Owns the collector lifecycle: construct the collector over the
        configured hosts, expose it on :class:`OttoSuite` so per-test event
        fixtures (and the per-class collection task below) can reach it,
        export collected data on teardown, then close.

        Note: this fixture *does not* drive ``collector.run()``. OttoSuites
        use ``loop_scope='class'``, so each test class runs on its own event
        loop while the session loop is dormant. A task created here would
        be starved during tests. ``_otto_class_monitor_task`` (class-scoped,
        class loop) drives collection on the loop that's actually ticking.
        """
        if not self._monitor:
            yield
            return

        from ..configmodule import all_hosts
        from ..host import UnixHost
        from ..monitor.factory import build_monitor_collector
        from .suite import OttoSuite

        pattern = re.compile(self._monitor_hosts) if self._monitor_hosts else None
        # build_monitor_collector only handles UnixHost; embedded RTOS
        # targets don't expose the metric-collection commands it issues.
        hosts = [h for h in all_hosts(pattern=pattern) if isinstance(h, UnixHost)]
        if not hosts:
            label = f'matching "{self._monitor_hosts}"' if self._monitor_hosts else ""
            logger.warning(f"--monitor: no hosts {label} — collection disabled.")
            yield
            return

        output = self._monitor_output
        db_path = output if output is not None and output.suffix.lower() == ".db" else None
        collector = build_monitor_collector(hosts=hosts, db_path=db_path)

        OttoSuite._session_monitor_collector = collector  # noqa: SLF001 — intra-package write to OttoSuite class-level monitor collector slot
        try:
            yield
        finally:
            if output is not None and output.suffix.lower() != ".db":
                output.parent.mkdir(parents=True, exist_ok=True)
                collector.export_json(str(output))
                logger.info(f"Monitor data written to {output}")
            elif db_path is not None:
                logger.info(f"Monitor data written to {db_path}")
            await collector.close()
            OttoSuite._session_monitor_collector = None  # noqa: SLF001 — intra-package clear of OttoSuite class-level monitor collector slot

    @pytest_asyncio.fixture(
        scope="class",
        loop_scope="class",
        autouse=True,
    )
    async def _otto_class_monitor_task(self) -> AsyncGenerator[None, None]:
        """Drive ``collector.run()`` on the test class's event loop.

        OttoSuite tests use ``loop_scope='class'``, so a task on the session
        loop never ticks while tests run (events still record because
        ``add_event`` is just a list append from the class loop). Restarting
        the collection task per class on the class loop ensures
        ``_collect_one`` actually executes during tests.

        Collected metrics accumulate on the shared session-scoped collector,
        so a single export at session teardown captures every class's data.
        Between classes, collection pauses — gaps are expected.
        """
        from .suite import OttoSuite

        collector = getattr(OttoSuite, "_session_monitor_collector", None)
        if not self._monitor or collector is None:
            yield
            return

        task = asyncio.create_task(
            collector.run(interval=timedelta(seconds=self._monitor_interval))
        )
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
