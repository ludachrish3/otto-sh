"""Tests for MetricCollector.run() backpressure behavior.

These tests verify that the collection loop:
  - Collects from multiple hosts each tick
  - Times out slow hosts without blocking fast hosts (via run timeout)
  - Continues collecting after host errors
  - Respects the duration parameter
  - Passes the interval as a cumulative timeout to run
  - Parses real parser output into series and the archive (``TestCollectorLiveRun``)
"""

import asyncio
import contextlib
import heapq
import itertools
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from otto.monitor.collector import MetricCollector, MonitorTarget
from otto.monitor.db import MetricDB
from otto.monitor.parsers import LoadParser, MemParser, MetricDataPoint, MetricParser, ParseContext
from otto.monitor.session import new_frame
from otto.monitor.snmp import OID_SYS_UPTIME, SnmpSource
from otto.result import CommandResult, Results
from otto.utils import Status

# Bound at import, BEFORE any test patches otto.monitor.collector.asyncio.sleep:
# that patch target rebinds `sleep` on the GLOBAL asyncio module (the collector
# holds no private copy), so the driver below must yield through this captured
# real function or its own settle yields would become virtual sleepers.
_REAL_SLEEP = asyncio.sleep


class _VirtualClock:
    """Parallel-aware virtual clock for the collector's bucket loops.

    Replaces ``otto.monitor.collector``'s ``asyncio.sleep`` AND its
    ``datetime.now`` with one shared timeline, so tick counts become exact
    (review §3.6: knowable 4 ticks were asserted ``>= 2``). Unlike
    test_utils_wait_for.py's single-consumer FakeClock, this one models
    CONCURRENT sleepers: each sleeper parks on a heap keyed by wake time and
    the driver advances ``now`` to the EARLIEST pending wake only once every
    runnable task has settled — two buckets sleeping 0.05 and 0.2 advance in
    parallel, not summed.

    A mock host's own delay is NOT a foreign clock: the patch target rebinds
    ``sleep`` on the global asyncio module (see ``_REAL_SLEEP`` above), so
    ``_make_mock_host``'s ``asyncio.sleep(delay)`` parks on this same
    timeline and a collect that "takes" 0.15s costs exactly 0.15 virtual
    seconds. That is what lets the slow-host and cadence tests below assert
    exact tick counts instead of counting ticks inside a wall-clock window
    (#407: a runner stall ends such a window at one tick, so no floor above
    1 survives). What stays real is the collector's own
    ``asyncio.wait_for`` on the SNMP path (an event-loop timer, not
    ``sleep``) and anything that MEASURES elapsed wall time — which is why
    ``test_slow_host_does_not_block_fast_host`` deliberately does not use
    this clock: its subject IS the wall clock.
    """

    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self._sleepers: list[tuple[datetime, int, asyncio.Event]] = []
        self._seq = itertools.count()

    def now_fn(self, tz=None):
        return self.now

    async def sleep(self, secs: float) -> None:
        ev = asyncio.Event()
        heapq.heappush(self._sleepers, (self.now + timedelta(seconds=secs), next(self._seq), ev))
        await ev.wait()

    async def drive(self, coro, *, cancel_at: timedelta | None = None) -> None:
        """Run *coro* to completion (or cancel once ``now`` reaches
        *cancel_at* past start), advancing virtual time between settles.

        Check order is load-bearing for exact counts: each pass SETTLES the
        runnable tasks, then tests cancel_at, then advances — so a wake
        scheduled exactly at/past cancel_at still collects once before the
        cancel lands (the per-parser test's 13th fast tick). The 50-yield
        settle is a heuristic; if it ever under-settles, the symptom is an
        exact-count mismatch, not a hang."""
        start = self.now
        task = asyncio.create_task(coro)
        try:
            while not task.done():
                for _ in range(50):
                    await _REAL_SLEEP(0)
                    if task.done():
                        break
                if task.done():
                    break
                if cancel_at is not None and self.now - start >= cancel_at:
                    task.cancel()
                    break
                if not self._sleepers:
                    continue
                wake, _, ev = heapq.heappop(self._sleepers)
                self.now = max(self.now, wake)
                ev.set()
        finally:
            with contextlib.suppress(asyncio.CancelledError):
                await task


