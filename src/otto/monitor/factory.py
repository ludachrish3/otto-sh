"""Shared monitor-collector factory.

Used by both ``otto monitor`` (live dashboard) and ``otto test --monitor``
(session-scoped collection during a test run).  Centralising this here keeps
both call sites consistent — same parser-registry lookup, same per-host log
silencing, same target construction.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import cast

from ..host.remote_host import RemoteHost
from .collector import MetricCollector, MonitorTarget
from .parsers import get_host_parsers
from .snmp import SnmpClient, SnmpSource, SnmpVersion


def build_monitor_collector(
    hosts: Sequence[RemoteHost],
    db_path: Path | None = None,
) -> MetricCollector:
    """Build a :class:`~otto.monitor.collector.MetricCollector` over *hosts*, one :class:`~otto.monitor.collector.MonitorTarget` each.

    Silences host logging (collection is chatty) and chooses each host's
    collection mode:

    - a host with an ``snmp`` block is polled over SNMP — its
      :class:`~otto.host.options.SnmpOptions` becomes a live
      :class:`~otto.monitor.snmp.SnmpClient` (address defaulting to the host's
      own ``ip``) plus the OID list to GET;
    - otherwise it is polled by running shell commands, with its parser set
      resolved via :func:`~otto.monitor.parsers.get_host_parsers` so per-host customisations
      registered by init modules are honoured.

    Args:
        hosts: Hosts to sample on each tick.
        db_path: Optional SQLite file for persistence; ``None`` means in-memory.
    """
    targets: list[MonitorTarget] = []
    for host in hosts:
        host.log = False
        snmp = host.snmp
        if snmp is not None:
            client = SnmpClient(
                address=host.address_for(snmp.address or host.ip),
                port=snmp.port,
                community=snmp.community,
                version=cast(SnmpVersion, snmp.version),
            )
            targets.append(MonitorTarget(
                host=host, parsers={}, snmp=SnmpSource(client=client, oids=list(snmp.oids)),
            ))
        else:
            targets.append(MonitorTarget(host=host, parsers=get_host_parsers(host.id)))

    return MetricCollector(
        targets=targets,
        db_path=str(db_path) if db_path else None,
    )
