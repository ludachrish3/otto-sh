"""The monitor fixture replaces the helpers on self (spec §5.2)."""

import asyncio
import contextlib
import json
import socket
import urllib.request
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.host.element import Element
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.models import MonitorExport
from otto.models.monitor import MonitorSessionFragment
from otto.monitor.collector import MetricCollector
from otto.monitor.db import MetricDB, read_sessions
from otto.monitor.export import build_db_export
from otto.suite.monitor_fixture import MonitorHandle
from otto.suite.plugin import OttoPlugin
from tests.unit.suite._inner import run_inner

pytest_plugins = ["pytester"]


@pytest.mark.parametrize("loop_scope", ["function", "class", "module", "session"])
def test_a_test_that_forgets_to_stop_still_stops_and_stamps_the_end(
    pytester, otto_plugins, tmp_path, hermetic_monitor_dist: Path, loop_scope: str
):
    # monitor.start() launches the real dashboard server, which refuses to
    # start without a built React dist — hence the hermetic one. pytest does
    # not run `make web`, so without this the test passes on any developer
    # checkout (which has a dist) and fails in CI (which does not). This
    # patches a process-global (otto.monitor.server._STATIC_DIR), which the
    # nested in-process session below shares, since `runpytest_inprocess`
    # runs in the SAME interpreter, not a subprocess.
    del hermetic_monitor_dist
    db = tmp_path / "run.db"
    # Parametrized over every loop_scope (fix round 1, Important #1): the
    # `monitor` fixture's teardown must run stop() before ITS OWN loop's
    # runner fixture tears down, whichever scope the test pins — a fixed
    # loop_scope="function" test was the one that reproduced the escape: its
    # own `_function_scoped_runner` is function-scoped too, and without
    # `monitor` depending on it at setup, pytest was free to tear the runner
    # down first, orphaning `stop()`.
    run_inner(
        pytester,
        otto_plugins,
        # No real host: MonitorHandle.start() unconditionally builds a lab
        # snapshot (spec 2026-07-12), which requires a RemoteHost with an
        # `element` set (see otto.monitor.session._host_snapshot) — a bare
        # `LocalHost()` is not a RemoteHost at all (it lacks `ip`/`board`/
        # `slot`/`hop`/`os_type`/etc., confirmed by
        # tests/unit/monitor/test_lab_snapshot.py's own "LocalHost is not a
        # RemoteHost" note) and crashes there. An empty host list sidesteps
        # that pre-existing, out-of-scope gap while still exercising every
        # assertion below.
        test_m=(
            "import pytest\n"
            f"@pytest.mark.asyncio(loop_scope={loop_scope!r})\n"
            "async def test_forgets(monitor):\n"
            f"    await monitor.start(hosts=[], interval=1.0, db_path={str(db)!r})\n"
            "    assert monitor.started\n"
        ),
    ).assert_outcomes(passed=1)
    import sqlite3

    # A connection's own `with` only commits; `closing` is what closes it.
    with contextlib.closing(sqlite3.connect(db)) as conn:
        (end,) = conn.execute("SELECT end FROM sessions").fetchone()
    assert end is not None


def test_event_results_and_events(pytester, otto_plugins, hermetic_monitor_dist: Path):
    del hermetic_monitor_dist
    run_inner(
        pytester,
        otto_plugins,
        test_e=(
            "async def test_events(monitor):\n"
            "    await monitor.start(hosts=[], interval=1.0)\n"
            "    await monitor.event('deploy', color='#ff0000')\n"
            "    assert [e.label for e in monitor.events()] == ['deploy']\n"
            "    assert isinstance(monitor.results(), dict)\n"
            "    await monitor.stop()\n"
            "    await monitor.stop()\n"
        ),
    ).assert_outcomes(passed=1)


