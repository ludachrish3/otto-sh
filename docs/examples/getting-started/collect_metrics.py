"""Poll the BusyBox guest for a few monitor ticks and report the series that landed."""

# doc: begin collect-metrics
import asyncio
from datetime import timedelta

import otto
from otto.monitor.factory import build_monitor_collector


async def main() -> None:
    """Collect from bb1350-qemu with whatever parsers the registrations resolved for it."""
    async with otto.open_context(lab="busybox"):
        host = otto.get_host("bb1350-qemu")
        collector = build_monitor_collector(hosts=[host])
        try:
            await collector.run(interval=timedelta(seconds=5), duration=timedelta(seconds=12))
        finally:
            await collector.close()
        for series, points in sorted(collector.get_series().items()):
            print(f"{series}\t{len(points)} samples")


asyncio.run(main())
# doc: end collect-metrics
