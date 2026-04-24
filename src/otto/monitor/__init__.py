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

from .collector import MetricCollector
from .events import MonitorEvent
from .parsers import DEFAULT_PARSERS, MetricParser
from .server import MonitorServer

__all__ = [
    'MetricCollector',
    'MetricParser',
    'MonitorEvent',
    'MonitorServer',
    'DEFAULT_PARSERS',
]