@contextlib.contextmanager
def _virtual_time():
    vt = _VirtualClock()
    with (
        patch("otto.monitor.collector.asyncio.sleep", new=vt.sleep),
        patch("otto.monitor.collector.datetime", new=SimpleNamespace(now=vt.now_fn)),
    ):
        yield vt


class StubParser(MetricParser):
    """Minimal parser for testing — returns a single data point."""

    chart = "Test"
    y_title = "Value"
    unit = ""
    command = "echo 42"

    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint] | None:
        try:
            return {"value": MetricDataPoint(float(output.strip()))}
        except ValueError:
            return None


def _make_mock_host(name: str, delay: float = 0.0, fail: bool = False) -> MagicMock:
    """Create a mock host whose run returns after *delay* seconds.

    The mock respects the ``timeout`` kwarg: if the delay exceeds the timeout,
    the command returns ``Status.Error`` with a timeout message — mimicking
    the real ``run`` deadline behavior.

    If *fail* is True, run raises RuntimeError instead.
    """
    host = MagicMock()
    host.name = name
    host.id = name
    host.log = LogMode.QUIET

    async def _run_cmds(cmds, timeout=None, log=None):
        if fail:
            raise RuntimeError(f"{name} is unreachable")
        # Only apply delay to collection commands, not the one-time
        # setup command (grep ^processor) which has no timeout.
        is_setup = len(cmds) == 1 and "processor" in cmds[0]
        if delay > 0 and not is_setup:
            if timeout is not None and delay > timeout:
                # Simulate what real run does: the command times out
                # via _run_one's wait_for, session recovers, returns Error
                await asyncio.sleep(timeout)
                results = [
                    CommandResult(
                        Status.Error,
                        value=f"Command timed out after {timeout}s",
                        command=cmd,
                        retcode=-1,
                    )
                    for cmd in cmds
                ]
                return Results.collect(results)
            await asyncio.sleep(delay)
        results = [
            CommandResult(Status.Success, value="42\n", command=cmd, retcode=0) for cmd in cmds
        ]
        return Results.collect(results)

    host.run = AsyncMock(side_effect=_run_cmds)
    return host


def _build_collector(hosts: list[MagicMock]) -> MetricCollector:
    """Build a MetricCollector with mock targets and no DB."""
    parsers = {StubParser.command: StubParser()}
    targets = [MonitorTarget(host=h, parsers=parsers) for h in hosts]
    return MetricCollector(targets=targets)


