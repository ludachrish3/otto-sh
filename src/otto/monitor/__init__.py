"""
otto.monitor — Interactive performance monitoring dashboard.

Quick start (live mode):
    from datetime import timedelta
    from otto.monitor import MetricCollector, MonitorServer

    collector = MetricCollector(host, db_path='metrics.db')
    collector.start(interval=timedelta(seconds=5))

    server = MonitorServer(collector, host='0.0.0.0', port=8080)
    server.start()
    print(f'Dashboard: {server.url}')

Historical mode:
    collector = MetricCollector.from_sqlite('metrics.db')
    server = MonitorServer(collector)
    server.start()
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
    """Lazily resolve MonitorServer so importing otto.monitor (e.g. via
    otto.models -> monitor.collector) does not pull in fastapi/uvicorn."""
    if name == "MonitorServer":
        from .server import MonitorServer

        return MonitorServer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
