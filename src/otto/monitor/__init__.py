"""
otto.monitor — Interactive performance monitoring dashboard.

``otto monitor``, ``otto test --monitor`` and the ``monitor`` test fixture
all drive this one library; the CLI leaf modules only parse, call it and translate its
errors into exit codes.

Quick start (live mode, persisting to a session-scoped SQLite archive).
:class:`MonitorSession` owns the session's identity, lab snapshot, collector
and archive; ``async with session:`` opens the archive before any collection
task exists and, on exit, stamps the end and finalizes it. ``server.serve()``
runs until stopped, so the collection task runs beside it:
    import asyncio

    from otto.monitor import MonitorServer, MonitorSession

    async def main(host):
        # `host` is an already-configured otto.host.UnixHost.
        session = MonitorSession.build(
            [host], interval=5, db_path='metrics.db', label='fan fix', owns_hosts=True
        )
        server = MonitorServer(
            session.collector, host='0.0.0.0', port=8080,
            frame=session.frame, lab=session.lab,
        )
        async with session:
            task = session.spawn()
            try:
                print(f'Dashboard: {server.url}')
                await server.serve()  # blocks until server.stop() is called
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main(host))

Omit ``db_path=`` to keep the data in memory (no persistence).

:func:`run_live` is the one-call equivalent of ``otto monitor --live``: it
selects hosts from the active lab (so it needs one), builds the session,
serves the dashboard and returns a :class:`LiveReport`.

Review mode (serves a previously saved ``.db`` or ``.json`` export; no live
collection — the library behind ``otto monitor <source>``):
    import asyncio
    from pathlib import Path

    from otto.monitor import serve_review

    asyncio.run(serve_review(Path('metrics.db'), repos=[]))  # blocks until stopped

Every name is exported lazily (PEP 562): ``from otto.monitor import
MetricCollector`` imports ``otto.monitor.collector`` and what it needs, and
only a caller that names ``MonitorServer`` pays for fastapi and uvicorn. The
resolver does not write a resolved name back into the module dict; see
``otto.config``'s ``__dir__`` for why.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .collector import MetricCollector as MetricCollector
    from .errors import MonitorInputError as MonitorInputError
    from .errors import MonitorTlsError as MonitorTlsError
    from .errors import NoMonitorableHostsError as NoMonitorableHostsError
    from .errors import ReviewSourceError as ReviewSourceError
    from .events import MonitorEvent as MonitorEvent
    from .factory import build_monitor_collector as build_monitor_collector
    from .factory import is_monitorable as is_monitorable
    from .factory import monitorable as monitorable
    from .live import LiveReport as LiveReport
    from .live import run_live as run_live
    from .live import select_monitor_hosts as select_monitor_hosts
    from .parsers import DEFAULT_PARSERS as DEFAULT_PARSERS
    from .parsers import MetricParser as MetricParser
    from .review import load_review_document as load_review_document
    from .review import serve_review as serve_review
    from .server import MonitorServer as MonitorServer
    from .session import MonitorSession as MonitorSession
    from .tls import resolve_monitor_tls as resolve_monitor_tls

# name -> the module that defines it, imported on first access by __getattr__.
_LAZY_ATTRS: dict[str, str] = {
    "MetricCollector": "otto.monitor.collector",
    "MonitorInputError": "otto.monitor.errors",
    "MonitorTlsError": "otto.monitor.errors",
    "NoMonitorableHostsError": "otto.monitor.errors",
    "ReviewSourceError": "otto.monitor.errors",
    "MonitorEvent": "otto.monitor.events",
    "build_monitor_collector": "otto.monitor.factory",
    "is_monitorable": "otto.monitor.factory",
    "monitorable": "otto.monitor.factory",
    # These three resolve into otto.monitor.live, the lab-aware nested tach
    # module. tach does not see a string-keyed lazy import, so engine code
    # (otto.monitor.*, apart from live itself) must never import these names.
    "LiveReport": "otto.monitor.live",
    "run_live": "otto.monitor.live",
    "select_monitor_hosts": "otto.monitor.live",
    "DEFAULT_PARSERS": "otto.monitor.parsers",
    "MetricParser": "otto.monitor.parsers",
    "load_review_document": "otto.monitor.review",
    "serve_review": "otto.monitor.review",
    "MonitorServer": "otto.monitor.server",
    "MonitorSession": "otto.monitor.session",
    "resolve_monitor_tls": "otto.monitor.tls",
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
    "LiveReport",
    "MetricCollector",
    "MetricParser",
    "MonitorEvent",
    "MonitorInputError",
    "MonitorServer",
    "MonitorSession",
    "MonitorTlsError",
    "NoMonitorableHostsError",
    "ReviewSourceError",
    "build_monitor_collector",
    "is_monitorable",
    "load_review_document",
    "monitorable",
    "resolve_monitor_tls",
    "run_live",
    "select_monitor_hosts",
    "serve_review",
]