def test_event_reaches_the_sessions_collector_via_the_active_plugin(
    pytester, otto_plugins_with_monitor
):
    """The fixture must hand ``MonitorHandle`` the ACTIVE ``OttoPlugin``.

    With no ``start()`` call, ``monitor.event(...)`` has nowhere to record
    EXCEPT the running session's collector — reachable only through
    ``self._plugin.session_monitor_collector``. ``MonitorHandle(plugin=None)``
    (dropping the plugin the fixture is handed) keeps every OTHER test in
    this module green, since they all call ``start()`` themselves; only a
    bare ``event()``-with-no-``start()`` test like this one depends on the
    fixture actually wiring the plugin through. Red under that mutation: with
    no plugin, ``_active_collector()`` returns ``None`` and the 'mark' event
    is silently dropped instead of landing between the auto start/pass pair.
    """
    plugins, events = otto_plugins_with_monitor
    run_inner(
        pytester,
        plugins,
        test_mark=("async def test_it(monitor):\n    await monitor.event('mark')\n"),
    ).assert_outcomes(passed=1)
    labels = [(e.label, e.source) for e in events()]
    assert labels == [
        ("test_mark.test_it: start", "auto"),
        ("mark", "user_code"),
        ("test_mark.test_it: pass", "auto"),
    ]


def test_a_plain_function_and_a_class_both_get_it(pytester, otto_plugins):
    run_inner(
        pytester,
        otto_plugins,
        test_both=(
            "def test_plain(monitor):\n    assert not monitor.started\n"
            "class TestC:\n    def test_m(self, monitor):\n        assert not monitor.started\n"
        ),
    ).assert_outcomes(passed=2)


# ── _active_collector: own collector vs. the running session's ──────────────


def _plugin_with_session_collector(collector: object) -> OttoPlugin:
    """A bare ``OttoPlugin`` exposing *collector* as its session-wide collector.

    Stands in for a running ``otto test --monitor`` session: the plugin is
    never ``pytest_configure``d, so this sets the same slot
    ``OttoPlugin._otto_session_monitor`` would under ``--monitor``.
    """
    plugin = OttoPlugin()
    plugin.session_monitor_collector = collector
    return plugin


class TestActiveCollector:
    """The handle's own collector takes precedence; falls back to the running
    session's collector, set by ``OttoPlugin._otto_session_monitor``."""

    def test_returns_none_when_no_monitor_active(self) -> None:
        handle = MonitorHandle(plugin=None)
        assert handle._active_collector() is None

    def test_own_collector_takes_precedence(self) -> None:
        own = MagicMock(name="own")
        session = MagicMock(name="session")
        handle = MonitorHandle(plugin=_plugin_with_session_collector(session))
        handle._monitor_collector = own
        handle._started = True
        assert handle._active_collector() is own

    def test_falls_back_to_session_collector(self) -> None:
        session = MagicMock(name="session")
        handle = MonitorHandle(plugin=_plugin_with_session_collector(session))
        assert handle._active_collector() is session


# ── event(): validates through the shared event seam ────────────────────────


