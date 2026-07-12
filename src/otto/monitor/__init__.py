"""
otto.monitor — Interactive performance monitoring dashboard.

Quick start (live mode, persisting to a session-scoped SQLite archive).
``collector.run()`` and ``server.serve()`` both run until cancelled/stopped,
so drive them concurrently — this mirrors what ``otto.cli.monitor`` itself
does (see its ``_run_monitor``):
    import asyncio
    from datetime import datetime, timedelta, timezone

    from otto.monitor import MonitorServer, build_monitor_collector
    from otto.monitor.db import MetricDB
    from otto.monitor.session import new_frame

    async def main(host):
        # `host` is an already-configured otto.host.UnixHost.
        #
        # One live run == one session. The frame carries its identity; the
        # collector itself stays session-blind, so framing happens out here.
        # lab_json/meta_json are knowable up front; the series-label -> chart
        # map is not (it accrues as points arrive), so the collector writes
        # it itself.
        db = MetricDB('metrics.db', new_frame(label='fan fix', note=None),
                      lab_json='{}', meta_json='{}')
        collector = build_monitor_collector([host], db=db)
        server = MonitorServer(collector, host='0.0.0.0', port=8080)

        collection = asyncio.create_task(collector.run(interval=timedelta(seconds=5)))
        try:
            print(f'Dashboard: {server.url}')
            await server.serve()  # blocks until server.stop() is called
        finally:
            collection.cancel()
            await asyncio.gather(collection, return_exceptions=True)
            # An unstamped end reads as "crashed" to the review shell.
            await db.finalize(datetime.now(tz=timezone.utc))
            await collector.close()

    asyncio.run(main(host))

Omit ``db=`` for an in-memory collector (no persistence).

Review mode (serves a previously saved export; no live collection — see
``otto.cli.monitor`` for the ``otto monitor <source>`` CLI this mirrors):
    import asyncio

    from otto.monitor import MetricCollector, MonitorServer
    from otto.monitor.export import build_db_export

    async def main():
        export = build_db_export('metrics.db')
        collector = MetricCollector(targets=[])
        server = MonitorServer(collector, mode='review', document=export, source_name='metrics.db')
        await server.serve()  # blocks until server.stop() is called

    asyncio.run(main())
"""

from typing import TYPE_CHECKING

from .collector import MetricCollector
from .events import MonitorEvent
from .factory import build_monitor_collector
from .parsers import DEFAULT_PARSERS, MetricParser

if TYPE_CHECKING:
    from .server import MonitorServer

__all__ = [
    "DEFAULT_PARSERS",
    "MetricCollector",
    "MetricParser",
    "MonitorEvent",
    "MonitorServer",
    "build_monitor_collector",
]


def __getattr__(name: str) -> object:
    """Lazily resolve MonitorServer to keep importing otto.monitor import-light.

    Importing otto.monitor (e.g. via otto.models -> monitor.collector) must not
    pull in fastapi/uvicorn; the server is resolved only on attribute access.
    """
    if name == "MonitorServer":
        from .server import MonitorServer

        return MonitorServer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
