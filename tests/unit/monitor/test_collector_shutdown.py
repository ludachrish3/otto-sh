"""Regression test for the `otto monitor` Ctrl+C shutdown traceback.

Before the fix, `_run_monitor` only awaited `collector.close_db()` on
shutdown, so the `bash` subprocesses spawned lazily by `LocalSession` for
local metric collection survived past the event loop's close.  Their
`BaseSubprocessTransport.__del__` then ran `loop.call_soon(...)` on a
closed loop and printed a `RuntimeError: Event loop is closed` traceback.

These tests exercise the real shutdown path against a real `LocalHost`
and assert that after `collector.close()` the underlying `LocalSession`
has released its subprocess (so no transport will be GC'd later).
"""

import asyncio
from datetime import timedelta

import pytest

from otto.host.localHost import LocalHost
from otto.monitor.collector import MetricCollector, MonitorTarget
from otto.monitor.parsers import MetricDataPoint, MetricParser


class EchoParser(MetricParser):
    """Trivial parser that runs a real shell command via LocalSession."""

    chart = 'Test'
    y_title = 'Value'
    unit = ''
    command = 'echo 42'

    def parse(self, output: str) -> dict[str, MetricDataPoint] | None:
        try:
            return {'value': MetricDataPoint(float(output.strip()))}
        except ValueError:
            return None


def _local_session(host: LocalHost):
    """Reach into the LocalHost's SessionManager to inspect the bash session."""
    return host._session_mgr._session


class TestCollectorShutdown:

    @pytest.mark.asyncio
    async def test_close_releases_local_session_subprocess(self):
        """collector.close() must terminate the bash subprocess LocalSession
        opened during collection — otherwise its transport is GC'd after the
        event loop closes, raising "Event loop is closed" from __del__.
        """
        host = LocalHost()
        host.log = False
        target = MonitorTarget(host=host, parsers={EchoParser.command: EchoParser()})
        collector = MetricCollector(targets=[target])

        task = asyncio.create_task(
            collector.run(
                interval=timedelta(milliseconds=100),
                duration=timedelta(milliseconds=250),
            )
        )
        await task

        session = _local_session(host)
        assert session is not None, 'LocalSession was never opened during collection'
        assert session._process is not None, (
            'Expected an active bash subprocess before close()'
        )

        await collector.close()

        assert session._process is None, (
            'LocalSession._process should be None after collector.close() — '
            'otherwise its subprocess transport will be GC\'d after loop close '
            'and raise "Event loop is closed".'
        )

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self):
        """Calling close() twice must not raise (mirrors cancel/finally paths)."""
        host = LocalHost()
        host.log = False
        target = MonitorTarget(host=host, parsers={EchoParser.command: EchoParser()})
        collector = MetricCollector(targets=[target])

        await collector.run(
            interval=timedelta(milliseconds=100),
            duration=timedelta(milliseconds=150),
        )
        await collector.close()
        await collector.close()

    @pytest.mark.asyncio
    async def test_dead_session_replaced_closes_old_process(self):
        """When _ensure_session() replaces a dead session, it must close the old one.

        Before the fix, _ensure_session() created a new session but left the
        old session's subprocess alive.  On shutdown, only the current session
        was closed via close_all(), leaving the orphaned session's
        BaseSubprocessTransport to be GC'd after the loop closed — triggering
        "RuntimeError: Event loop is closed" from __del__.
        """
        host = LocalHost()
        host.log = False
        target = MonitorTarget(host=host, parsers={EchoParser.command: EchoParser()})
        collector = MetricCollector(targets=[target])

        # Run briefly to establish a session
        await collector.run(
            interval=timedelta(milliseconds=100),
            duration=timedelta(milliseconds=150),
        )

        old_session = _local_session(host)
        assert old_session is not None, 'Session was never created'
        assert old_session._process is not None, 'Session has no subprocess'

        # Simulate session death (what _recover_session does on failure)
        old_session._alive = False

        # Run again — _ensure_session() will create a new session
        await collector.run(
            interval=timedelta(milliseconds=100),
            duration=timedelta(milliseconds=150),
        )

        new_session = _local_session(host)
        assert new_session is not old_session, (
            '_ensure_session should have created a new session'
        )

        # The critical assertion: old session's subprocess must be closed
        assert old_session._process is None, (
            'Old session._process should be None after replacement — '
            'otherwise its subprocess transport will be GC\'d after loop close '
            'and raise "Event loop is closed".'
        )

        await collector.close()