class TestCollectorRun:
    @pytest.mark.asyncio
    async def test_normal_collection(self):
        """Two fast hosts both produce data within a short run."""
        host_a = _make_mock_host("host_a")
        host_b = _make_mock_host("host_b")
        collector = _build_collector([host_a, host_b])

        with _virtual_time() as vt:
            await vt.drive(
                collector.run(
                    interval=timedelta(milliseconds=100),
                    duration=timedelta(milliseconds=350),
                )
            )

        series = collector.get_series()
        assert "host_a/value" in series, f"host_a missing from series: {list(series)}"
        assert "host_b/value" in series, f"host_b missing from series: {list(series)}"
        # Exact on the virtual clock: the pre-loop initial collect at t=0,
        # then each iteration collects CONCURRENTLY with its sleep — at
        # t=0 (again), 100, 200, 300ms; the 400ms wake fails the duration
        # check. 5 = 1 + 4.
        assert len(series["host_a/value"]) == 5
        assert len(series["host_b/value"]) == 5

    @pytest.mark.asyncio
    async def test_slow_host_times_out(self):
        """A slow host is skipped while a fast host still gets collected."""
        fast = _make_mock_host("fast", delay=0.0)
        slow = _make_mock_host("slow", delay=5.0)  # way longer than interval
        collector = _build_collector([fast, slow])

        with _virtual_time() as vt:
            await vt.drive(
                collector.run(
                    interval=timedelta(milliseconds=200),
                    duration=timedelta(milliseconds=500),
                )
            )

        series = collector.get_series()
        # Exact on the virtual clock. The slow host's mock sleeps its whole
        # `timeout` (the 200ms interval) before returning the timed-out
        # Results, and that sleep rides the SAME timeline, so every tick
        # costs 200ms: the initial collect lands at 200ms, then the loop
        # ticks at 400 and 600ms (the 600ms check fails 600 < 500). 3, not
        # the 5 of the all-fast tests above.
        assert "fast/value" in series
        assert len(series["fast/value"]) == 3

        # Slow host should have no data (timed out every tick)
        assert "slow/value" not in series

    @pytest.mark.asyncio
    async def test_host_error_does_not_crash_loop(self):
        """A host that raises does not prevent other hosts from being collected."""
        good = _make_mock_host("good")
        bad = _make_mock_host("bad", fail=True)
        collector = _build_collector([good, bad])

        with _virtual_time() as vt:
            await vt.drive(
                collector.run(
                    interval=timedelta(milliseconds=100),
                    duration=timedelta(milliseconds=350),
                )
            )

        series = collector.get_series()
        assert "good/value" in series
        # Exact: the bad host's per-tick raise must not eat any good tick
        # (same 5-tick derivation as test_normal_collection).
        assert len(series["good/value"]) == 5
        assert "bad/value" not in series

    @pytest.mark.asyncio
    async def test_duration_stops_loop(self):
        """The loop exits after the specified duration."""
        host = _make_mock_host("host")
        collector = _build_collector([host])

        with _virtual_time() as vt:
            await vt.drive(
                collector.run(
                    interval=timedelta(milliseconds=50),
                    duration=timedelta(milliseconds=200),
                )
            )
        # drive() returned => run() honored its duration on the virtual
        # timeline; the count pins WHERE it stopped: initial + concurrent
        # collects at 0/50/100/150ms — the 200ms wake fails the check.
        assert len(collector.get_series()["host/value"]) == 5

    @pytest.mark.asyncio
    async def test_interval_passed_as_timeout_to_run(self):
        """The collector passes the interval as the timeout to each run call."""
        host = _make_mock_host("host")
        collector = _build_collector([host])

        # Patch asyncio.sleep inside the collector module so the inter-
        # iteration wait completes instantly. Without this the test waits
        # the full 3-second interval between iterations even though
        # duration is only 100ms.
        from unittest.mock import patch

        with patch("otto.monitor.collector.asyncio.sleep", new=AsyncMock()):
            await collector.run(
                interval=timedelta(seconds=3),
                duration=timedelta(milliseconds=100),
            )

        # Inspect the calls to run — each should have timeout=3.0
        for call in host.run.call_args_list:
            if "timeout" in call.kwargs:
                assert call.kwargs["timeout"] == 3.0

    @pytest.mark.serial_timing
    @pytest.mark.asyncio
    async def test_slow_host_does_not_block_fast_host(self):
        """A slow host times out at the interval boundary, not indefinitely.

        With a 200ms interval, the slow host (5s delay) should time out after
        ~200ms, so the entire run should complete in well under 5s.
        """
        fast = _make_mock_host("fast", delay=0.0)
        slow = _make_mock_host("slow", delay=5.0)
        collector = _build_collector([fast, slow])

        start = asyncio.get_running_loop().time()
        await collector.run(
            interval=timedelta(milliseconds=200),
            duration=timedelta(milliseconds=500),
        )
        elapsed = asyncio.get_running_loop().time() - start

        # Should complete in ~0.5-1.0s, not 5+s
        assert elapsed < 2.0, f"Run took {elapsed:.2f}s — slow host may be blocking fast host"


def _batches_from(host: MagicMock) -> list[list[str]]:
    """Extract the command list passed to each recorded ``host.run()`` call.

    Drops the one-time core-count probe (a single ``grep -c ^processor ...``
    call with no ``timeout`` kwarg) so callers only see collection ticks.
    """
    return [
        list(call.args[0])
        for call in host.run.call_args_list
        if not (len(call.args[0]) == 1 and "processor" in call.args[0][0])
    ]


