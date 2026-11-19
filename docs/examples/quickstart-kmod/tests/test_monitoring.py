"""Monitor the lab's hosts while lines are written to the module, and check the project's metric."""

import asyncio

import pytest
from quickstart_kmod.wordcount import count_words

from otto.host import UnixHost
from otto.lab import all_hosts
from otto.suite import MonitorHandle


@pytest.mark.ensure("installed")
class TestMonitoring:
    """Monitoring the lab's hosts while the module counts."""

    async def test_module_size_is_charted(self, monitor: MonitorHandle) -> None:
        """The project's ModuleSize parser produced samples for dut."""
        await monitor.start(hosts=[h for h in all_hosts() if isinstance(h, UnixHost)], interval=1.0)
        await monitor.event("lines written", color="#2ca02c")
        for line in ["one", "one two", "one two three"]:
            await count_words(line)
            await asyncio.sleep(1)
        await monitor.stop()
        assert monitor.results()["dut/Module size"]
