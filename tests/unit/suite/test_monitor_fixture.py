"""The monitor fixture replaces the helpers on self (spec §5.2)."""

import asyncio
import contextlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.host.element import Element
from otto.host.factory import create_host_from_dict
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from otto.models.monitor import MonitorSessionFragment
from otto.monitor.collector import MetricCollector
from otto.monitor.db import MetricDB, read_sessions
from otto.monitor.errors import MonitorInputError
from otto.monitor.export import build_db_export
from otto.suite.monitor_fixture import MonitorHandle
from otto.suite.plugin import OttoPlugin
from tests.unit.suite._inner import run_inner

pytest_plugins = ["pytester"]


def _unix(name: str = "box") -> UnixHost:
    host = UnixHost(
        ip="10.0.0.1",
        element=Element(name),
        creds=[Cred(login="a", password="b")],
        log=LogMode.NORMAL,
    )
    host.run = AsyncMock(return_value=None)  # type: ignore[method-assign]  # never poll a real host
    return host


def _embedded(name: str, *, snmp: bool):
    spec = {"ip": "192.0.2.1", "os_type": "embedded", "command_frame": "zephyr"}
    if snmp:
        spec["snmp"] = {"oids": ["1.3.6.1.2.1.1.3.0"]}
    return create_host_from_dict(spec, element=Element(name))


def _embedded_with_snmp():
    return _embedded("s1", snmp=True)


def _embedded_without_snmp(name: str):
    return _embedded(name, snmp=False)


# The pytester sources below run in this interpreter: they stub the lab and
# build one host that is never polled for real.
_INNER_HOST = (
    "from unittest.mock import AsyncMock\n"
    "from types import SimpleNamespace\n"
    "from otto.host.element import Element\n"
    "from otto.host.login_proxy import Cred\n"
    "from otto.host.unix_host import UnixHost\n"
    "def _host(monkeypatch):\n"
    "    monkeypatch.setattr('otto.config.fleet.get_lab', lambda: SimpleNamespace(links=[]))\n"
    "    h = UnixHost(ip='10.0.0.1', element=Element('box'),\n"
    "                 creds=[Cred(login='a', password='b')])\n"
    "    h.run = AsyncMock(return_value=None)\n"
    "    return h\n"
)


@pytest.fixture(autouse=True)
def _stub_lab(monkeypatch):
    """Handles built with ``plugin=None`` read the lab's declared links at start."""
    monkeypatch.setattr("otto.config.fleet.get_lab", lambda: SimpleNamespace(links=[]))


@pytest.mark.parametrize("loop_scope", ["function", "class", "module", "session"])
def test_a_test_that_forgets_to_stop_still_stops_and_stamps_the_end(
    pytester, otto_plugins, tmp_path, loop_scope: str
):
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
        test_m=(
            "import pytest\n" + _INNER_HOST + f"@pytest.mark.asyncio(loop_scope={loop_scope!r})\n"
            "async def test_forgets(monitor, monkeypatch):\n"
            "    await monitor.start(hosts=[_host(monkeypatch)], interval=1.0,\n"
            f"                        db_path={str(db)!r})\n"
            "    assert monitor.started\n"
        ),
    ).assert_outcomes(passed=1)
    import sqlite3

    # A connection's own `with` only commits; `closing` is what closes it.
    with contextlib.closing(sqlite3.connect(db)) as conn:
        (end,) = conn.execute("SELECT end FROM sessions").fetchone()
    assert end is not None