@pytest.mark.asyncio
async def test_tick_cadence_not_slowed_by_collection_time() -> None:
    """Sleep and collection run concurrently: period ~= interval, not interval + collect time.

    interval 0.2s, collection takes 0.15s, duration 0.9s. On the virtual
    clock both the sleep and the mock host's delay ride one timeline, so the
    two verdicts are exact integers, not a range:
      concurrent  -> a tick costs max(0.2, 0.15) = 0.2s: collects finish at
                     0.15, 0.35, 0.55, 0.75, 0.95  -> 5 batches
      serialized  -> a tick costs 0.2 + 0.15 = 0.35s: 0.15, 0.5, 0.85, 1.2
                     -> 4 batches
    A `>= 4` floor inside a real 0.9s window could not tell those apart AND
    collapsed to 1 on a stalled runner (#407); the count below discriminates
    and cannot be moved by scheduling.
    """
    host = _make_mock_host("host", delay=0.15)
    collector = _build_collector([host])

    with _virtual_time() as vt:
        await vt.drive(
            collector.run(
                interval=timedelta(seconds=0.2),
                duration=timedelta(seconds=0.9),
            )
        )

    batches = _batches_from(host)
    assert len(batches) == 5, (
        f"expected 5 concurrent collection ticks (serialized would be 4), "
        f"got {len(batches)}: {batches}"
    )


@pytest.mark.asyncio
async def test_per_parser_interval_buckets_commands():
    """A parser with a faster interval is collected more often than the global tick."""

    class FastParser(MetricParser):
        y_title = "Fast"
        unit = ""
        command = "echo fast"
        chart = "Fast"
        interval = 0.05

        def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint] | None:
            return {self.chart: MetricDataPoint(value=1.0)}

    class SlowParser(MetricParser):
        y_title = "Slow"
        unit = ""
        command = "echo slow"
        chart = "Slow"

        def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint] | None:
            return {self.chart: MetricDataPoint(value=1.0)}

    host = _make_mock_host("host")
    parsers = {FastParser.command: FastParser(), SlowParser.command: SlowParser()}
    collector = MetricCollector(targets=[MonitorTarget(host=host, parsers=parsers)])

    with _virtual_time() as vt:
        await vt.drive(
            collector.run(interval=timedelta(seconds=0.2)),
            cancel_at=timedelta(seconds=0.54),
        )

    batches = _batches_from(host)
    fast_calls = sum(1 for b in batches if b == ["echo fast"])
    slow_calls = sum(1 for b in batches if "echo slow" in b)
    # Exact on the virtual clock (cancel at 0.54s, off any tick boundary):
    # fast 13 = initial@0 + iteration collects at 0, .05...50 (11) + one at
    # .55 — that last exists because drive() settles/advances BEFORE testing
    # cancel_at (see drive's docstring); reordering that check makes this 12.
    # slow 4 = initial@0 + iteration collects at 0, 0.2, 0.4.
    # The old 2x-ratio floor passed even with the fast bucket at HALF rate.
    assert (fast_calls, slow_calls) == (13, 4)
    assert all(b == ["echo fast"] or "echo fast" not in b for b in batches), (
        "fast command must never ride the slow batch"
    )


class _FakeSnmpClient:
    """Duck-typed SnmpClient: returns canned varbind values, records calls."""

    def __init__(self, values: dict[str, float | None]) -> None:
        self._values = values
        self.calls = 0

    async def get(self, oids: list[str]) -> dict[str, float | None]:
        self.calls += 1
        return {oid: self._values.get(oid) for oid in oids}