class TestEvent:
    """``event()`` obeys the same validation as every other marking surface —
    ``EventCreateBody`` is the one seam (Chris's dedup directive).

    Validation must fire synchronously, at the call site, before the
    collector is ever touched: ``event()`` is normally awaited
    (``await monitor.event(...)``, see ``docs/cli/monitor/dashboard.md``),
    but a bad label/color/dash must raise even for a caller that never gets
    that far — calling it and discarding the result without awaiting is
    exactly what a fire-and-forget mistake looks like, and it must still be
    loud.
    """

    def test_event_rejects_invalid_dash_loud(self) -> None:
        handle = MonitorHandle(plugin=None)
        collector = MagicMock(name="collector")
        handle._monitor_collector = collector
        handle._started = True
        with pytest.raises(ValueError, match="dash"):
            handle.event("checkpoint", dash="wavy")
        collector.add_event.assert_not_called()

    def test_event_rejects_non_hex_color(self) -> None:
        handle = MonitorHandle(plugin=None)
        collector = MagicMock(name="collector")
        handle._monitor_collector = collector
        handle._started = True
        with pytest.raises(ValueError, match="color"):
            handle.event("checkpoint", color="red")
        collector.add_event.assert_not_called()

    def test_event_rejects_blank_label(self) -> None:
        handle = MonitorHandle(plugin=None)
        collector = MagicMock(name="collector")
        handle._monitor_collector = collector
        handle._started = True
        with pytest.raises(ValueError, match="label"):
            handle.event("   ")
        collector.add_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_event_valid_call_still_records(self) -> None:
        """Behaviorally unchanged for valid input: the collector still records it."""
        handle = MonitorHandle(plugin=None)
        collector = MagicMock(name="collector")
        collector.add_event = AsyncMock(return_value=None)
        handle._monitor_collector = collector
        handle._started = True
        await handle.event("checkpoint", color="#112233", dash="dot")
        collector.add_event.assert_awaited_once_with(
            label="checkpoint", color="#112233", dash="dot", source="user_code"
        )

    @pytest.mark.asyncio
    async def test_event_reaches_stream_subscribers(self) -> None:
        """A handle-emitted event must arrive as a format:1 fragment in real time
        -- the acceptance criterion behind "events appear while otto test
        --monitor runs" (spec 2026-07-18 Real-time suite events).

        Unlike the MagicMock arrangement above (which only proves
        ``add_event`` was *called* the right way), this uses a REAL
        ``MetricCollector`` so the assertion runs through its actual
        ``subscribe()``/``_publish()`` plumbing -- the same path a real
        dashboard SSE client reads from.
        """
        handle = MonitorHandle(plugin=None)
        collector = MetricCollector(hosts=[])
        collector.session_id = "s-suite-live"
        handle._monitor_collector = collector
        handle._started = True

        q = collector.subscribe()
        await handle.event("checkpoint", color="#112233", dash="dot")
        payload = q.get_nowait()
        frag = MonitorSessionFragment.model_validate(payload)
        assert frag.events
        assert frag.events[0].label == "checkpoint"
        assert frag.session == collector.session_id


# ── start(db_path=...) / stop(): real archive shape ──────────────────────────
#
# Spec 2026-07-12: a --db-backed session must never persist the degraded
# lab_json="{}"/meta_json="{}" scaffold — that renders with no chart specs,
# no units, and no lab topology on replay. Mirrors otto.suite.plugin's own
# db-output test (test_session_monitor_db_output_persists_real_lab_and_meta):
# asserts on the round-tripped artifact via build_db_export, not on the
# MetricDB constructor args.


def _make_unconnected_host(host_id: str = "router1") -> UnixHost:
    """A real UnixHost that makes no connection at construction.

    ``snapshot_lab`` (called unconditionally by ``start``, spec 2026-07-12)
    validates its result against pydantic's ``HostSnapshot`` — a bare
    ``MagicMock(spec=UnixHost)`` fails that validation because its unset
    attributes are auto-vivified ``Mock`` objects, not strings (see
    ``tests/unit/suite/test_plugin.py``'s identical helper).
    """
    return UnixHost(
        ip="10.0.0.1", element=Element(host_id), creds=[Cred(login="admin", password="secret")]
    )


async def _fake_collector_run(collector: MetricCollector, interval, duration=None) -> None:
    """Stand in for ``MetricCollector.run``: open the real DB, then idle.

    Exercises the exact same DB-opening call site (``init_db()``) a real run
    does — so the session row really gets INSERTed/UPDATEd — without
    attempting any host I/O: the host in these tests is a real, unconnected
    UnixHost pointed at a bogus IP, and a genuine collection tick would try
    to SSH to it. Blocks until ``stop()`` cancels the task that owns this
    coroutine (see ``start``'s ``_run()`` wrapper).
    """
    await collector.init_db()
    await asyncio.Event().wait()


