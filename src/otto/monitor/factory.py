"""Shared monitor-collector factory.

Used by both ``otto monitor`` (live dashboard) and ``otto test --monitor``
(session-scoped collection during a test run).  Centralising this here keeps
both call sites consistent — same parser-registry lookup, same per-host log
silencing, same target construction.
"""

from pathlib import Path
from typing import Optional

from .collector import MetricCollector, MonitorTarget
from .parsers import get_host_parsers
from ..host.unixHost import UnixHost


def build_monitor_collector(
    hosts: list[UnixHost],
    db_path: Optional[Path] = None,
) -> MetricCollector:
    """Build a :class:`MetricCollector` over *hosts* with per-host parsers.

    Silences host logging (collection is chatty), resolves each host's parser
    set via :func:`get_host_parsers` so per-host customisations registered
    by init modules are honoured, and pairs them up as
    :class:`MonitorTarget` objects.

    Args:
        hosts: Hosts to sample on each tick.
        db_path: Optional SQLite file for persistence; ``None`` means in-memory.
    """
    for host in hosts:
        host.log = False

    targets = [MonitorTarget(host=h, parsers=get_host_parsers(h.id)) for h in hosts]

    return MetricCollector(
        targets=targets,
        db_path=str(db_path) if db_path else None,
    )