class TestSnmpCollection:
    """An SNMP target collects via its client, not the host shell."""

    def _make_snmp_target(
        self, name: str, client: _FakeSnmpClient
    ) -> tuple[MagicMock, MonitorTarget]:
        host = MagicMock()
        host.name = name
        host.id = name
        host.log = LogMode.QUIET
        host.run = AsyncMock()  # must NOT be called for an SNMP target
        target = MonitorTarget(
            host=host,
            parsers={},
            snmp=SnmpSource(client=client, oids=[OID_SYS_UPTIME]),  # type: ignore[arg-type]
        )
        return host, target

    @pytest.mark.asyncio
    async def test_snmp_target_populates_series_from_oids(self):
        client = _FakeSnmpClient({OID_SYS_UPTIME: 12345})
        host, target = self._make_snmp_target("zephyr37_fat", client)
        collector = MetricCollector(targets=[target])

        with _virtual_time() as vt:
            await vt.drive(
                collector.run(
                    interval=timedelta(milliseconds=100),
                    duration=timedelta(milliseconds=350),
                )
            )

        series = collector.get_series()
        # sysUpTime (1/100 s) scaled to seconds by the descriptor: 12345 -> 123.45
        assert "zephyr37_fat/Uptime" in series, f"series: {list(series)}"
        assert series["zephyr37_fat/Uptime"][0].value == 123.45
        # Exact: initial + concurrent collects at 0/100/200/300ms. NB each
        # SNMP tick still rides a REAL 100ms asyncio.wait_for inside the
        # collector (wall clock, not virtualized): a >100ms stall there
        # drops a tick and reds this count — accepted, it names a real stall.
        assert client.calls == 5
        host.run.assert_not_called()  # no shell, no core-count probe

    @pytest.mark.asyncio
    async def test_snmp_and_shell_targets_coexist(self):
        client = _FakeSnmpClient({OID_SYS_UPTIME: 100})
        _, snmp_target = self._make_snmp_target("zephyr37_fat", client)
        shell_host = _make_mock_host("test1")
        shell_target = MonitorTarget(host=shell_host, parsers={StubParser.command: StubParser()})
        collector = MetricCollector(targets=[snmp_target, shell_target])

        await collector.run(
            interval=timedelta(milliseconds=100),
            duration=timedelta(milliseconds=350),
        )

        series = collector.get_series()
        assert "zephyr37_fat/Uptime" in series  # SNMP path
        assert "test1/value" in series  # shell path, unaffected


@pytest.mark.asyncio
async def test_shell_collection_is_silenced_per_call_not_on_the_host():
    from otto.host.element import Element
    from otto.host.login_proxy import Cred
    from otto.host.unix_host import UnixHost
    from otto.monitor.factory import build_monitor_collector

    host = UnixHost(
        ip="10.0.0.1",
        element=Element("box"),
        creds=[Cred(login="a", password="b")],
        log=LogMode.NORMAL,
    )
    host.run = AsyncMock(return_value=None)  # type: ignore[method-assign]
    collector = build_monitor_collector([host])
    await collector._collect_one(collector._targets[0], 1.0)
    assert host.run.await_args.kwargs["log"] is LogMode.NEVER
    assert host.log is LogMode.NORMAL


# ── Real parsers through a live run ──────────────────────────────────────────

# Synthetic command outputs matching the parsers' expected formats.
_FREE_OUTPUT = (
    "              total        used        free      shared  buff/cache   available\n"
    "Mem:    16000000000  10000000000   3000000000           0  3000000000  6000000000\n"
    "Swap:    2048000000           0  2048000000"
)
_LOADAVG_OUTPUT = "0.52 0.58 0.59 1/432 12345"
_CPUINFO_OUTPUT = "4"


def _make_monitor_host(name: str = "router1") -> MagicMock:
    """Return a mock UnixHost whose run returns canned metric output.

    The mock boundary is at the host I/O layer — the collector, parsers,
    and storage all run for real.
    """
    host = MagicMock(spec=UnixHost)
    host.name = name
    host.id = name
    host.log = LogMode.NORMAL

    responses: dict[str, CommandResult] = {
        "grep -c ^processor /proc/cpuinfo": CommandResult(
            Status.Success,
            value=_CPUINFO_OUTPUT,
            command="grep -c ^processor /proc/cpuinfo",
            retcode=0,
        ),
        "free -b": CommandResult(
            Status.Success,
            value=_FREE_OUTPUT,
            command="free -b",
            retcode=0,
        ),
        "cat /proc/loadavg": CommandResult(
            Status.Success,
            value=_LOADAVG_OUTPUT,
            command="cat /proc/loadavg",
            retcode=0,
        ),
    }

    async def fake_run_cmds(
        cmds: list[str] | str, timeout: float | None = None, log: object = None
    ) -> Results:
        if isinstance(cmds, str):
            cmds = [cmds]
        results = []
        for cmd in cmds:
            if cmd in responses:
                results.append(responses[cmd])
            else:
                results.append(CommandResult(Status.Failed, value="", command=cmd, retcode=1))
        return Results.collect(results)

    host.run = AsyncMock(side_effect=fake_run_cmds)
    return host


