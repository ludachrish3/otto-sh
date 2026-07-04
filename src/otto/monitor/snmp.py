"""
otto SNMP manager — general SNMP v2c polling for performance monitoring.

This is the reusable core that lets otto monitor *any* SNMP speaker — network
gear, a Linux box running net-snmp, or a Zephyr device running otto's test-bed
agent — over a separate channel from command execution. It is **not**
embedded-only: a Unix host may also be monitored via SNMP.

Two layers, mirroring the shell-monitoring split in :mod:`otto.monitor.parsers`
(where :class:`~otto.monitor.parsers.MetricParser` owns presentation and lab
data owns "what command to run"):

- **Acquisition** — :class:`SnmpClient`: a thin async pysnmp v2c GET wrapper.
  Lab data (the host's ``snmp`` block) supplies only connection params and the
  bare list of OIDs to poll. No presentation fields ever live in lab data.
- **Presentation** — :class:`SnmpMetric` + the descriptor registry: maps each
  OID to how it is charted (label, chart group, unit, tab) and how its raw
  varbind is interpreted (``scale``). This is the SNMP analog of
  ``MetricParser``; graphing decisions live here. Built-in descriptors cover a
  standard OID set; private/device OIDs register a descriptor from an init
  module via :func:`register_snmp_metric`. An OID with no registered descriptor
  falls back to default styling (:func:`resolve_snmp_metric`) so a host can add
  a bare OID with zero code and still get a chart.

The pysnmp dependency is imported lazily inside :meth:`SnmpClient.get` so this
module imports cleanly without it, and unit tests can mock at the ``get``
boundary rather than against pysnmp internals.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, SupportsInt

from pydantic import ConfigDict

from ..models.base import OttoModel
from ..registry import Registry, caller_module
from .parsers import MetricDataPoint, human_readable
from .rates import RateTracker

logger = logging.getLogger("otto")


# ---------------------------------------------------------------------------
# otto enterprise OID subtree
# ---------------------------------------------------------------------------
# Private/enterprise OIDs the otto Zephyr test-bed agent serves for data that
# no standard small-agent MIB carries (CPU%, heap, thread count). The same
# constants are mirrored by the firmware agent so the manager and the device
# agree on the OID map.
#
# TODO: 63245 is a placeholder Private Enterprise Number. Apply for a real PEN
# with IANA before shipping the agent outside the test bed.
OTTO_PEN = 63245
_OTTO_BASE = f"1.3.6.1.4.1.{OTTO_PEN}"

# Standard scalar OIDs (work against any standards-compliant agent).
OID_SYS_UPTIME = "1.3.6.1.2.1.1.3.0"  # sysUpTime, TimeTicks (1/100 s)


SnmpVersion = Literal["1", "2c"]


# ---------------------------------------------------------------------------
# Presentation layer — SnmpMetric descriptor (the SNMP analog of MetricParser)
# ---------------------------------------------------------------------------


class SnmpMetric(OttoModel):
    """How a single OID's value is interpreted and charted.

    Mirrors the presentation attributes :class:`~otto.monitor.parsers.MetricParser`
    already exposes (``chart``/``y_title``/``unit``/``tab``/``tab_label``) plus a
    ``scale`` factor that converts the raw integer varbind into a real value
    (e.g. sysUpTime is in hundredths of a second → ``scale=0.01`` for seconds;
    a CPU OID reported in centi-percent → ``scale=0.01`` for percent).

    These are deliberately *not* sourced from lab data — graphing stays in the
    monitor module. ``frozen=True``: a descriptor is an immutable, low-volume
    value object shared across ticks; the registry only ever replaces, never
    mutates. Built and registered through the public path
    (:func:`register_snmp_metric`) for first- and third-party descriptors alike.
    """

    model_config = ConfigDict(frozen=True)

    oid: str
    label: str
    chart: str
    y_title: str = ""
    unit: str = ""
    tab: str = "metrics"
    tab_label: str = "Metrics"
    scale: float = 1.0

    kind: Literal["gauge", "counter"] = "gauge"
    """How the varbind is interpreted: a ``gauge`` charts its (scaled) value
    directly; a ``counter`` is monotonic and is converted to a per-second rate
    via the target's :class:`~otto.monitor.rates.RateTracker` (negative delta
    -> re-baseline, skip the tick — see :mod:`otto.monitor.rates`)."""

    meta_of: str | None = None
    """When set to another OID, this descriptor's value is not charted as its
    own series — it is attached to the hover-meta dict of the series produced
    by the ``meta_of`` OID, under this descriptor's ``label``. A ``meta_of``
    target absent this tick simply drops the meta; never an error."""

    def to_point(self, raw: float) -> MetricDataPoint:
        """Apply ``scale`` to a raw numeric varbind, returning a chartable point."""
        return MetricDataPoint(value=round(raw * self.scale, 2))


# ---------------------------------------------------------------------------
# Built-in descriptor registry
# ---------------------------------------------------------------------------
# otto registers its own built-ins through the SAME register_snmp_metric() entry
# point a third party uses — one validation path for first- and third-party
# descriptors, mirroring the host-class registry decision. (See the Phase A
# design, "SNMP-metric registration symmetry".)

# Registration here always overwrites (see register_snmp_metric) — re-teaching
# otto how to chart an OID, including one of the built-ins below, is documented,
# tested behavior, not a mistake to catch loudly.
SNMP_METRICS: Registry[SnmpMetric] = Registry(
    "SNMP metric descriptor", register_hint="otto.monitor.snmp.register_snmp_metric()"
)


def register_snmp_metric(metric: SnmpMetric) -> None:
    """Register (or override) the descriptor for ``metric.oid``.

    Call from an init module listed in ``.otto/settings.toml`` to teach otto how
    to chart a private/device-specific OID — the same extension pattern as
    :func:`otto.monitor.parsers.register_host_parsers` and
    :func:`otto.host.command_frame.register_command_frame`.
    """
    SNMP_METRICS.register(metric.oid, metric, overwrite=True, origin=caller_module())


def _register_builtin_metrics() -> None:
    """Register the built-in descriptors via the public path.

    Standard ``sysUpTime`` works against any compliant agent (net-snmp, routers,
    …); the enterprise OIDs are scalars served by otto's Zephyr agent.
    """
    for metric in (
        SnmpMetric(
            oid=OID_SYS_UPTIME,
            label="Uptime",
            chart="Uptime",
            y_title="Uptime",
            unit="s",
            scale=0.01,
        ),
        SnmpMetric(
            oid=f"{_OTTO_BASE}.1.1.0",
            label="Overall CPU",
            chart="CPU",
            y_title="Usage %",
            unit="%",
            tab="cpu",
            tab_label="CPU",
            scale=0.01,
        ),
        SnmpMetric(
            oid=f"{_OTTO_BASE}.1.2.0",
            label="Heap Used",
            chart="Memory Usage",
            y_title="Memory",
            unit="B",
            tab="memory",
            tab_label="Memory",
        ),
        SnmpMetric(
            oid=f"{_OTTO_BASE}.1.3.0",
            label="Heap Free",
            chart="Memory Usage",
            y_title="Memory",
            unit="B",
            tab="memory",
            tab_label="Memory",
        ),
        SnmpMetric(
            oid=f"{_OTTO_BASE}.1.4.0", label="Threads", chart="Threads", y_title="Count", unit=""
        ),
    ):
        register_snmp_metric(metric)


_register_builtin_metrics()


def get_snmp_metric(oid: str) -> SnmpMetric | None:
    """Return the registered descriptor for ``oid``, or ``None``."""
    return SNMP_METRICS.get(oid) if oid in SNMP_METRICS else None


def resolve_snmp_metric(oid: str) -> SnmpMetric:
    """Return the descriptor for ``oid``, or a default-styled fallback.

    The fallback charts the OID under its own label with no unit on the generic
    ``metrics`` tab, so a host can poll a bare OID it declared in lab data
    without anyone having registered a descriptor for it.
    """
    metric = get_snmp_metric(oid)
    if metric is not None:
        return metric
    return SnmpMetric(oid=oid, label=oid, chart=oid)


def process_snmp_values(
    values: dict[str, float | None],
    *,
    rates: RateTracker,
    ts: datetime,
) -> list[tuple[str, MetricDataPoint, SnmpMetric]]:
    """Turn one GET's ``{oid: raw_value}`` into chartable ``(label, point, view)`` triples.

    Applies each descriptor's ``scale``; converts ``kind="counter"`` values to
    per-second rates via *rates* (first sighting / reset ticks emit nothing);
    routes ``meta_of`` descriptors into their target series' hover meta instead
    of their own series. OIDs with a ``None`` value are skipped.
    """
    resolved = {oid: resolve_snmp_metric(oid) for oid in values}
    scaled: dict[str, float] = {}
    for oid, raw in values.items():
        if raw is None:
            continue
        metric = resolved[oid]
        value = raw * metric.scale
        if metric.kind == "counter":
            rate = rates.update(oid, value, ts)
            if rate is None:
                continue
            value = rate
        scaled[oid] = round(value, 2)

    meta_map: dict[str, dict[str, str]] = {}
    for oid, value in scaled.items():
        metric = resolved[oid]
        if metric.meta_of is not None:
            formatted = (
                human_readable(value) if metric.unit == "B" else f"{value} {metric.unit}".strip()
            )
            meta_map.setdefault(metric.meta_of, {})[metric.label] = formatted

    triples: list[tuple[str, MetricDataPoint, SnmpMetric]] = []
    for oid, value in scaled.items():
        metric = resolved[oid]
        if metric.meta_of is not None:
            continue
        triples.append((metric.label, MetricDataPoint(value=value, meta=meta_map.get(oid)), metric))
    return triples


# ---------------------------------------------------------------------------
# Acquisition layer — thin async pysnmp v2c GET wrapper
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SnmpClient:
    """Async SNMP v1/v2c GET client for a single endpoint.

    The endpoint ``(address, port)`` is whatever is reachable from the otto
    host — for a device behind a hop, that is the local end of a UDP forward /
    relay, not the device's own address. Keeping the endpoint explicit means
    this client knows nothing about hop topology.
    """

    address: str
    port: int = 161
    community: str = "public"
    version: SnmpVersion = "2c"
    timeout: float = 2.0
    retries: int = 1

    async def get(self, oids: list[str]) -> dict[str, float | None]:
        """GET ``oids`` in one PDU; return ``{oid: numeric_value_or_None}``.

        Non-numeric or errored varbinds map to ``None`` so the caller can skip
        them. On a transport/PDU error the whole batch returns ``None`` values
        and the error is logged — a failed tick is non-fatal, matching the
        shell collector's per-host error handling.
        """
        if not oids:
            return {}

        # Lazy import so this module loads without pysnmp and unit tests can
        # mock this method without the dependency installed.
        from pysnmp.hlapi.v1arch.asyncio import (  # type: ignore[import-untyped]
            CommunityData,
            ObjectIdentity,
            ObjectType,
            SnmpDispatcher,
            UdpTransportTarget,
            get_cmd,
        )

        mp_model = 1 if self.version == "2c" else 0
        result: dict[str, float | None] = dict.fromkeys(oids)

        # One dispatcher per GET; close it in finally so the UDP socket isn't
        # leaked (otherwise it lingers until GC and trips ResourceWarning).
        dispatcher = SnmpDispatcher()
        try:
            transport = await UdpTransportTarget.create(
                (self.address, self.port),
                timeout=self.timeout,
                retries=self.retries,
            )
            error_indication, error_status, _error_index, var_binds = await get_cmd(
                dispatcher,
                CommunityData(self.community, mpModel=mp_model),
                transport,
                *(ObjectType(ObjectIdentity(oid)) for oid in oids),
            )
        except Exception as exc:  # noqa: BLE001 — SNMP client raises heterogeneous errors; all map to warning + empty result
            logger.warning("SNMP GET to %s:%d failed: %s", self.address, self.port, exc)
            return result
        finally:
            dispatcher.close()

        if error_indication:
            logger.warning("SNMP error from %s:%d: %s", self.address, self.port, error_indication)
            return result
        if error_status:
            logger.warning(
                "SNMP error-status from %s:%d: %s",
                self.address,
                self.port,
                error_status.prettyPrint(),
            )
            return result

        for var_bind in var_binds:
            oid_obj, value = var_bind
            oid_str = str(oid_obj)
            numeric = _coerce_numeric(value)
            # pysnmp may return the OID with or without a trailing instance .0;
            # match back to the requested key when possible.
            key = oid_str if oid_str in result else _match_requested(oid_str, result)
            if key is not None:
                result[key] = numeric
        return result


@dataclass(slots=True)
class SnmpSource:
    """A :class:`~otto.monitor.collector.MonitorTarget`'s SNMP collection mode.

    Pairs the :class:`SnmpClient` (where/how to reach the agent) with the bare
    list of OIDs to poll each tick. Presentation for those OIDs comes from the
    descriptor registry, not from here — this is acquisition only.
    """

    client: SnmpClient
    oids: list[str]
    rates: RateTracker = field(default_factory=RateTracker)
    """Per-target counter->rate state for ``kind="counter"`` descriptors."""


def _coerce_numeric(value: object) -> float | None:
    """Best-effort conversion of a pysnmp varbind value to float, else None.

    pysnmp's integer/gauge/counter/timeticks types satisfy ``SupportsInt``;
    string-valued varbinds (sysDescr, etc.) do not and map to ``None``.
    """
    if not isinstance(value, SupportsInt):
        return None
    try:
        return float(int(value))
    except (TypeError, ValueError):
        return None


def _match_requested(oid_str: str, requested: dict[str, float | None]) -> str | None:
    """Map a returned OID back to a requested key, tolerating a trailing ``.0``."""
    for key in requested:
        if key == oid_str or key.rstrip(".0") == oid_str.rstrip(".0"):
            return key
    return None
