import asyncio
import inspect
import os
import re
from datetime import datetime, timedelta
from logging import getLogger
from typing import TYPE_CHECKING, Generic, TypeVar, cast

import pytest

from otto.logger.logger import OttoLogger

if TYPE_CHECKING:
    from otto.host.remoteHost import RemoteHost
    from otto.monitor.collector import MetricCollector, MonitorTarget
    from otto.monitor.events import MonitorEvent
    from otto.monitor.parsers import MetricParser
    from otto.monitor.server import MonitorServer

logger: OttoLogger = getLogger('otto') # type: ignore

TOptions = TypeVar('TOptions')


def _sanitize_node_name(name: str) -> str:
    """Replace filesystem-unsafe characters from parametrized test names.

    ``test_foo[router-True]`` becomes ``test_foo_router-True_``.
    """
    return re.sub(r'[\[\]/<>:"|?*\\]', '_', name)


class OttoSuite(Generic[TOptions]):
    """Base class for otto test suites.

    Subclass this and decorate with ``@register_suite()`` to register your
    suite as an ``otto test <ClassName>`` subcommand.  OttoSuite is a plain
    class (not ``unittest.TestCase``), so all standard pytest features work
    natively — fixtures, ``@pytest.mark.parametrize``, markers, conftest.py,
    and yield-based setup/teardown.

    Defining suite options
    ----------------------
    Define a ``@dataclass`` before the suite class, annotate each field with
    ``Annotated[T, typer.Option(help="...")]`` so the help text appears in
    ``otto test <ClassName> --help``, then pass it as the generic argument::

        from dataclasses import dataclass
        from typing import Annotated

        import typer

        from otto.suite import OttoSuite, register_suite

        @dataclass
        class _Opts:
            device_type: Annotated[str, typer.Option(
                help="Kind of device under test ('router', 'switch').",
            )] = "router"

        @register_suite()
        class TestMyDevice(OttoSuite[_Opts]):
            \"\"\"Validate device configuration.\"\"\"
            Options = _Opts

    Accessing options in tests
    --------------------------
    Request the ``suite_options`` fixture as a parameter.  It provides the
    ``Options`` instance constructed from CLI arguments::

        async def test_device_reachable(self, suite_options: _Opts) -> None:
            self.logger.info(f"Testing {suite_options.device_type}")

    Parametrized tests
    ------------------
    Use ``@pytest.mark.parametrize`` on test methods::

        @pytest.mark.parametrize("interface", ["eth0", "eth1", "mgmt0"])
        async def test_interface_up(self, interface: str) -> None:
            self.logger.info(f"Checking {interface}")
            assert True

    Each parameter combination gets its own ``testDir`` with a sanitized name.

    Using fixtures
    --------------
    Test methods can request any pytest fixture as a parameter.  Define
    shared fixtures in a ``conftest.py`` alongside your suite::

        # conftest.py
        @pytest.fixture
        async def primary_host():
            from otto.configmodule import get_host
            host = get_host("primary")
            yield host
            await host.close()

        # test_device.py
        async def test_with_host(self, primary_host) -> None:
            result = await primary_host.oneshot("echo hello")
            assert result.status == Status.Success

    Inheriting repo-wide options
    ----------------------------
    Create a shared base dataclass in your pylib and inherit from it to share
    common options across multiple suites::

        # pylib/my_suites/options.py
        @dataclass
        class RepoOptions:
            lab_env: Annotated[str, typer.Option(
                help="Lab environment to target.",
            )] = "staging"

        # tests/test_device.py
        @dataclass
        class _Opts(RepoOptions):                 # inherits --lab-env
            firmware: Annotated[str, typer.Option(
                help="Firmware version to validate.",
            )] = "latest"

        @register_suite()
        class TestDevice(OttoSuite[_Opts]):
            Options = _Opts

    Both ``--lab-env`` and ``--firmware`` appear in
    ``otto test TestDevice --help``.

    The *same* ``RepoOptions`` dataclass may also be passed to
    ``@instruction(options=...)`` so that ``otto run`` subcommands expose
    the same repo-wide flags as ``otto test`` — see
    :func:`otto.cli.run.instruction`.

    Built-in autouse fixtures
    -------------------------
    OttoSuite provides three autouse fixtures that run for every test:

    - ``_otto_log_test_start`` — logs a banner marking the start of each test
    - ``_otto_test_dir`` — creates ``self.testDir`` with sanitized node name
    - ``_otto_monitor_events`` — records monitor start/end events

    Per-test timeouts are enforced by the shared ``_otto_timeout`` fixture
    in :mod:`otto.suite.timeout`, registered automatically by ``OttoPlugin``.
    Apply ``@pytest.mark.timeout(seconds)`` to individual tests, classes,
    or set a ``timeout`` class attribute on the suite.
    """

    def setup_method(self, method: object = None) -> None:
        self.suiteDir = logger.output_dir
        """Base directory where all artifacts go for the suite"""

        self.logger = logger
        """Logger for writing test info to console and log file."""

        self._expect_failures: list[str] = []
        """Collected non-fatal expectation failures for the current test."""

        self._monitor_collector: 'MetricCollector | None' = None
        self._monitor_server:    'MonitorServer | None'   = None
        self._monitor_task:      'asyncio.Task[None] | None' = None

    def teardown_method(self, method: object = None) -> None:
        logger.debug('Welcome to the base teardown_method() method')

    @classmethod
    def setup_class(cls):
        logger.debug('Welcome to the base setup_class() method')
        cls.testDir = logger.output_dir / 'setupClass'

    @classmethod
    def teardown_class(cls):
        logger.debug('Welcome to the base teardown_class() method')
        cls.testDir = logger.output_dir / 'teardownClass'

    # ── Expect (non-fatal assertions) ────────────────────────────────────

    def expect(self, condition: object, msg: str | None = None) -> None:
        """Record a non-fatal expectation.

        Unlike ``assert``, a failing ``expect()`` does **not** stop the
        test.  All failures are collected and reported together when the
        test finishes — the test is marked failed only at that point.

        Use ``assert`` for preconditions that must hold before the test
        can continue (fatal).  Use ``expect()`` for checks where you want
        to see *all* failures at once (non-fatal).

        Args:
            condition: Any truthy/falsy expression to evaluate.
            msg: Optional human-friendly message printed alongside the
                auto-captured source line and locals — not a replacement.

        Examples:
            Fatal vs non-fatal::

                # Fatal — test stops here if command itself failed
                assert result.status == Status.Success

                # Non-fatal — records failure, test continues
                self.expect("hostname" in result.output)
                self.expect("interface" in result.output)
                self.expect(result.retcode == 0, "unexpected retcode")

            The failure report always includes the source location and
            caller locals.  When *msg* is provided it appears *in addition
            to* the auto-captured source context, never replacing it::

                >>> from unittest.mock import MagicMock
                >>> from otto.suite.suite import OttoSuite
                >>> suite = OttoSuite()
                >>> suite._expect_failures = []
                >>> suite.logger = MagicMock()
                >>> x = 42
                >>> suite.expect(x == 99, "math is broken")
                >>> report = suite._expect_failures[0]
                >>> "Message: math is broken" in report
                True
                >>> "x = 42" in report
                True

        .. note::
            The auto-captured source line and locals are best-effort.
            Provide *msg* when the expression alone isn't self-explanatory.
        """
        if condition:
            return

        # Capture caller context for the failure message
        frame_info = inspect.stack(context=1)[1]
        filename = os.path.basename(frame_info.filename)
        lineno = frame_info.lineno
        source_line = (frame_info.code_context or [''])[0].strip()

        # Build a summary of the caller's local variables
        caller_locals = frame_info.frame.f_locals
        locals_summary = ', '.join(
            f'{k} = {v!r}'
            for k, v in caller_locals.items()
            if not k.startswith('_') and k != 'self'
        )

        # Assemble the failure report
        header = f'{filename}:{lineno}'
        parts = [header, f'  {source_line}']
        if msg:
            parts.append(f'  Message: {msg}')
        if locals_summary:
            parts.append(f'  Locals: {locals_summary}')
        report = '\n'.join(parts)

        self._expect_failures.append(report)
        log_msg = f'[bold yellow]EXPECT FAILED[/bold yellow]  {header}\n  {source_line}'
        if msg:
            log_msg += f'\n  Message: {msg}'
        self.logger.warning(log_msg)

    # ── Autouse fixtures ───────────────────────────────────────────────────

    @pytest.fixture(autouse=True)
    def _otto_log_test_start(self, request: pytest.FixtureRequest):
        """Log a banner announcing the start of each test."""
        node = cast(pytest.Item, request.node)
        logger.info(f'[bold cyan]=== {node.name} ===[/bold cyan]')
        yield

    @pytest.fixture(autouse=True)
    def _otto_test_dir(self, request: pytest.FixtureRequest):
        """Create a per-test artifact directory with a sanitized node name."""
        node_name = _sanitize_node_name(request.node.name)
        logger.debug('_otto_test_dir: setting up testDir for %s', node_name)
        self.testDir = self.suiteDir / 'tests' / node_name
        yield
        if self._expect_failures:
            summary = '\n\n'.join(self._expect_failures)
            pytest.fail(
                f'{len(self._expect_failures)} expectation(s) failed:\n\n{summary}',
                pytrace=False,
            )

    @pytest.fixture(autouse=True)
    async def _otto_monitor_events(self, request: pytest.FixtureRequest):
        """Record monitor start/end events for each test."""
        node      = cast(pytest.Item, request.node)
        node_name: str = node.name

        if self._monitor_collector is not None:
            await self._monitor_collector.add_event(
                label=f'{type(self).__name__}.{node_name}: start',
                color='#888888',
                dash='dash',
                source='auto',
            )

        yield

        if self._monitor_collector is not None:
            rep     = getattr(node, 'rep_call', None)  # type: ignore[arg-type]
            outcome = 'fail' if (rep is not None and not rep.passed) else 'pass'
            color   = '#2ca02c' if outcome == 'pass' else '#d62728'
            await self._monitor_collector.add_event(
                label=f'{type(self).__name__}.{node_name}: {outcome}',
                color=color,
                dash='solid',
                source='auto',
            )

    # ── Monitoring helpers ─────────────────────────────────────────────────

    async def startMonitor(
        self,
        hosts: 'list[RemoteHost] | None' = None,
        interval: 'timedelta | float' = timedelta(seconds=5),
        parsers: 'list[MetricParser] | None' = None,
        port: int = 0,
        bind: str = '127.0.0.1',
        db_path: 'str | None' = None,
        targets: 'list[MonitorTarget] | None' = None,
    ) -> str:
        """
        Start metric collection from all hosts and launch the web dashboard.

        Must be called with ``await``::

            url = await self.startMonitor(hosts=[host])

        All hosts are polled simultaneously on each tick via asyncio.gather().
        Series keys in results are ``"hostname/metric_label"``.

        Args:
            hosts: The RemoteHosts to monitor. Ignored when *targets* is provided.
            interval: How often to poll the hosts. timedelta or float (seconds).
            parsers: Custom metric parsers applied to all hosts. Ignored when *targets* is provided.
            port: TCP port for the dashboard web server (0 = auto-assign).
            bind: Address to bind to. Use '0.0.0.0' for access from other machines.
            db_path: Path for SQLite persistence. If None, data is in-memory only.
            targets: Per-host MonitorTarget objects. When provided, *hosts* and *parsers*
                are ignored. Use this to assign different parsers to different hosts.

        Returns:
            Dashboard URL, e.g. 'http://127.0.0.1:8080'.
        """
        from otto.monitor.collector import MetricCollector
        from otto.monitor.server import MonitorServer

        if targets is None and hosts is None:
            raise ValueError('Provide either hosts or targets')

        if isinstance(interval, (int, float)):
            interval = timedelta(seconds=float(interval))

        if targets is not None:
            self._monitor_collector = MetricCollector(
                targets=targets,
                db_path=db_path,
            )
        else:
            self._monitor_collector = MetricCollector(
                hosts=hosts,
                parsers=parsers,
                db_path=db_path,
            )
        self._monitor_server = MonitorServer(
            self._monitor_collector,
            host=bind,
            port=port,
        )

        collector = self._monitor_collector
        server    = self._monitor_server

        async def _run() -> None:
            task = asyncio.create_task(collector.run(interval))
            try:
                await server.serve()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        self._monitor_task = asyncio.create_task(_run())

        # Wait until the server is ready to accept connections
        while not self._monitor_server.started:
            await asyncio.sleep(0.05)

        url = self._monitor_server.url
        logger.info(f'Monitor dashboard: {url}')
        return url

    async def stopMonitor(self) -> None:
        """Stop metric collection and shut down the dashboard server.

        Must be called with ``await``::

            await self.stopMonitor()
        """
        if self._monitor_server is not None:
            self._monitor_server.stop()
        if self._monitor_task is not None:
            try:
                await asyncio.wait_for(self._monitor_task, timeout=10)
            except asyncio.TimeoutError:
                pass
            self._monitor_task = None
        if self._monitor_collector is not None:
            await self._monitor_collector.close_db()
        self._monitor_server    = None
        self._monitor_collector = None

    async def addMonitorEvent(
        self,
        label: str,
        color: str = '#888888',
        dash:  str = 'dash',
    ) -> None:
        """
        Record a labeled event on the live dashboard at the current time.

        Has no effect if monitoring is not active.
        """
        if self._monitor_collector is not None:
            await self._monitor_collector.add_event(
                label=label,
                color=color,
                dash=dash,
                source='user_code',
            )

    def getMonitorResults(self) -> 'dict[str, list[tuple[datetime, float]]]':
        """Return collected metric series after stopMonitor(). Empty dict if never started."""
        if self._monitor_collector is None:
            return {}
        return {
            key: [(ts, v) for ts, v, _ in pts]
            for key, pts in self._monitor_collector.get_series().items()
        }

    def getMonitorEvents(self) -> 'list[MonitorEvent]':
        """Return all recorded events after stopMonitor(). Empty list if never started."""
        if self._monitor_collector is None:
            return []
        return self._monitor_collector.get_events()
