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
    Implements ``@pytest.mark.retry(n)`` by delegating to the shared
    ``otto.suite._retry`` hookwrapper — per-attempt timeout re-arm,
    JUnit/terminal rerun evidence, and double-registration safety live there.

``pytest_runtest_logreport``
    In stability mode, accumulates per-test pass/fail counts into the
    ``StabilityCollector`` attached to the plugin instance.
"""

import asyncio
import logging
import re
import time
from collections.abc import AsyncGenerator, Generator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio
from _pytest.runner import call_and_report, show_test_item

from otto.suite._retry import report_retries, retry_hookwrapper

logger = logging.getLogger(__name__)

#: Stash key indicating that ``--cov`` was passed to ``otto test``.
#: Fixtures can read this to decide whether to preserve ``.gcda`` files
#: on remote hosts for post-run collection.
otto_cov_key: pytest.StashKey[bool] = pytest.StashKey()


_MAX_NAMED_HOSTS = 5
"""How many ids the no-monitorable-hosts warning spells out before summarizing.

The ids are what make the message actionable — the fix is per-host lab config,
so "which host" IS the question — but this branch fires only when EVERY selected
host is unmonitorable, which on a large fleet is a whole-lab misconfiguration
and a wall of ids nobody reads.
"""


def _no_monitorable_hosts_message(walked: "list[Any]") -> str:
    """Explain a ``--monitor`` run that has nothing to sample, per condition.

    THE REGEX IS INNOCENT HERE, and saying otherwise was the defect. A
    ``--monitor-hosts`` pattern that fullmatched nothing — or whose every match
    a membership flag removed — raises
    :class:`~otto.config.scope.EmptySelectionError` before this is reached, so
    getting here with a pattern PROVES the pattern matched. The old label
    (``no hosts matching "X"``) sent that reader off to widen a regex that was
    already right, past the real cause: a host otto's suite collector cannot
    sample. That collector reads metrics over a shell, so a non-Unix host — an
    embedded RTOS console, whose single session cannot be shared with a metrics
    poller — offers it nothing. (``otto monitor`` can also poll SNMP; the suite
    collector cannot, so this message must not offer that route.)

    The other reach of the same branch is an EMPTY walk, which a pattern can
    also arrive at over an empty base set. That one must not mention the
    pattern either — the lab held nothing to select.

    Args:
        walked: The hosts the selection yielded, monitorable or not.

    Returns:
        The one-line WARNING, ids included when there are any.
    """
    if not walked:
        return "--monitor: no hosts available to monitor — collection disabled."
    ids = sorted(host.id for host in walked)
    shown = ", ".join(ids[:_MAX_NAMED_HOSTS])
    if len(ids) > _MAX_NAMED_HOSTS:
        shown += f" (+{len(ids) - _MAX_NAMED_HOSTS} more)"
    return (
        f"--monitor: {len(ids)} host(s) selected, but none of them can be monitored: "
        f"{shown}. The suite collector samples metrics over a shell (Unix hosts) and "
        "these offer none, so the selection is not the problem — collection disabled."
    )


class StabilityCollector:
    """Accumulates per-test pass/fail counts across multiple stability runs."""

    def __init__(self) -> None:
        # Maps test node id → (passed_count, total_count)
        self.results: dict[str, tuple[int, int]] = {}

    def record(self, nodeid: str, passed: bool) -> None:
        """Increment the pass and total counts for *nodeid* by one."""
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

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_call(self, item: pytest.Item) -> Generator[None, Any, None]:
        """Implement ``@pytest.mark.retry(n)`` via the shared hookwrapper.

        A hookwrapper, not a plain impl: ``pytest_runtest_call`` is not
        ``firstresult``, so a plain impl runs *alongside* pytest's default
        runner — the body executed once more after a successful retry, and
        that extra run decided the outcome. All retry semantics live in
        ``otto.suite._retry.retry_hookwrapper``.
        """
        yield from retry_hookwrapper(item)

    def pytest_terminal_summary(self, terminalreporter: Any) -> None:
        """Name every retried test so a pass-after-retries stays visible."""
        report_retries(terminalreporter)

    # PERMANENT(no-tuple-return): pytest dictates this hook's return shape.
    # ast-grep-ignore: no-tuple-return
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

        The *category* must mirror pytest's own categorisation exactly. This
        hook is ``firstresult``; pluggy runs ``tryfirst`` impls, then the rest
        newest-first, then ``trylast``, so otto's undecorated impl runs after
        ``_pytest.subtests`` (``tryfirst``, which therefore still sees every
        report first) and replaces the three below it outright —
        ``_pytest.skipping`` (xfail/xpass), ``_pytest.runner``
        (setup/teardown) and ``_pytest.terminal`` (the rest). In particular a
        *passing* setup or teardown report carries the **empty** category:
        only the ``call`` phase counts towards "passed". Returning "passed"
        for all three phases counted every test three times — a one-test
        suite reported ``3 passed``.
        """
        # mirrors _pytest.skipping.pytest_report_teststatus
        if hasattr(report, "wasxfail"):
            if report.skipped:
                return ("xfailed", "", "XFAIL")
            if report.passed:
                return ("xpassed", "", "XPASS")
        # mirrors _pytest.runner.pytest_report_teststatus
        if report.when in ("setup", "teardown"):
            if report.failed:
                return ("error", "", "ERROR")
            if report.skipped:
                return ("skipped", "", "SKIPPED")
            return ("", "", "")
        # mirrors _pytest.terminal.pytest_report_teststatus
        outcome: str = report.outcome
        if report.when == "collect" and outcome == "failed":
            outcome = "error"
        return (outcome, "", outcome.upper())

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
        """Attach the phase report to *item* so fixtures can inspect pass/fail during teardown."""
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

        from ..config import all_hosts
        from ..config.scope import EmptySelectionError
        from ..host import UnixHost
        from ..monitor.db import MetricDB
        from ..monitor.export import build_live_export, build_session_metric_db, document_json
        from ..monitor.factory import build_monitor_collector
        from ..monitor.session import new_frame, snapshot_lab
        from .suite import OttoSuite

        pattern = re.compile(self._monitor_hosts) if self._monitor_hosts else None
        # The list() is INSIDE the guard, not just the call: `all_hosts` is a
        # generator, so its empty-selection refusal (D6) is raised at the first
        # `next()`. Unguarded it escapes a session-scoped fixture as a raw
        # errored-fixture traceback through pytest-asyncio's internals, once
        # per test in the session, for a one-line mistake in the invocation.
        # `pytest.exit` because that IS the truthful outcome: the user asked
        # for a monitored run over hosts this lab does not have, so the run
        # stops with the error's own words and pytest's usage-error code
        # rather than quietly running unmonitored (the pre-D6 behavior, which
        # answered a question nobody asked) or failing every test.
        try:
            walked = list(all_hosts(pattern=pattern))
        except EmptySelectionError as exc:
            pytest.exit(f"--monitor-hosts: {exc}", returncode=pytest.ExitCode.USAGE_ERROR)
        # build_monitor_collector only handles UnixHost; embedded RTOS
        # targets don't expose the metric-collection commands it issues.
        hosts = [h for h in walked if isinstance(h, UnixHost)]
        if not hosts:
            logger.warning(_no_monitorable_hosts_message(walked))
            yield
            return

        output = self._monitor_output
        db_path = output if output is not None and output.suffix.lower() == ".db" else None
        # Session identity + lab snapshot are built ONCE and shared by BOTH
        # output branches (JSON export below, and the MetricDB passed to
        # build_monitor_collector) so a --monitor run always carries real
        # session framing, never an anonymous/empty one (spec 2026-07-12).
        # No suite/run name is threaded into OttoPlugin today, so `label`
        # stays None (honest, not a placeholder). Declared links aren't
        # resolvable here — the pytest-suite context has no active lab config,
        # only the monitored host objects — so `declared=[]`; implicit
        # hop-links still derive from `hosts` itself.
        frame = new_frame(label=None, note=None)
        lab = snapshot_lab(hosts, declared=[])

        monitor_db: MetricDB | None = None
        if db_path is not None:
            # Same construction-order knot the CLI's `--live --db` path has
            # (see otto.cli.monitor): MetricDB needs meta_json up front, but
            # the collector that will OWN this db can't be built until the db
            # exists. So derive the meta from a throwaway collector over the
            # same hosts — get_meta_model() depends only on hosts + the parser
            # catalog, never on the db. build_session_metric_db is the ONE
            # shared place this construction happens — see its docstring for
            # why persisting "{}" here would render a DB-backed suite run with
            # no chart specs and no units, the same degradation an empty
            # chart_map caused.
            #
            # `interval` MUST be passed explicitly: the collector only records
            # its own interval once run() starts (the class-scoped fixture
            # below), which is after this row is written — so reading it off
            # the model here would persist null forever and leave the replayed
            # session's derived health unresolvable. We have the number right
            # here: it's --monitor-interval.
            meta_collector = build_monitor_collector(hosts=hosts)
            monitor_db = build_session_metric_db(
                str(db_path), frame, lab, meta_collector, interval=self._monitor_interval
            )
        collector = build_monitor_collector(hosts=hosts, db=monitor_db)
        # Open the session archive HERE, not inside the per-class collection
        # task: that task is cancelled at class teardown, and a class that
        # finishes before open() completes would leave a partially-initialized
        # DB — which the NEXT class's retry then rejects as unsupported, with
        # the error swallowed by the task's gather(return_exceptions=True),
        # and the teardown's finalize() no-oping on the never-opened
        # connection (same race as suite.start_monitor — issues #136 etc.).
        # aiosqlite delivers each call's result on the calling loop, so a
        # connection opened on this session loop is safe to write from the
        # class loops that drive run(). (spawn_collection() doesn't fit this
        # cross-loop split; run()'s precondition still enforces the ordering
        # loudly if this await is ever dropped.)
        await collector.init_db()

        OttoSuite._session_monitor_collector = collector  # noqa: SLF001 — intra-package write to OttoSuite class-level monitor collector slot
        # Imported before the try: an ImportError inside the finally would
        # mask the suite body's own exception.
        from ..host.connections import teardown_step

        try:
            yield
        finally:
            # Stamp end BEFORE building/finalizing either output — an
            # unstamped end is the producer's deliberate crash marker (see
            # MetricDB.finalize / otto.monitor.export._fallback_end), so a
            # clean teardown must not leave every session looking crashed.
            end = datetime.now(tz=timezone.utc)
            if output is not None and output.suffix.lower() != ".db":
                frame.end = end
                output.parent.mkdir(parents=True, exist_ok=True)
                export = build_live_export(frame, collector, lab)
                output.write_text(document_json(export))
                logger.info(f"Monitor data written to {output}")
            elif db_path is not None:
                if monitor_db is not None:
                    await monitor_db.finalize(end)
                logger.info(f"Monitor data written to {db_path}")
            with teardown_step("suite monitor", "collector close"):
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
