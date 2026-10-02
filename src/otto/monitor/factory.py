"""Shared monitor-collector factory.

Used by both ``otto monitor`` (live dashboard) and ``otto test --monitor``
(session-scoped collection during a test run).  Centralising this here keeps
both call sites consistent — same parser-registry lookup, same target
construction.
"""

import copy
from collections.abc import Awaitable, Callable, Sequence
from typing import cast

from ..host.remote_host import RemoteHost
from ..host.unix_host import UnixHost
from ..models.monitor import TunnelRecord
from .collector import MetricCollector, MonitorTarget
from .db import MetricDB
from .parsers import MetricParser, get_host_parsers
from .snmp import SnmpClient, SnmpSource, SnmpVersion, expand_oid_bundles


def is_monitorable(host: RemoteHost) -> bool:
    """Whether otto can sample *host*: over a shell (a Unix host) or over SNMP.

    The one predicate every monitor producer selects with. An embedded RTOS
    console cannot share its single session with a metrics poller, so a host
    that is not Unix is sampled only through a declared ``snmp`` block.
    """
    return isinstance(host, UnixHost) or host.snmp is not None


def monitorable(hosts: Sequence[RemoteHost]) -> list[RemoteHost]:
    """Return the hosts in *hosts* that :func:`is_monitorable` accepts, order kept."""
    return [host for host in hosts if is_monitorable(host)]


def build_monitor_collector(
    hosts: Sequence[RemoteHost],
    *,
    parsers: list[MetricParser] | None = None,
    db: MetricDB | None = None,
    tunnel_source: Callable[[], Awaitable[list[TunnelRecord]]] | None = None,
) -> MetricCollector:
    """Build a :class:`~otto.monitor.collector.MetricCollector` over *hosts*.

    Creates one :class:`~otto.monitor.collector.MonitorTarget` per host and
    chooses each host's collection mode. Collection is silenced per call by the
    collector, never by changing ``host.log``, so a host shared with tests keeps
    its own logging:

    - a host with an ``snmp`` block is polled over SNMP — its
      :class:`~otto.host.options.SnmpOptions` becomes a live
      :class:`~otto.monitor.snmp.SnmpClient` (address defaulting to the host's
      own ``ip``) plus the OID list to GET;
    - otherwise it is polled by running shell commands, with its parser set
      resolved via :func:`~otto.monitor.parsers.get_host_parsers` so per-host customisations
      registered by init modules are honoured (or replaced by *parsers*).

    Args:
        hosts: Hosts to sample on each tick.
        parsers: Optional parsers that replace the registered set for every
            shell-polled host (like the registered set, each host gets its own
            copies, because parsers keep per-host state); ``None`` uses each
            host's registered parsers.
            SNMP targets are unaffected.
        db: Optional session-bound :class:`~otto.monitor.db.MetricDB` for persistence
            (unopened); ``None`` means in-memory. The frame (session identity) is
            the caller's job — this factory stays session-blind, same as the
            collector it builds.
        tunnel_source: Optional full-lab tunnel discovery callable for the
            collector's tunnel loop (spec 2026-07-16). The CLI composes this
            over the WHOLE lab — tunnels may traverse hosts that metric
            collection was never pointed at.
    """
    targets: list[MonitorTarget] = []
    for host in hosts:
        snmp = host.snmp
        if snmp is not None:
            client = SnmpClient(
                address=host.address_for(snmp.address or host.ip),
                port=snmp.port,
                community=snmp.community,
                version=cast("SnmpVersion", snmp.version),
            )
            targets.append(
                MonitorTarget(
                    host=host,
                    parsers={},
                    snmp=SnmpSource(client=client, oids=expand_oid_bundles(snmp.oids)),
                )
            )
        else:
            shell_parsers = (
                {p.command: copy.deepcopy(p) for p in parsers}
                if parsers is not None
                else get_host_parsers(host.id)
            )
            targets.append(MonitorTarget(host=host, parsers=shell_parsers))

    return MetricCollector(
        targets=targets,
        db=db,
        tunnel_source=tunnel_source,
    )
