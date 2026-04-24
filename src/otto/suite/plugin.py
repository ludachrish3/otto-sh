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

import time
from pathlib import Path
from typing import Any, Generator, cast

import pytest
from _pytest.runner import call_and_report, runtestprotocol, show_test_item

from ..logger import getOttoLogger

logger = getOttoLogger()

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
    ) -> None:
        self._sut_test_dirs = sut_test_dirs or []
        self._stability_collector = stability_collector
        self._cov = cov
        self._iterations = iterations
        self._duration = duration

    def pytest_configure(self, config: pytest.Config) -> None:
        """Register the shared async timeout fixture and enforce auto asyncio mode.

        OttoSuites always run with ``asyncio_mode=auto`` so that async
        fixtures and test methods work without explicit ``@pytest.mark.asyncio``
        markers.  This is distinct from otto's own unit tests which use
        ``asyncio_mode=strict`` (set in ``pyproject.toml``).
        """
        from . import timeout
        if not config.pluginmanager.has_plugin('otto-timeout'):
            config.pluginmanager.register(timeout, name='otto-timeout')
        config.option.asyncio_mode = "auto"
        config.stash[otto_cov_key] = self._cov

    def pytest_ignore_collect(
        self,
        collection_path: Path,
        config: pytest.Config,  # noqa: ARG002
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
    def pytest_runtest_protocol(self, item: pytest.Item, nextitem: pytest.Item | None) -> bool | None:
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

        max_iters = self._iterations if self._iterations > 0 else float('inf')
        deadline = (time.monotonic() + self._duration) if self._duration > 0 else float('inf')

        # _request, _initrequest, funcargs live on pytest.Function (private
        # API not surfaced on pytest.Item). Duck-type via hasattr and route
        # all access through an Any-cast alias so ty stays out of the way.
        item_any = cast(Any, item)
        hasrequest = hasattr(item, '_request')
        if hasrequest and not item_any._request:
            item_any._initrequest()

        # ── Setup (once) ──────────────────────────────────────────────
        setup_report = call_and_report(item, 'setup', log=True)
        if not setup_report.passed:
            # Teardown even on setup failure, then exit
            call_and_report(item, 'teardown', log=True, nextitem=nextitem)
            if hasrequest:
                item_any._request = False
                item_any.funcargs = None
            return True

        if item.config.getoption('setupshow', False):
            show_test_item(item)

        # ── Call (repeated) ───────────────────────────────────────────
        iteration = 0
        while iteration < max_iters and time.monotonic() < deadline:
            call_and_report(item, 'call', log=True)
            iteration += 1

        # ── Teardown (once) ───────────────────────────────────────────
        call_and_report(item, 'teardown', log=True, nextitem=nextitem)
        if hasrequest:
            item_any._request = False
            item_any.funcargs = None

        return True

    def pytest_runtest_call(self, item: pytest.Item) -> None:
        """Implement ``@pytest.mark.retry(n)`` — retry the test body up to n times.

        Stops on the first success.  Re-raises the last exception if all
        attempts fail.  Each failed attempt is logged at WARNING level.
        """
        retry_marker = item.get_closest_marker('retry')
        if retry_marker is None:
            return  # normal execution via other hooks

        n: int = int(retry_marker.args[0]) if retry_marker.args else 1
        last_exc: BaseException | None = None
        for attempt in range(n):
            try:
                item.runtest()
                return  # success — stop retrying
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    f'retry: {item.nodeid} attempt {attempt + 1}/{n} failed: {exc}'
                )
        if last_exc is not None:
            raise last_exc

    def pytest_report_teststatus(
        self,
        report: pytest.TestReport,
        config: pytest.Config,  # noqa: ARG002
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
            return ('passed', '', 'PASSED')
        if report.failed:
            return ('failed', '', 'FAILED')
        if report.skipped:
            return ('skipped', '', 'SKIPPED')
        return None

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        """In stability mode, accumulate per-test pass/fail counts."""
        if self._stability_collector is None:
            return
        if report.when != 'call':
            return
        self._stability_collector.record(report.nodeid, passed=report.passed)

    @pytest.hookimpl(tryfirst=True, hookwrapper=True)
    def pytest_runtest_makereport(
        self,
        item: pytest.Item,
        call: pytest.CallInfo[None],  # noqa: ARG002
    ) -> Generator[None, None, None]:
        outcome = yield
        # hookwrapper=True: yield returns a pluggy Result whose
        # get_result() surfaces the TestReport; the pytest stubs type it
        # as None, so cast to access the runtime API.
        rep = cast(Any, outcome).get_result()
        setattr(item, f'rep_{rep.when}', rep)