class TestCollectorLiveRun:
    """Tests that let MetricCollector.run() execute for real with mock hosts.

    Mock boundary: host.run (I/O layer).
    Exercises: parser selection, output parsing, series storage, DB writes.
    """

    @pytest.mark.asyncio
    async def test_single_cycle_parses_metrics(self):
        host = _make_monitor_host("router1")
        collector = MetricCollector(
            hosts=[host],
            parsers=[MemParser(), LoadParser()],
        )

        await collector.run(
            interval=timedelta(seconds=1),
            duration=timedelta(seconds=0),
        )

        series = collector.get_series()
        # MemParser produces "Memory Usage" keyed by chart name
        assert "router1/Memory Usage" in series
        mem_pt = series["router1/Memory Usage"][0]
        assert abs(mem_pt.value - 62.5) < 0.1  # 10B / 16B = 62.5%
        assert mem_pt.meta is not None

        # LoadParser produces three load average series
        assert "router1/Load (1m)" in series
        assert "router1/Load (5m)" in series
        assert "router1/Load (15m)" in series
        load_1m = series["router1/Load (1m)"][0].value
        assert abs(load_1m - 0.52) < 0.01

    @pytest.mark.asyncio
    async def test_collection_stores_to_sqlite(self, tmp_path):
        host = _make_monitor_host("router1")
        db_file = tmp_path / "test_metrics.db"
        db = MetricDB(
            str(db_file),
            new_frame(label=None, note=None),
            lab_json="{}",
            meta_json="{}",
        )
        collector = MetricCollector(
            hosts=[host],
            parsers=[MemParser(), LoadParser()],
            db=db,
        )

        # The Tier-0.7 seam: run() refuses an unopened DB (no lazy in-task
        # open), so this drives the same path production does.
        await collector.init_db()
        await collector.run(
            interval=timedelta(seconds=1),
            duration=timedelta(seconds=0),
        )
        await collector.close_db()

        assert db_file.exists()
        with contextlib.closing(sqlite3.connect(str(db_file))) as conn, conn:
            rows = conn.execute("SELECT host, label, value FROM metrics").fetchall()
        # 1 Memory Usage + 3 Load averages = 4 rows
        assert len(rows) >= 4
        labels = {row[1] for row in rows}
        assert "Memory Usage" in labels
        assert "Load (1m)" in labels

    @pytest.mark.asyncio
    async def test_multiple_hosts_collected(self):
        host1 = _make_monitor_host("host1")
        host2 = _make_monitor_host("host2")
        collector = MetricCollector(
            hosts=[host1, host2],
            parsers=[LoadParser()],
        )

        await collector.run(
            interval=timedelta(seconds=1),
            duration=timedelta(seconds=0),
        )

        series = collector.get_series()
        assert "host1/Load (1m)" in series
        assert "host2/Load (1m)" in series

    @pytest.mark.asyncio
    async def test_failed_command_does_not_crash_collector(self):
        host = _make_monitor_host("router1")
        # Override host.run to always return failures with empty output
        host.run = AsyncMock(
            return_value=Results.collect(
                [
                    CommandResult(Status.Failed, value="", command="free -b", retcode=1),
                    CommandResult(Status.Failed, value="", command="cat /proc/loadavg", retcode=1),
                ]
            )
        )
        collector = MetricCollector(
            hosts=[host],
            parsers=[MemParser(), LoadParser()],
        )

        await collector.run(
            interval=timedelta(seconds=1),
            duration=timedelta(seconds=0),
        )

        # Parsers return {} for empty/unparseable output — no series created
        assert collector.get_series() == {}