def test_event_results_and_events(pytester, otto_plugins):
    run_inner(
        pytester,
        otto_plugins,
        test_e=(
            _INNER_HOST + "async def test_events(monitor, monkeypatch):\n"
            "    await monitor.start(hosts=[_host(monkeypatch)], interval=1.0)\n"
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
        handle._session = SimpleNamespace(collector=own)
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
        handle._session = SimpleNamespace(collector=collector)
        handle._started = True
        with pytest.raises(ValueError, match="dash"):
            handle.event("checkpoint", dash="wavy")
        collector.add_event.assert_not_called()

    def test_event_rejects_non_hex_color(self) -> None:
        handle = MonitorHandle(plugin=None)
        collector = MagicMock(name="collector")
        handle._session = SimpleNamespace(collector=collector)
        handle._started = True
        with pytest.raises(ValueError, match="color"):
            handle.event("checkpoint", color="red")
        collector.add_event.assert_not_called()

    def test_event_rejects_blank_label(self) -> None:
        handle = MonitorHandle(plugin=None)
        collector = MagicMock(name="collector")
        handle._session = SimpleNamespace(collector=collector)
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
        handle._session = SimpleNamespace(collector=collector)
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
        handle._session = SimpleNamespace(collector=collector)
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
    coroutine (see ``start``'s spawned task).
    """
    await collector.init_db()
    await asyncio.Event().wait()


class TestStartArchive:
    @pytest.mark.asyncio
    async def test_db_output_persists_real_lab_meta_and_end_stamp(self, tmp_path: Path) -> None:
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
    async def test_start_returns_with_session_archive_committed(self, tmp_path: Path) -> None:
        """Regression: nightly/CI flake (issues #136/#137/#142/#143/#144).

        ``MetricDB.open()`` used to run only inside the collector task spawned
        by ``start``, racing server startup. A prompt ``stop()`` then cancelled ``open()`` at
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

    @pytest.mark.asyncio
    async def test_the_labs_declared_links_reach_the_archive(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A link the lab declares between two monitored hosts lands in the lab snapshot."""
        from otto.link.model import Link, LinkEndpoint

        out_path = tmp_path / "monitor.db"
        declared = Link(a=LinkEndpoint(host="r1"), b=LinkEndpoint(host="r2"), name="uplink")
        monkeypatch.setattr("otto.config.fleet.get_lab", lambda: SimpleNamespace(links=[declared]))
        handle = MonitorHandle(plugin=None)

        with patch.object(MetricCollector, "run", _fake_collector_run):
            await handle.start(
                hosts=[_make_unconnected_host("r1"), _make_unconnected_host("r2")],
                db_path=str(out_path),
                interval=1.0,
            )
            await handle.stop()

        (session,) = build_db_export(str(out_path)).sessions
        assert [link.name for link in session.lab.links] == ["uplink"]


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
    async def test_events_and_results_still_read_after_a_completed_stop(self) -> None:
        handle = MonitorHandle(plugin=None)
        await handle.start(hosts=[_unix()], interval=1.0)
        await handle.event("mark", color="#112233")
        await handle.stop()

        assert not handle.started
        assert [e.label for e in handle.events()] == ["mark"]
        assert isinstance(handle.results(), dict)

    @pytest.mark.asyncio
    async def test_a_second_start_replaces_the_stopped_collectors_data(self) -> None:
        handle = MonitorHandle(plugin=None)
        await handle.start(hosts=[_unix()], interval=1.0)
        await handle.event("first")
        await handle.stop()
        assert [e.label for e in handle.events()] == ["first"]

        await handle.start(hosts=[_unix()], interval=1.0)
        assert handle.events() == [], "a fresh start() must not inherit the old collector's data"
        await handle.stop()


# ── collection only: no dashboard ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_serves_nothing_and_returns_none(monkeypatch):
    monkeypatch.setattr("otto.config.fleet.get_lab", lambda: SimpleNamespace(links=[]))
    with patch("otto.monitor.server.MonitorServer") as server_cls:
        handle = MonitorHandle(plugin=None)
        result = await handle.start(hosts=[_unix()], interval=1)
        await handle.stop()
    assert result is None
    server_cls.assert_not_called()


@pytest.mark.asyncio
async def test_start_stamps_the_session_frame_without_a_db(monkeypatch):
    monkeypatch.setattr("otto.config.fleet.get_lab", lambda: SimpleNamespace(links=[]))
    handle = MonitorHandle(plugin=None)
    await handle.start(hosts=[_unix("router1")], interval=1)
    try:
        session = handle._session
        assert session is not None
        assert session.frame.id != ""
        assert [h.id for h in session.lab.hosts] == ["router1"]
    finally:
        await handle.stop()


@pytest.mark.asyncio
async def test_an_snmp_host_is_collected_over_snmp(monkeypatch):
    monkeypatch.setattr("otto.config.fleet.get_lab", lambda: SimpleNamespace(links=[]))
    handle = MonitorHandle(plugin=None)
    await handle.start(hosts=[_embedded_with_snmp()], interval=1)
    try:
        assert handle._session.collector._targets[0].snmp is not None
    finally:
        await handle.stop()


@pytest.mark.asyncio
async def test_a_host_that_cannot_be_sampled_is_refused_at_start(monkeypatch):
    monkeypatch.setattr("otto.config.fleet.get_lab", lambda: SimpleNamespace(links=[]))
    handle = MonitorHandle(plugin=None)
    with pytest.raises(MonitorInputError, match="cannot monitor z"):
        await handle.start(hosts=[_embedded_without_snmp("z")])
    assert not handle.started


@pytest.mark.asyncio
async def test_a_second_start_without_stop_is_refused(monkeypatch):
    monkeypatch.setattr("otto.config.fleet.get_lab", lambda: SimpleNamespace(links=[]))
    handle = MonitorHandle(plugin=None)
    await handle.start(hosts=[_unix()], interval=1)
    try:
        with pytest.raises(RuntimeError, match=r"stop\(\)"):
            await handle.start(hosts=[_unix()], interval=1)
    finally:
        await handle.stop()


@pytest.mark.asyncio
async def test_stop_closes_only_the_archive_never_the_tests_hosts(monkeypatch):
    monkeypatch.setattr("otto.config.fleet.get_lab", lambda: SimpleNamespace(links=[]))
    host = _unix()
    host.close = AsyncMock()  # type: ignore[method-assign]
    handle = MonitorHandle(plugin=None)
    await handle.start(hosts=[host], interval=1)
    await handle.stop()
    host.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_open_still_leaves_stop_to_close_the_archive(tmp_path):
    """The fixture's teardown calls ``stop()`` only while ``started``: an archive that
    half-opened must still be closed, or its connection and file lock leak."""
    handle = MonitorHandle(plugin=None)
    closed = AsyncMock()
    with (
        patch.object(MetricCollector, "init_db", AsyncMock(side_effect=OSError("locked"))),
        patch.object(MetricCollector, "close_db", closed),
    ):
        with pytest.raises(OSError, match="locked"):
            await handle.start(hosts=[_unix()], interval=1, db_path=str(tmp_path / "m.db"))
        assert handle.started
        assert handle._task is None
        await handle.stop()
    assert not handle.started
    closed.assert_awaited()
