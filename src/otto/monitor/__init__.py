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

        # spawn_collection opens the archive BEFORE the task exists — an
        # in-task open races cancellation into a partial DB.
        collection = await collector.spawn_collection(timedelta(seconds=5))
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

Every name is exported lazily (PEP 562): ``from otto.monitor import
MetricCollector`` imports ``otto.monitor.collector`` and what it needs, and
only a caller that names ``MonitorServer`` pays for fastapi and uvicorn. The
resolver does not write a resolved name back into the module dict; see
``otto.config``'s ``__dir__`` for why.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .collector import MetricCollector as MetricCollector
    from .events import MonitorEvent as MonitorEvent
    from .factory import build_monitor_collector as build_monitor_collector
    from .parsers import DEFAULT_PARSERS as DEFAULT_PARSERS
    from .parsers import MetricParser as MetricParser
    from .server import MonitorServer as MonitorServer

# name -> the module that defines it, imported on first access by __getattr__.
_LAZY_ATTRS: dict[str, str] = {
    "MetricCollector": "otto.monitor.collector",
    "MonitorEvent": "otto.monitor.events",
    "build_monitor_collector": "otto.monitor.factory",
    "DEFAULT_PARSERS": "otto.monitor.parsers",
    "MetricParser": "otto.monitor.parsers",
    "MonitorServer": "otto.monitor.server",
}


def __getattr__(name: str) -> object:
    """PEP 562 lazy resolver for otto.monitor's public exports."""
    import importlib

    if name in _LAZY_ATTRS:
        return getattr(importlib.import_module(_LAZY_ATTRS[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include the lazy exports in dir()/tab-completion; the module dict holds none of them."""
    return sorted(set(globals()) | set(_LAZY_ATTRS))


__all__ = [
    "DEFAULT_PARSERS",
    "MetricCollector",
    "MetricParser",
    "MonitorEvent",
    "MonitorServer",
    "build_monitor_collector",
]
