"""Monitor dut while the tools run, and check the project's own metric was charted."""

import asyncio

from quickstart_shell.tools import run_tool

from otto.lab import get_host
from otto.suite import MonitorHandle


class TestMonitoring:
    """Monitoring a host while the staged tools run."""

    async def test_install_size_is_charted(self, monitor: MonitorHandle) -> None:
        """The project's InstallSize parser produced samples for dut."""
        await monitor.start(hosts=[get_host("dut")], interval=1.0)
        await monitor.event("tools running", color="#2ca02c")
        for _ in range(3):
            await run_tool("shout", "monitored")
            await asyncio.sleep(1)
        await monitor.stop()
        assert monitor.results()["dut/Install size"]
