"""Per-host parser scoping through the full registry -> factory -> collector path.

Two mock shell hosts; UptimeParser registered for host A only. Proves the
executed third-party extension path (registration -> get_host_parsers ->
build_monitor_collector -> series -> /api/meta) AND its scoping: the host
that did NOT register keeps exactly the defaults.
"""

import asyncio
import contextlib
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.examples.monitor import UptimeParser
from otto.logger.mode import LogMode
from otto.monitor.factory import build_monitor_collector
from otto.monitor.parsers import DEFAULT_PARSERS, register_host_parsers
from otto.result import CommandResult, Results
from otto.utils import Status

_CANNED = {
    "cat /proc/uptime": "12345.67 23456.78\n",
    "free -b": (
        "              total        used        free\n"
        "Mem:     16000000000  4000000000  8000000000\n"
        "Swap:              0           0           0\n"
    ),
}


def _make_host(name: str, host_id: str) -> MagicMock:
    host = MagicMock()
    host.name = name
    host.id = host_id
    host.snmp = None
    host.log = LogMode.QUIET

    async def _run(cmds, timeout=None):
        return Results.collect(
            [
                CommandResult(Status.Success, value=_CANNED.get(cmd, ""), command=cmd, retcode=0)
                for cmd in cmds
            ]
        )

    host.run = AsyncMock(side_effect=_run)
    return host


class TestPerHostScoping:
    def test_uptime_parser_parses(self):
        from otto.monitor.parsers import ParseContext

        points = UptimeParser().parse("12345.67 23456.78\n", ctx=ParseContext())
        assert set(points) == {"Uptime"}
        assert points["Uptime"].value == 12345.67

    @pytest.mark.asyncio
    async def test_registered_host_gets_uptime_unregistered_does_not(self):
        register_host_parsers(
            "scoping-host-a",
            {**DEFAULT_PARSERS, UptimeParser().command: UptimeParser()},
        )
        host_a = _make_host("scope_a", "scoping-host-a")
        host_b = _make_host("scope_b", "scoping-host-b")
        collector = build_monitor_collector([host_a, host_b])
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                collector.run(interval=timedelta(seconds=0.05), duration=timedelta(seconds=0.2)),
                timeout=5,
            )
        series = set(collector._store.series)
        assert "scope_a/Uptime" in series          # registered host gets the custom metric
        assert "scope_b/Uptime" not in series      # unregistered host does NOT
        assert "scope_b/Memory Usage" in series    # ...and keeps the untouched defaults
        meta = collector.get_meta_model()
        assert any(m.chart == "Uptime" for m in meta.metrics)  # /api/meta grew the chart
