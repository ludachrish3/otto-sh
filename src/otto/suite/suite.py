"""``OttoSuite`` base class and supporting fixtures for otto-managed pytest sessions."""

import asyncio
import contextlib
import inspect
import re
from collections.abc import AsyncGenerator, Coroutine, Generator
from datetime import datetime, timedelta, timezone
from logging import getLogger
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast

import pytest
import pytest_asyncio

from otto.context import get_context

if TYPE_CHECKING:
    from otto.host.unix_host import UnixHost
    from otto.monitor.collector import MetricCollector, MonitorTarget
    from otto.monitor.db import MetricDB
    from otto.monitor.events import MonitorEvent
    from otto.monitor.parsers import MetricParser
    from otto.monitor.server import MonitorServer

logger = getLogger(__name__)

TOptions = TypeVar("TOptions")
"""Type variable for the options dataclass of an :class:`OttoSuite` subclass."""


def _sanitize_node_name(name: str) -> str:
    """Replace filesystem-unsafe characters from parametrized test names.

    ``test_foo[router-True]`` becomes ``test_foo_router-True_``.
    """
    return re.sub(r'[\[\]/<>:"|?*\\]', "_", name)


class OttoSuite(Generic[TOptions]):
    """Base class for otto test suites.

    Subclass this with a ``Test*``-prefixed name and it is automatically
    registered as an ``otto test <ClassName>`` subcommand.  OttoSuite is a
    plain class (not ``unittest.TestCase``), so all standard pytest features
    work natively — fixtures, ``@pytest.mark.parametrize``, markers,
    conftest.py, and yield-based setup/teardown.

    Defining suite options
    ----------------------
    Define a ``@dataclass`` before the suite class, annotate each field with
    ``Annotated[T, typer.Option(help="...")]`` so the help text appears in
    ``otto test <ClassName> --help``, then pass it as the generic argument::

        from dataclasses import dataclass
        from typing import Annotated

        import typer

        from otto.suite import OttoSuite

        @dataclass
        class _Opts:
            device_type: Annotated[str, typer.Option(
                help="Kind of device under test ('router', 'switch').",
            )] = "router"

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
            from otto.config import get_host

            host = get_host("primary")
            yield host
            await host.close()


        # test_device.py
        async def test_with_host(self, primary_host) -> None:
            result = await primary_host.exec("echo hello")
            assert result.status == Status.Success

    Inheriting repo-wide options
    ----------------------------
    Create a shared base in your pylib and inherit from it to share common
    options across multiple suites. Decorate Options classes with ``@options``
    (``from otto import options`` — a re-export of
    ``pydantic.dataclasses.dataclass``) to get validation for free::

        # pylib/my_suites/options.py
        from otto import options
        from pydantic import Field


        @options
        class RepoOptions:
            lab_env: Annotated[
                str,
                typer.Option(
                    help="Lab environment to target.",
                ),
            ] = "staging"


        # tests/test_device.py
        @options
        class _Opts(RepoOptions):  # inherits --lab-env
            retries: Annotated[
                int,
                typer.Option(
                    help="Connection retries (must be >= 0).",
                ),
            ] = Field(default=3, ge=0)


        class TestDevice(OttoSuite[_Opts]):
            Options = _Opts

    Both ``--lab-env`` and ``--retries`` appear in
    ``otto test TestDevice --help``. ``@options`` is a drop-in for ``@dataclass``
    that adds validation: ``--retries -1`` now fails with a clean CLI error
    instead of being silently accepted. A plain ``@dataclass`` still works —
    validation is simply opt-in per Options class.

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

    Per-test timeouts are enforced by ``pytest-timeout`` (a runtime
    dependency). Apply ``@pytest.mark.timeout(seconds)`` to individual tests
    or classes; on timeout the test fails and the session continues.
    """

    #: Set by ``OttoPlugin._otto_session_monitor`` when ``otto test --monitor``
    #: drives session-wide collection.  Falls back to ``None`` so per-suite
    #: ``start_monitor()`` calls keep working unchanged.
    _session_monitor_collector: "MetricCollector | None" = None

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Auto-register ``Test*``-named subclasses as ``otto test`` subcommands.

        Matches pytest's own ``python_classes = Test*`` collection rule, so a
        shared base class (``BaseSomething(OttoSuite)``) is naturally skipped.
        pytest re-imports a suite file under its own module name when a suite
        runs; the registry's same-file overwrite rule absorbs that re-fire.
        """
        super().__init_subclass__(**kwargs)
        if cls.__name__.startswith("Test"):
            from otto.suite.register import register_suite_class

            register_suite_class(cls)

    def setup_method(self, method: object = None) -> None:  # noqa: ARG002 — required by pytest setup_method hook signature
        """Initialise per-test instance attributes before each test method runs."""
        output_dir = get_context().output_dir
        if output_dir is None:
            raise RuntimeError("output_dir is not set; create_output_dir must run before suite")
        self.suiteDir = output_dir
        """Base directory where all artifacts go for the suite"""

        self.logger = logger
        """Logger for writing test info to console and log file."""

        self._expect_failures: list[str] = []
        """Collected non-fatal expectation failures for the current test."""

        self._monitor_collector: "MetricCollector | None" = None
        self._monitor_server: "MonitorServer | None" = None
        self._monitor_task: "asyncio.Task[None] | None" = None
        self._monitor_db: "MetricDB | None" = None

    def _active_monitor_collector(self) -> "MetricCollector | None":
        """Return the per-suite collector if active, else the session-wide one."""
        if self._monitor_collector is not None:
            return self._monitor_collector
        return type(self)._session_monitor_collector  # noqa: SLF001 — intra-package read of OttoSuite class-level monitor collector via runtime type

    def teardown_method(self, method: object = None) -> None:  # noqa: ARG002 — required by pytest teardown_method hook signature
        """Override in subclasses to clean up after each test method (no-op in the base class)."""
        logger.debug("Welcome to the base teardown_method() method")

    @classmethod
    def setup_class(cls) -> None:
        """Set ``cls.testDir`` to the ``setupClass`` output sub-directory before the class runs."""
        logger.debug("Welcome to the base setup_class() method")
        output_dir = get_context().output_dir
        if output_dir is None:
            raise RuntimeError("output_dir is not set; create_output_dir must run before suite")
        cls.testDir = output_dir / "setupClass"

    @classmethod
    def teardown_class(cls) -> None:
        """Set ``cls.testDir`` to the ``teardownClass`` sub-directory after the class finishes."""
        logger.debug("Welcome to the base teardown_class() method")
        output_dir = get_context().output_dir
        if output_dir is None:
            raise RuntimeError("output_dir is not set; create_output_dir must run before suite")
        cls.testDir = output_dir / "teardownClass"

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
                self.expect("hostname" in result.value)
                self.expect("interface" in result.value)
                self.expect(result.retcode == 0, "unexpected retcode")

            The failure report always includes the source location and
            caller locals.  When *msg* is provided it appears *in addition
            to* the auto-captured source context, never replacing it:

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
        filename = Path(frame_info.filename).name
        lineno = frame_info.lineno
        source_line = (frame_info.code_context or [""])[0].strip()

        # Build a summary of the caller's local variables
        caller_locals = frame_info.frame.f_locals
        locals_summary = ", ".join(
            f"{k} = {v!r}"
            for k, v in caller_locals.items()
            if not k.startswith("_") and k != "self"
        )

        # Assemble the failure report
        header = f"{filename}:{lineno}"
        parts = [header, f"  {source_line}"]
        if msg:
            parts.append(f"  Message: {msg}")
        if locals_summary:
            parts.append(f"  Locals: {locals_summary}")
        report = "\n".join(parts)

        self._expect_failures.append(report)
        log_msg = f"[bold yellow]EXPECT FAILED[/bold yellow]  {header}\n  {source_line}"
        if msg:
            log_msg += f"\n  Message: {msg}"
        self.logger.warning(log_msg)

    # ── Autouse fixtures ───────────────────────────────────────────────────

    @pytest.fixture(autouse=True)
    def _otto_log_test_start(self, request: pytest.FixtureRequest) -> None:
        """Log a banner announcing the start of each test."""
        node = cast("pytest.Item", request.node)
        logger.info(f"[bold cyan]=== {node.name} ===[/bold cyan]")

    @pytest.fixture(autouse=True)
    def _otto_test_dir(self, request: pytest.FixtureRequest) -> Generator[None, None, None]:
        """Create a per-test artifact directory with a sanitized node name."""
        node_name = _sanitize_node_name(request.node.name)
        logger.debug("_otto_test_dir: setting up testDir for %s", node_name)
        self.testDir = self.suiteDir / "tests" / node_name
        yield
        if self._expect_failures:
            summary = "\n\n".join(self._expect_failures)
            pytest.fail(
                f"{len(self._expect_failures)} expectation(s) failed:\n\n{summary}",
                pytrace=False,
            )

    @pytest.fixture(autouse=True)
    async def _otto_monitor_events(
        self, request: pytest.FixtureRequest
    ) -> AsyncGenerator[None, None]:
        """Record monitor start/end events for each test."""
        node = cast("pytest.Item", request.node)
        node_name: str = node.name

        collector = self._active_monitor_collector()
        if collector is not None:
            await collector.add_event(
                label=f"{type(self).__name__}.{node_name}: start",
                color="#888888",
                dash="dash",
                source="auto",
            )

        yield

        collector = self._active_monitor_collector()
        if collector is not None:
            rep = getattr(node, "rep_call", None)  # type: ignore[arg-type]
            outcome = "fail" if (rep is not None and not rep.passed) else "pass"
            color = "#2ca02c" if outcome == "pass" else "#d62728"
            await collector.add_event(
                label=f"{type(self).__name__}.{node_name}: {outcome}",
                color=color,
                dash="solid",
                source="auto",
            )

    @pytest_asyncio.fixture(autouse=True, scope="class", loop_scope="class")
    async def _otto_release_connections(
        self, request: pytest.FixtureRequest
    ) -> AsyncGenerator[None, None]:
        """Release host connections at class teardown during coverage runs.

        OttoSuites run each test class on its own event loop
        (``loop_scope='class'``). A persistent shell session — and the single
        socket of an RTOS telnet console — is bound to the loop that opened it.
        Under ``--cov`` the coverage collector runs *after* pytest on a separate
        ``asyncio.run`` loop (see ``otto.coverage.collect.collect_coverage``); a session
        opened here and reused from that loop hangs — its reads await futures on
        the now-closed class loop — and the stale single-client socket still
        holds the device's only telnet slot, blocking the collector's reconnect.
        Closing connections here, in the class loop that created them, lets the
        collector connect fresh (the loaded LLEXT product stays resident on the
        device — only the TCP session is dropped, not the extension).

        Scoped to ``--cov`` so ordinary runs keep their persistent sessions and
        pay no reconnect cost; outside coverage there is no cross-loop consumer.
        """
        yield
        from .plugin import otto_cov_key

        if not request.config.stash.get(otto_cov_key, False):
            return

        from ..config import all_hosts

        # include_local: this teardown flushes sessions (which is what makes
        # remotes write .gcda) — close everything the suite may have touched,
        # including the built-in local host, even though it isn't fleet.
        for host in all_hosts(include_local=True):
            try:
                await host.close()
            except Exception as exc:  # noqa: PERF203,BLE001 — per-item teardown resilience, best-effort host close
                logger.debug(
                    "OttoSuite: error closing %s at class teardown: %s",
                    getattr(host, "id", host),
                    exc,
                )

    # ── Monitoring helpers ─────────────────────────────────────────────────

    async def start_monitor(
        self,
        hosts: "list[UnixHost] | None" = None,
        interval: "timedelta | float" = timedelta(seconds=5),
        parsers: "list[MetricParser] | None" = None,
        port: int = 0,
        bind: str = "127.0.0.1",
        db_path: "str | None" = None,
        targets: "list[MonitorTarget] | None" = None,
    ) -> str:
        """
        Start metric collection from all hosts and launch the web dashboard.

        Must be called with ``await``::

            url = await self.start_monitor(hosts=[host])

        All hosts are polled simultaneously on each tick via asyncio.gather().
        Series keys in results are ``"hostname/metric_label"``.

        Args:
            hosts: The UnixHosts to monitor. Ignored when *targets* is provided.
            interval: How often to poll the hosts. timedelta or float (seconds).
            parsers: Custom metric parsers applied to all hosts. Ignored when *targets* is provided.
            port: TCP port for the dashboard web server (0 = auto-assign).
            bind: Address to bind to. Use '0.0.0.0' for access from other machines.
            db_path: Path for SQLite persistence. If None, data is in-memory only.
            targets: Per-host MonitorTarget objects. When provided, *hosts* and *parsers*
                are ignored. Use this to assign different parsers to different hosts.

        Returns:
            Dashboard URL, keyed with the per-run access key, e.g.
            'http://127.0.0.1:8080/?key=abc123...'.
        """
        from otto.models import validate_interval
        from otto.monitor.collector import MetricCollector
        from otto.monitor.export import build_session_metric_db
        from otto.monitor.server import MonitorServer
        from otto.monitor.session import new_frame, snapshot_lab

        if targets is None and hosts is None:
            raise ValueError("Provide either hosts or targets")

        if isinstance(interval, (int, float)):
            interval = timedelta(seconds=float(interval))
        validate_interval(interval.total_seconds())

        # Real session identity + lab snapshot (spec 2026-07-12), built
        # unconditionally: MonitorServer stamps collector.session_id from
        # `frame` and (live mode) needs both `frame`/`lab` to serve
        # /api/monitor_sessions, regardless of whether this run also
        # persists to a --db archive. No suite/run name is threaded through
        # today, so `label`/`note` stay None (honest, not a placeholder) —
        # same as otto.suite.plugin's --monitor --db path.
        snapshot_hosts = (
            [target.host for target in targets] if targets is not None else list(hosts or [])
        )
        frame = new_frame(label=None, note=None)
        lab = snapshot_lab(snapshot_hosts, declared=[])

        monitor_db = None
        if db_path is not None:
            # See otto.monitor.export.build_session_metric_db for why a
            # throwaway meta_collector (never run, never handed this
            # MetricDB), built the SAME way (targets, or hosts+parsers) as
            # the real collector below, is unavoidable here. chart_map is
            # NOT built here either: it accumulates as points arrive, so the
            # collector writes it into the session row itself
            # (MetricDB.write_chart_map).
            meta_collector = (
                MetricCollector(targets=targets)
                if targets is not None
                else MetricCollector(hosts=hosts, parsers=parsers)
            )
            monitor_db = build_session_metric_db(
                db_path, frame, lab, meta_collector, interval=interval.total_seconds()
            )
        self._monitor_db = monitor_db

        if targets is not None:
            self._monitor_collector = MetricCollector(
                targets=targets,
                db=monitor_db,
            )
        else:
            self._monitor_collector = MetricCollector(
                hosts=hosts,
                parsers=parsers,
                db=monitor_db,
            )
        self._monitor_server = MonitorServer(
            self._monitor_collector,
            host=bind,
            port=port,
            frame=frame,
            lab=lab,
        )

        collector = self._monitor_collector
        server = self._monitor_server

        # Open the session archive BEFORE spawning the collector task.
        # collector.run() also calls init_db() (idempotent), but in-task the
        # open races server startup — the only thing this method awaits — so
        # a prompt stop_monitor() could cancel open() mid-flight, leaving a
        # partially-initialized DB that finalize() silently no-ops on
        # (nightly/CI flake, issues #136/#137/#142/#143/#144). Awaiting it
        # here also surfaces a locked/unsupported --db file as a loud error
        # at start, instead of dying inside the task where _run()'s gather
        # (return_exceptions=True) would swallow it.
        await collector.init_db()

        async def _run() -> None:
            task = asyncio.create_task(collector.run(interval))
            try:
                await server.serve()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        self._monitor_task = asyncio.create_task(_run())

        # Wait until the server is ready to accept connections
        while not self._monitor_server.started:  # noqa: ASYNC110 — polling external uvicorn state; no event source available
            await asyncio.sleep(0.05)

        url = self._monitor_server.url
        # SECURITY: `url` carries the per-run ?key=<token> access credential, so
        # log only the keyless origin — `serve()` already prints the full keyed
        # URL straight to the terminal. Writing the key here would persist a live
        # credential to the suite run's console.log / verbose.log.
        logger.info(f"Monitor dashboard: {self._monitor_server.origin}")
        return url

    async def stop_monitor(self) -> None:
        """Stop metric collection and shut down the dashboard server.

        Must be called with ``await``::

            await self.stop_monitor()
        """
        if self._monitor_server is not None:
            self._monitor_server.stop()
        if self._monitor_task is not None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._monitor_task, timeout=10)
            self._monitor_task = None
        if self._monitor_db is not None:
            # Stamp this session's end BEFORE closing the DB connection below
            # (finalize() is a no-op once closed) — an unstamped end is the
            # producer's deliberate crash marker (MetricDB.finalize /
            # otto.monitor.export._fallback_end), so a clean stop_monitor()
            # must not leave every archived suite run looking crashed.
            await self._monitor_db.finalize(datetime.now(tz=timezone.utc))
        if self._monitor_collector is not None:
            await self._monitor_collector.close_db()
        self._monitor_server = None
        self._monitor_collector = None
        self._monitor_db = None

    def add_monitor_event(
        self,
        label: str,
        color: str = "#888888",
        dash: str = "dash",
    ) -> Coroutine[Any, Any, None]:
        """
        Record a labeled event on the live dashboard at the current time.

        Has no effect if monitoring is not active.  Honors a per-suite
        collector created by :meth:`start_monitor` first, then falls back to
        the session-wide collector started by ``otto test --monitor``.

        Validates through the same seam as every other marking surface
        (:class:`~otto.models.monitor.EventCreateBody`, Plan 5c) — a blank
        label, a non-``#rrggbb`` color, or an unknown dash style raises
        ``pydantic.ValidationError`` (a ``ValueError`` subclass) synchronously,
        at the call site, before the collector is ever touched. Call sites
        still ``await`` the result (``await self.add_monitor_event(...)``) —
        this is a plain (non-``async``) method only so that raise happens
        immediately even if the caller forgets to await it, rather than
        silently vanishing into an unawaited coroutine.
        """
        # Same rules as every other marking surface (Plan 5c, one seam):
        # constructing the body IS the validation — pydantic.ValidationError
        # (a ValueError) surfaces bad input at the call site instead of
        # persisting a style the dashboard cannot render.
        from ..models.monitor import EventCreateBody

        EventCreateBody(label=label, color=color, dash=dash)
        return self._record_monitor_event(label, color, dash)

    async def _record_monitor_event(self, label: str, color: str, dash: str) -> None:
        """Do the actual write; split out so validation above stays synchronous."""
        collector = self._active_monitor_collector()
        if collector is not None:
            await collector.add_event(
                label=label,
                color=color,
                dash=dash,
                source="user_code",
            )

    def get_monitor_results(self) -> "dict[str, list[tuple[datetime, float]]]":
        """Return collected metric series after stop_monitor(). Empty dict if never started."""
        if self._monitor_collector is None:
            return {}
        return {
            key: [(pt.ts, pt.value) for pt in pts]
            for key, pts in self._monitor_collector.get_series().items()
        }

    def get_monitor_events(self) -> "list[MonitorEvent]":
        """Return all recorded events after stop_monitor(). Empty list if never started."""
        if self._monitor_collector is None:
            return []
        return self._monitor_collector.get_events()