class TestStartArchive:
    @pytest.mark.asyncio
    async def test_db_output_persists_real_lab_meta_and_end_stamp(
        self, tmp_path: Path, hermetic_monitor_dist: Path
    ) -> None:
        # start() launches the real dashboard server, which refuses to start
        # without a built React dist — hence the hermetic one. pytest does
        # not run `make web`, so without this the test passes on any developer
        # checkout (which has a dist) and fails in CI (which does not).
        del hermetic_monitor_dist
        out_path = tmp_path / "monitor.db"
        handle = MonitorHandle(plugin=None)

        with patch.object(MetricCollector, "run", _fake_collector_run):
            await handle.start(
                hosts=[_make_unconnected_host("router1")],
                db_path=str(out_path),
                interval=1.0,
            )
            await handle.stop()

        (session,) = build_db_export(str(out_path)).sessions
        # lab: the real snapshot, not "{}"
        assert [h.id for h in session.lab.hosts] == ["router1"]
        # meta: the real parser catalog, not "{}" — chart specs carry the
        # units and grouping the review shell renders from.
        assert session.meta.charts, "session meta persisted with no chart specs"
        assert session.meta.interval == 1.0
        # end: a clean stop() must stamp the RAW column, not rely on the
        # producer's crash-tolerant fallback to paper over a null one —
        # build_db_export()'s SessionRecord.end is NEVER None (_fallback_end
        # always synthesizes one: row.end, else the last sample, else start),
        # so it can't tell a finalized session from a crashed one. Only the
        # archive's own column can (mirrors test_plugin.py's identical check).
        (raw_session,) = read_sessions(str(out_path))
        assert raw_session.end is not None, "a clean stop() left end unstamped"

    @pytest.mark.asyncio
    async def test_start_returns_with_session_archive_committed(
        self, tmp_path: Path, hermetic_monitor_dist: Path
    ) -> None:
        """Regression: nightly/CI flake (issues #136/#137/#142/#143/#144).

        ``MetricDB.open()`` used to run only inside the collector task spawned
        by ``start``'s ``_run()``, racing uvicorn startup — the only thing
        ``start()`` awaits. A prompt ``stop()`` then cancelled ``open()`` at
        whichever await point it had reached, leaving the archive in one of
        three partial states (no tables / user_version 0 / no session row)
        while ``finalize()`` silently no-oped on the never-opened connection.
        The slowed ``open()`` here turns that CI-load coin flip into a
        certainty: pre-fix, the collector task is still sleeping when
        ``start()`` returns, so the read below finds no committed session.
        The invariant pinned: when ``start(db_path=...)`` returns, the
        session row is already committed — before any cancellable task can be
        torn down.
        """
        del hermetic_monitor_dist
        out_path = tmp_path / "monitor.db"
        handle = MonitorHandle(plugin=None)

        real_open = MetricDB.open

        async def slow_open(db: MetricDB) -> None:
            await asyncio.sleep(0.5)
            await real_open(db)

        with (
            patch.object(MetricCollector, "run", _fake_collector_run),
            patch.object(MetricDB, "open", slow_open),
        ):
            await handle.start(
                hosts=[_make_unconnected_host("router1")],
                db_path=str(out_path),
                interval=1.0,
            )
            try:
                (session,) = build_db_export(str(out_path)).sessions
                assert [h.id for h in session.lab.hosts] == ["router1"]
            finally:
                await handle.stop()

        (raw_session,) = read_sessions(str(out_path))
        assert raw_session.end is not None, "a clean stop() left end unstamped"


class TestStartupFailure:
    @pytest.mark.asyncio
    async def test_failure_reaps_monitor_task_and_reraises(
        self, hermetic_monitor_dist: Path
    ) -> None:
        """A startup failure must not leave the dead serve task parked.

        ``start()`` inherits the server's startup failure through
        ``wait_started()`` (gate G7). On that path it must also reap the
        ``_monitor_task`` it just spawned: left behind, a later ``stop()``
        would await the same dead task and surface the identical failure a
        second time, and an unawaited dead task fires "exception was never
        retrieved" at GC.
        """
        del hermetic_monitor_dist
        handle = MonitorHandle(plugin=None)

        # Hold a bound, listening socket so uvicorn's bind fails with the
        # SystemExit the server translates to RuntimeError.
        blocker = socket.socket()
        try:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen(1)
            port = blocker.getsockname()[1]
            with patch.object(MetricCollector, "run", _fake_collector_run):
                with pytest.raises(RuntimeError, match="already in use"):
                    await handle.start(
                        hosts=[_make_unconnected_host("router1")],
                        interval=1.0,
                        port=port,
                    )
                assert handle._monitor_task is None, (
                    "start() re-raised but left the dead serve task in _monitor_task"
                )
                # A cleanup-path stop() after the failure must be a quiet
                # no-op for the task, not a second raise.
                await handle.stop()
        finally:
            blocker.close()


