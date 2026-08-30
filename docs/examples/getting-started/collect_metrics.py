"""Poll the BusyBox guest for a few monitor ticks and report the series that landed."""

# doc: begin collect-metrics
import asyncio
from datetime import timedelta
from pathlib import Path

import otto
from otto.monitor.factory import build_monitor_collector

HERE = Path(__file__).resolve().parent


async def main() -> None:
    """Collect from bb1350_qemu with whatever parsers the registrations resolved for it."""
    async with otto.open_context(lab="busybox", search_paths=[HERE / "lab_data"]):
        host = otto.get_host("bb1350_qemu")
        collector = build_monitor_collector(hosts=[host])
        try:
            await collector.run(interval=timedelta(seconds=5), duration=timedelta(seconds=12))
        finally:
            await collector.close()
        for series, points in sorted(collector.get_series().items()):
            print(f"{series}\t{len(points)} samples")


asyncio.run(main())
# doc: end collect-metrics
