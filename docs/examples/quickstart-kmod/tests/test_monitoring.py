"""Monitor dut while lines are written to the module, and check the project's metric."""

import asyncio

import pytest
from quickstart_kmod.wordcount import count_words

from otto.lab import get_host
from otto.suite import MonitorHandle


@pytest.mark.ensure("installed")
class TestMonitoring:
    """Monitoring a host while the module counts."""

    async def test_module_size_is_charted(self, monitor: MonitorHandle) -> None:
        """The project's ModuleSize parser produced samples for dut."""
        await monitor.start(hosts=[get_host("dut")], interval=1.0)
        await monitor.event("lines written", color="#2ca02c")
        for line in ["one", "one two", "one two three"]:
            await count_words(line)
            await asyncio.sleep(1)
        await monitor.stop()
        assert monitor.results()["dut/Module size"]