class TestLiveSessionWiring:
    @pytest.mark.asyncio
    async def test_no_db_path_still_stamps_session_id_and_serves_monitor_sessions(
        self, hermetic_monitor_dist: Path
    ) -> None:
        """Pins an escaped defect found building the monitor revamp: ``start()``
        used to build ``frame``/``lab`` only inside its ``if db_path is not
        None:`` branch, so the in-memory-only (``db_path=None``) path — the
        one every suite/pytest-plugin caller actually uses — passed neither
        to ``MonitorServer``. Two silent consequences: (a) ``collector.
        session_id`` stayed ``""``, so every SSE fragment published on this
        path is addressed to a session the browser never holds and is
        dropped; (b) ``/api/monitor_sessions`` in live mode requires
        ``frame``/``lab`` and 500s (``RuntimeError``) without them. This
        boots a real MonitorServer (hence ``hermetic_monitor_dist`` — pytest
        never runs `make web`) and hits the live endpoint over a real socket
        rather than only inspecting private attributes, so a regression that
        broke serving (not just the id stamp) would fail it too.
        """
        del hermetic_monitor_dist
        handle = MonitorHandle(plugin=None)

        with patch.object(MetricCollector, "run", _fake_collector_run):
            await handle.start(
                hosts=[_make_unconnected_host("router1")],
                interval=1.0,
            )
            try:
                assert handle._monitor_collector is not None
                assert handle._monitor_collector.session_id != "", (
                    "collector.session_id was never stamped — MonitorServer "
                    "was built without frame= on the db_path=None path"
                )

                resp = await asyncio.to_thread(
                    urllib.request.urlopen,
                    f"{handle._monitor_server.origin}/api/monitor_sessions?key={handle._monitor_server.key}",
                    timeout=10,
                )
                with contextlib.closing(resp) as opened:
                    payload = json.loads(opened.read())
                export = MonitorExport.model_validate(payload)
                assert export.format == 1
                (session,) = export.sessions
                assert session.id == handle._monitor_collector.session_id
                assert session.end is None, "a live session is one whose end is still open"
            finally:
                await handle.stop()


# ── started / results / events before start ──────────────────────────────────


def test_started_is_false_before_start() -> None:
    handle = MonitorHandle(plugin=None)
    assert not handle.started


def test_results_and_events_are_empty_before_start() -> None:
    handle = MonitorHandle(plugin=None)
    assert handle.results() == {}
    assert handle.events() == []


# ── data survives a completed stop (fix round 1, Minor #3) ──────────────────


class TestDataSurvivesStop:
    """Controller ruling: results()/events() stay readable after stop(); `started`
    is its OWN state, False after a completed stop, not derived from whether a
    collector object still exists. A second start() replaces it."""

    @pytest.mark.asyncio
    async def test_events_and_results_still_read_after_a_completed_stop(
        self, hermetic_monitor_dist: Path
    ) -> None:
        handle = MonitorHandle(plugin=None)
        await handle.start(hosts=[], interval=1.0)
        await handle.event("mark", color="#112233")
        await handle.stop()

        assert not handle.started
        assert [e.label for e in handle.events()] == ["mark"]
        assert isinstance(handle.results(), dict)

    @pytest.mark.asyncio
    async def test_a_second_start_replaces_the_stopped_collectors_data(
        self, hermetic_monitor_dist: Path
    ) -> None:
        handle = MonitorHandle(plugin=None)
        await handle.start(hosts=[], interval=1.0)
        await handle.event("first")
        await handle.stop()
        assert [e.label for e in handle.events()] == ["first"]

        await handle.start(hosts=[], interval=1.0)
        assert handle.events() == [], "a fresh start() must not inherit the old collector's data"
        await handle.stop()
