"""Generate the committed monitor dummy-data fixtures (spec 2026-07-10 §5).

Builds the three review-mode fixture documents THROUGH the pydantic export
models — conformance-checked by construction — and writes them minified to
``web/fixtures/``. Deterministic: fixed base timestamps + seeded
``random.Random``, so regeneration is byte-identical (drift-guarded by
``tests/unit/scripts/test_monitor_fixture_files.py``). Regenerate via
``make monitor-fixtures`` whenever the schema or the scenarios change.

Import discipline (spec §5): only :mod:`otto.models` and the leaf
:mod:`otto.link.model` — deliberately nothing from ``otto.configmodule``,
which the library-extraction branch renames.

Series shapes are realistic on purpose (diurnal CPU, sawtooth memory leak,
a spike under an event span): chart UX cannot be judged against noise.
"""

import json
import math
import random
import sys
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from otto.link.model import LinkEndpoint, make_static_link_id
from otto.models import (
    ChartSpecRecord,
    ElementRecord,
    EventRecord,
    HostSnapshot,
    LabSnapshot,
    LinkEndpointSnapshot,
    LinkSnapshot,
    LogEventRecord,
    MetricRecord,
    MonitorExport,
    SessionMeta,
    SessionRecord,
    TabSpecRecord,
    TunnelRecord,
)

BASE = datetime(2026, 7, 1, 8, 0, 0, tzinfo=timezone.utc)
"""Fixed session-start epoch — never wall clock (determinism)."""

OUTAGE_S = (3600.0, 4800.0)
"""workers_w2 goes silent in this window (seconds from session start)."""

_DURATION_S = 7200.0  # kitchen-sink session length: 2 h
_CADENCE_S = 15.0  # base cadence; spec §12 allows trimming from the 5 s sketch

_SPARSE_DURATION_S = 1800.0  # sprawl/isp-core session length: 30 min
_SPARSE_CADENCE_S = 300.0  # sprawl/isp-core cadence: lab-rich, metrics-sparse fixtures


# --- lab building blocks -----------------------------------------------------


def _host(
    host_id: str,
    element: str,
    ip: str,
    *,
    board: str | None = None,
    slot: int | None = None,
    hop: str | None = None,
    os_version: str | None = "24.04",
    interfaces: dict[str, str] | None = None,
) -> HostSnapshot:
    """One fixture host with the common defaults filled in."""
    return HostSnapshot(
        id=host_id,
        element=element,
        name=host_id.replace("_", " "),
        board=board,
        slot=slot,
        hop=hop,
        os_type="unix",
        os_name="Linux",
        os_version=os_version,
        ip=ip,
        interfaces=interfaces or {"eth0": ip},
        labs=["fixture"],
        is_virtual=True,
    )


def _link(
    a: tuple[str, str | None, str],
    b: tuple[str, str | None, str],
    *,
    protocol: str = "tcp",
    provenance: Literal["implicit", "declared", "dynamic"] = "declared",
    name: str | None = None,
    impair: str | None = None,
) -> LinkSnapshot:
    """One fixture link; the id comes from the real static-id derivation."""
    ea = LinkEndpoint(host=a[0], interface=a[1], ip=a[2])
    eb = LinkEndpoint(host=b[0], interface=b[1], ip=b[2])
    return LinkSnapshot(
        id=make_static_link_id(ea, eb, name),
        endpoints=[
            LinkEndpointSnapshot(host=a[0], interface=a[1], ip=a[2]),
            LinkEndpointSnapshot(host=b[0], interface=b[1], ip=b[2]),
        ],
        protocol=protocol,
        provenance=provenance,
        name=name,
        impair=impair,
    )


def _implicit_links(hosts: Iterable[HostSnapshot]) -> list[LinkSnapshot]:
    """Hop-chain edges: one implicit link per host with a ``hop`` set."""
    by_id = {h.id: h for h in hosts}
    return [
        _link((h.hop, None, by_id[h.hop].ip), (h.id, None, h.ip), provenance="implicit")
        for h in by_id.values()
        if h.hop is not None and h.hop in by_id
    ]


# --- series synthesis ---------------------------------------------------------


def _series(
    rng: random.Random,
    host: str,
    label: str,
    fn: Callable[[float], float],
    *,
    start: datetime,
    duration_s: float,
    cadence_s: float,
    source: str | None = None,
    gaps: tuple[tuple[float, float], ...] = (),
) -> list[MetricRecord]:
    """Sample ``fn(t_seconds)`` on a fixed cadence, skipping outage gaps."""
    del rng  # cadence jitter deliberately omitted: keeps health derivation exact
    records = []
    for tick in range(int(duration_s // cadence_s) + 1):
        t = tick * cadence_s
        if any(lo <= t < hi for lo, hi in gaps):
            continue
        records.append(
            MetricRecord(
                timestamp=start + timedelta(seconds=t),
                host=host,
                label=label,
                value=round(min(max(fn(t), 0.0), 1e9), 3),
                source=source,
            )
        )
    return records


def _diurnal(
    rng: random.Random, *, base: float, amp: float, period_s: float = 5400.0
) -> Callable[[float], float]:
    """Slow sine + noise, clamped to [0, 100] — a plausible CPU% curve."""
    phase = rng.uniform(0.0, 2 * math.pi)
    return lambda t: min(
        100.0,
        base + amp * math.sin(2 * math.pi * t / period_s + phase) + rng.uniform(-amp / 8, amp / 8),
    )


def _sawtooth(*, lo: float, hi: float, period_s: float) -> Callable[[float], float]:
    """Linear climb + reset — the classic leak-then-restart memory shape."""
    return lambda t: lo + (hi - lo) * ((t % period_s) / period_s)


def _noisy(rng: random.Random, *, base: float, jitter: float) -> Callable[[float], float]:
    """Flat line with noise — network/disk background chatter."""
    return lambda _t: base + rng.uniform(-jitter, jitter)


def _with_spike(
    fn: Callable[[float], float], *, center_s: float, width_s: float, height: float
) -> Callable[[float], float]:
    """Overlay a gaussian bump (aligned with an event span in the fixture)."""
    return lambda t: fn(t) + height * math.exp(-((t - center_s) ** 2) / (2 * width_s**2))


# --- presentation meta ---------------------------------------------------------


def _chart(label: str, unit: str, chart: str, interval: float) -> ChartSpecRecord:
    """One fixture chart spec (``command`` is an honest fixture marker)."""
    return ChartSpecRecord(
        label=label,
        y_title=label,
        unit=unit,
        command=f"fixture:{chart}",
        chart=chart,
        interval=interval,
    )


_HOST_CHARTS = [
    ("CPU %", "%", "cpu", _CADENCE_S),
    ("Memory MB", "MB", "mem", _CADENCE_S),
    ("Net kB/s", "kB/s", "net", 30.0),
    ("Disk io/s", "io/s", "disk", 30.0),
]
_MGMT_CHARTS = [
    ("PSU Temp °C", "°C", "psu-temp", 60.0),
    ("Fan RPM", "rpm", "fan", 60.0),
    ("Ambient °C", "°C", "ambient", 60.0),
]


def _meta(charts: list[tuple[str, str, str, float]], *, tables: bool) -> SessionMeta:
    """Session presentation meta for the given chart set."""
    specs = [_chart(*c) for c in charts]
    tabs = [TabSpecRecord(id="overview", label="Overview", metrics=[c.label for c in specs])]
    if tables:
        tabs.append(
            TabSpecRecord(
                id="kernel",
                label="Kernel",
                metrics=[],
                kind="table",
                columns=["level", "facility", "message"],
            )
        )
    return SessionMeta(interval=_CADENCE_S, charts=specs, tabs=tabs)


def _chart_map(meta: SessionMeta) -> dict[str, str]:
    """Bare series label -> chart key; fixtures use label == chart label."""
    return {c.label: c.label for c in meta.charts}


# --- host data bundles ----------------------------------------------------------


def _host_metrics(
    rng: random.Random,
    host_ids: list[str],
    *,
    start: datetime,
    duration_s: float,
    gaps_for: dict[str, tuple[tuple[float, float], ...]] | None = None,
    cpu_extra: dict[str, Callable[[Callable[[float], float]], Callable[[float], float]]]
    | None = None,
) -> list[MetricRecord]:
    """Build the four standard per-host series for each host id."""
    gaps_for = gaps_for or {}
    cpu_extra = cpu_extra or {}
    out: list[MetricRecord] = []
    for hid in host_ids:
        gaps = gaps_for.get(hid, ())
        cpu = _diurnal(rng, base=35.0, amp=20.0)
        if hid in cpu_extra:
            cpu = cpu_extra[hid](cpu)
        args = {"start": start, "duration_s": duration_s, "gaps": gaps}
        out += _series(rng, hid, "CPU %", cpu, cadence_s=_CADENCE_S, **args)
        out += _series(
            rng,
            hid,
            "Memory MB",
            _sawtooth(lo=900.0, hi=3100.0, period_s=2700.0),
            cadence_s=_CADENCE_S,
            **args,
        )
        out += _series(
            rng, hid, "Net kB/s", _noisy(rng, base=420.0, jitter=180.0), cadence_s=30.0, **args
        )
        out += _series(
            rng, hid, "Disk io/s", _noisy(rng, base=55.0, jitter=25.0), cadence_s=30.0, **args
        )
    return out


# --- the three documents ---------------------------------------------------------


def kitchen_sink() -> MonitorExport:
    """Every UI feature in one lab (spec §5 table)."""
    rng = random.Random(20260710)  # noqa: S311 — deterministic dummy data, not cryptography
    hosts = [
        _host(
            "edge-gw", "edge-gw", "10.20.0.1", interfaces={"eth0": "10.20.0.1", "eth1": "10.20.1.1"}
        ),
        _host("chassis-a_lc1", "chassis-a", "10.20.1.11", board="lc1", slot=1, hop="edge-gw"),
        _host(
            "chassis-a_lc2",
            "chassis-a",
            "10.20.1.12",
            board="lc2",
            slot=2,
            hop="edge-gw",
            os_version=None,
        ),  # metadata hole: no os_version
        _host("chassis-a_sup", "chassis-a", "10.20.1.15", board="sup", slot=5, hop="edge-gw"),
        _host("workers_w1", "workers", "10.20.2.21", board="w1"),  # board, no slot
        _host("workers_w2", "workers", "10.20.2.22", board="w2"),
        _host("workers_w3", "workers", "10.20.2.23", board="w3"),
        _host("db-01", "db-01", "10.20.3.31"),  # singleton, no hop
        _host("mgmt-01", "mgmt-01", "10.20.4.41"),  # the external source
    ]
    elements = [
        ElementRecord(
            id="spare-chassis", type="physical", description="Spare 8-slot chassis, unpopulated"
        ),
        ElementRecord(id="workers", type="logical", description="Load-generator cluster"),
    ]
    links = [
        *_implicit_links(hosts),
        _link(("workers_w1", "eth0", "10.20.2.21"), ("db-01", "eth0", "10.20.3.31"), name="app-db"),
        _link(
            ("workers_w3", "eth0", "10.20.2.23"),
            ("db-01", "eth0", "10.20.3.31"),
            protocol="udp",
            name="metrics-udp",
            impair="edge-gw",
        ),
    ]
    spike = {
        "chassis-a_lc1": lambda cpu: _with_spike(cpu, center_s=5400.0, width_s=180.0, height=45.0)
    }
    metrics = _host_metrics(
        rng,
        [h.id for h in hosts],
        start=BASE,
        duration_s=_DURATION_S,
        gaps_for={"workers_w2": (OUTAGE_S,)},
        cpu_extra=spike,
    )
    for board in ("chassis-a_lc1", "chassis-a_lc2", "chassis-a_sup"):
        metrics += _series(
            rng,
            board,
            "PSU Temp °C",
            _noisy(rng, base=41.0, jitter=2.5),
            start=BASE,
            duration_s=_DURATION_S,
            cadence_s=60.0,
            source="mgmt-01",
        )
        metrics += _series(
            rng,
            board,
            "Fan RPM",
            _noisy(rng, base=7200.0, jitter=400.0),
            start=BASE,
            duration_s=_DURATION_S,
            cadence_s=60.0,
            source="mgmt-01",
        )
    for element in ("chassis-a", "spare-chassis"):  # element-targeted series
        metrics += _series(
            rng,
            element,
            "Ambient °C",
            _noisy(rng, base=24.0, jitter=1.0),
            start=BASE,
            duration_s=_DURATION_S,
            cadence_s=60.0,
            source="mgmt-01",
        )
    events = [
        EventRecord(
            id=1,
            timestamp=BASE + timedelta(minutes=20),
            label="config reload",
            source="manual",
            color="#7c5cff",
        ),
        EventRecord(
            id=2,
            timestamp=BASE + timedelta(minutes=85),
            end_timestamp=BASE + timedelta(minutes=95),
            label="stress run",
            source="manual",
            color="#ff6b6b",
        ),  # aligns with the CPU spike
        EventRecord(
            id=3,
            timestamp=BASE + timedelta(minutes=90),
            end_timestamp=BASE + timedelta(minutes=100),
            label="log capture",
            source="manual",
            color="#2f9e6e",
        ),  # overlaps the stress span
        EventRecord(
            id=4,
            timestamp=BASE + timedelta(minutes=60),
            label="w2 lost",
            source="watchdog",
            color="#e8a13c",
        ),
    ]
    log_events = [
        LogEventRecord(
            timestamp=BASE + timedelta(minutes=3 * i + (0 if hid == "db-01" else 1)),
            host=hid,
            tab="kernel",
            fields={
                "level": rng.choice(["info", "warn", "err"]),
                "facility": "kern",
                "message": f"fixture kernel message {i} on {hid}",
            },
        )
        for hid in ("chassis-a_lc1", "db-01")
        for i in range(40)
    ]
    meta = _meta(_HOST_CHARTS + _MGMT_CHARTS, tables=True)
    session = SessionRecord(
        id="2026-07-01T08-00-00-kitchen-sink",
        label="kitchen sink",
        note="Synthetic fixture exercising every monitor UI feature.",
        start=BASE,
        end=BASE + timedelta(seconds=_DURATION_S),
        lab=LabSnapshot(elements=elements, hosts=hosts, links=links),
        meta=meta,
        metrics=metrics,
        events=events,
        log_events=log_events,
        chart_map=_chart_map(meta),
        tunnels=[
            # Bare 2-hop tunnel: no declared/implicit link joins edge-gw⇄db-01,
            # so this renders as a bare segment — the old chord look, pinned.
            TunnelRecord(
                id="tun-00000000demo-15001",
                protocol="udp",
                service_port=15001,
                hops=["edge-gw", "db-01"],
                status="ok",
                carriers_present=4,
                carriers_expected=4,
                age_seconds=3600.0,
            ),
        ],
    )
    return MonitorExport(format=1, sessions=[session])


def minimal() -> MonitorExport:
    """Cover the degenerate case: one singleton host, two series, nothing else."""
    rng = random.Random(11)  # noqa: S311 — deterministic dummy data, not cryptography
    host = _host("solo", "solo", "10.30.0.5")
    meta = _meta(_HOST_CHARTS[:2], tables=False)
    start = BASE
    metrics = _series(
        rng,
        "solo",
        "CPU %",
        _diurnal(rng, base=20.0, amp=10.0),
        start=start,
        duration_s=1800.0,
        cadence_s=30.0,
    )
    metrics += _series(
        rng,
        "solo",
        "Memory MB",
        _sawtooth(lo=400.0, hi=900.0, period_s=1200.0),
        start=start,
        duration_s=1800.0,
        cadence_s=30.0,
    )
    session = SessionRecord(
        id="2026-07-01T08-00-00-minimal",
        label="minimal",
        start=start,
        end=start + timedelta(seconds=1800.0),
        lab=LabSnapshot(hosts=[host]),
        meta=meta,
        metrics=metrics,
        chart_map=_chart_map(meta),
    )
    return MonitorExport(format=1, sessions=[session])


def cascade() -> MonitorExport:
    """Dead-gateway reachability scenario (topology spec 2026-07-11).

    ``gw-a`` and both rack hosts behind it go silent at 60 min: raw health
    reads down x3, the topology cascade must read down x1 (the gateway) +
    unreachable x2 (its children). ``solo-ok`` proves healthy branches are
    untouched. The rack pair also carries two declared links between the
    SAME endpoint pair -- the parallel-edge fan-out case.
    """
    rng = random.Random(20260711)  # noqa: S311 — deterministic dummy data, not cryptography
    hosts = [
        _host("gw-a", "gw-a", "10.30.0.1", interfaces={"eth0": "10.30.0.1", "eth1": "10.30.1.1"}),
        _host("rack-a_n1", "rack-a", "10.30.1.11", board="n1", slot=1, hop="gw-a"),
        _host("rack-a_n2", "rack-a", "10.30.1.12", board="n2", slot=2, hop="gw-a"),
        _host("solo-ok", "solo-ok", "10.30.2.21"),
    ]
    links = [
        *_implicit_links(hosts),
        _link(
            ("rack-a_n1", "eth0", "10.30.1.11"), ("rack-a_n2", "eth0", "10.30.1.12"), name="pair-a"
        ),
        _link(
            ("rack-a_n1", "eth0", "10.30.1.11"),
            ("rack-a_n2", "eth0", "10.30.1.12"),
            protocol="udp",
            name="pair-b",
        ),
    ]
    cadence = 30.0
    # _series ticks are inclusive of t == duration_s (the session-end sample),
    # but the gap test is half-open (lo <= t < hi): a hi of exactly
    # _DURATION_S would NOT catch that final tick, leaving one spurious
    # sample at session end and undoing "silent through session end". Push
    # hi one cadence past the end so the boundary tick is caught too.
    dead = (3600.0, _DURATION_S + cadence)
    gaps_for = {"gw-a": (dead,), "rack-a_n1": (dead,), "rack-a_n2": (dead,)}
    metrics: list[MetricRecord] = []
    for h in hosts:
        metrics += _series(
            rng,
            h.id,
            "CPU %",
            _diurnal(rng, base=35.0, amp=20.0),
            start=BASE,
            duration_s=_DURATION_S,
            cadence_s=cadence,
            gaps=gaps_for.get(h.id, ()),
        )
    meta = _meta([("CPU %", "%", "cpu", 30.0)], tables=False)
    session = SessionRecord(
        id="2026-07-01T08-00-00-cascade",
        label="cascade",
        start=BASE,
        end=BASE + timedelta(seconds=_DURATION_S),
        lab=LabSnapshot(hosts=hosts, links=links),
        meta=meta,
        chart_map=_chart_map(meta),
        metrics=metrics,
    )
    return MonitorExport(format=1, sessions=[session])


def sprawl() -> MonitorExport:
    """Build a deep management chain the layout redesign is judged against.

    ``jump-01`` -> {``edge-gw``, ``core-gw``} -> {app/cache/queue/workers
    hosts under ``core-gw``; ``chassis-a`` + ``console-01`` under
    ``edge-gw``} -> ``zephyr-01``/``zephyr-02`` under ``console-01``: a
    3-hop chain kitchen-sink (management depth capped at 1) cannot produce.
    The data plane skips columns (``chassis-a_lc1``/``app-01``/``zephyr-01``
    all reach back to the top-of-rack switches directly, bypassing their own
    management chain) and fans out in parallel (``workers_w1``/``w3`` both
    to ``db-01``). ``core-gw`` and everything hopping through it go dark for
    the back half of the session — a wider cascade than the two-host one in
    :func:`cascade`.
    """
    rng = random.Random(20260712)  # noqa: S311 — deterministic dummy data, not cryptography
    hosts = [
        _host("jump-01", "jump-01", "10.60.0.1"),
        _host("mgmt-01", "mgmt-01", "10.60.0.2"),
        _host("tor-sw-a", "tor-sw-a", "10.60.0.3"),
        _host("tor-sw-b", "tor-sw-b", "10.60.0.4"),
        _host(
            "edge-gw",
            "edge-gw",
            "10.60.1.1",
            hop="jump-01",
            interfaces={"eth0": "10.60.1.1", "eth1": "10.60.1.101"},
        ),
        _host("core-gw", "core-gw", "10.60.1.2", hop="jump-01"),
        _host("db-01", "db-01", "10.60.1.3", hop="jump-01"),
        _host("db-02", "db-02", "10.60.1.4", hop="jump-01"),
        _host("app-01", "app-01", "10.60.2.1", hop="core-gw"),
        _host("app-02", "app-02", "10.60.2.2", hop="core-gw"),
        _host("app-03", "app-03", "10.60.2.3", hop="core-gw"),
        _host("app-04", "app-04", "10.60.2.4", hop="core-gw"),
        _host("cache-01", "cache-01", "10.60.2.5", hop="core-gw"),
        _host("queue-01", "queue-01", "10.60.2.6", hop="core-gw"),
        _host("workers_w1", "workers", "10.60.2.11", board="w1", hop="core-gw"),
        _host("workers_w2", "workers", "10.60.2.12", board="w2", hop="core-gw"),
        _host("workers_w3", "workers", "10.60.2.13", board="w3", hop="core-gw"),
        _host("workers_w4", "workers", "10.60.2.14", board="w4", hop="core-gw"),
        _host("chassis-a_lc1", "chassis-a", "10.60.3.1", board="lc1", slot=1, hop="edge-gw"),
        _host("chassis-a_lc2", "chassis-a", "10.60.3.2", board="lc2", slot=2, hop="edge-gw"),
        _host("chassis-a_sup", "chassis-a", "10.60.3.3", board="sup", slot=3, hop="edge-gw"),
        _host("console-01", "console-01", "10.60.3.10", hop="edge-gw"),
        _host("zephyr-01", "zephyr-01", "10.60.4.1", hop="console-01"),
        _host("zephyr-02", "zephyr-02", "10.60.4.2", hop="console-01"),
    ]
    links = [
        *_implicit_links(hosts),
        _link(
            ("tor-sw-a", "eth0", "10.60.0.3"), ("tor-sw-b", "eth0", "10.60.0.4"), name="mlag-peer"
        ),
        _link(("edge-gw", "eth0", "10.60.1.1"), ("core-gw", "eth0", "10.60.1.2"), name="edge-core"),
        _link(("db-01", "eth0", "10.60.1.3"), ("db-02", "eth0", "10.60.1.4"), name="db-repl"),
        _link(
            ("app-01", "eth0", "10.60.2.1"), ("cache-01", "eth0", "10.60.2.5"), name="app01-cache"
        ),
        _link(
            ("app-02", "eth0", "10.60.2.2"), ("cache-01", "eth0", "10.60.2.5"), name="app02-cache"
        ),
        _link(
            ("app-03", "eth0", "10.60.2.3"), ("queue-01", "eth0", "10.60.2.6"), name="app03-queue"
        ),
        _link(
            ("app-04", "eth0", "10.60.2.4"), ("queue-01", "eth0", "10.60.2.6"), name="app04-queue"
        ),
        _link(
            ("app-01", "eth0", "10.60.2.1"),
            ("db-01", "eth0", "10.60.1.3"),
            name="app01-db",
            impair="edge-gw",
        ),
        _link(("app-02", "eth0", "10.60.2.2"), ("db-01", "eth0", "10.60.1.3"), name="app02-db"),
        _link(("app-03", "eth0", "10.60.2.3"), ("db-02", "eth0", "10.60.1.4"), name="app03-db"),
        _link(("app-04", "eth0", "10.60.2.4"), ("db-02", "eth0", "10.60.1.4"), name="app04-db"),
        _link(("workers_w1", "eth0", "10.60.2.11"), ("db-01", "eth0", "10.60.1.3"), name="w1-db"),
        _link(("workers_w3", "eth0", "10.60.2.13"), ("db-01", "eth0", "10.60.1.3"), name="w3-db"),
        _link(
            ("chassis-a_lc1", "eth0", "10.60.3.1"),
            ("tor-sw-a", "eth0", "10.60.0.3"),
            name="chassis-mgmt",
        ),
        _link(("app-01", "eth0", "10.60.2.1"), ("tor-sw-a", "eth0", "10.60.0.3"), name="app01-tor"),
        _link(
            ("zephyr-01", "eth0", "10.60.4.1"), ("tor-sw-b", "eth0", "10.60.0.4"), name="zephyr-tor"
        ),
    ]
    # core-gw down; everything that hops through it goes dark with it (a
    # wider cascade than the two-host one in `cascade()`).
    down = (
        "core-gw",
        "app-01",
        "app-02",
        "app-03",
        "app-04",
        "cache-01",
        "queue-01",
        "workers_w1",
        "workers_w2",
        "workers_w3",
        "workers_w4",
    )
    dead = (600.0, _SPARSE_DURATION_S + _SPARSE_CADENCE_S)  # see cascade()'s boundary-tick note
    metrics: list[MetricRecord] = []
    for h in hosts:
        args = {
            "start": BASE,
            "duration_s": _SPARSE_DURATION_S,
            "gaps": (dead,) if h.id in down else (),
        }
        metrics += _series(
            rng,
            h.id,
            "CPU %",
            _diurnal(rng, base=35.0, amp=20.0),
            cadence_s=_SPARSE_CADENCE_S,
            **args,
        )
        metrics += _series(
            rng,
            h.id,
            "Memory MB",
            _sawtooth(lo=900.0, hi=3100.0, period_s=2700.0),
            cadence_s=_SPARSE_CADENCE_S,
            **args,
        )
    for hid in ("tor-sw-a", "console-01"):  # reports-for star: mgmt-01 is the ambient source
        metrics += _series(
            rng,
            hid,
            "Ambient °C",
            _noisy(rng, base=24.0, jitter=1.0),
            start=BASE,
            duration_s=_SPARSE_DURATION_S,
            cadence_s=_SPARSE_CADENCE_S,
            source="mgmt-01",
        )
    meta = _meta(
        [
            ("CPU %", "%", "cpu", _SPARSE_CADENCE_S),
            ("Memory MB", "MB", "mem", _SPARSE_CADENCE_S),
            ("Ambient °C", "°C", "ambient", _SPARSE_CADENCE_S),
        ],
        tables=False,
    )
    session = SessionRecord(
        id="2026-07-01T08-00-00-sprawl",
        label="sprawl",
        note="Deep management chain (jump -> gateway -> console -> zephyr) with skip-column links.",
        start=BASE,
        end=BASE + timedelta(seconds=_SPARSE_DURATION_S),
        lab=LabSnapshot(hosts=hosts, links=links),
        meta=meta,
        metrics=metrics,
        chart_map=_chart_map(meta),
        tunnels=[
            # 3-hop path riding two DECLARED links (app01-tor, app01-db):
            # the riding-overlay vector. Degraded: 4 of 6 carriers alive.
            TunnelRecord(
                id="tun-000000a9db01-15002",
                protocol="tcp",
                service_port=15002,
                hops=["tor-sw-a", "app-01", "db-01"],
                status="degraded",
                carriers_present=4,
                carriers_expected=6,
                age_seconds=900.0,
            ),
            # The old tun-jump-zephyr, kept as a 2-hop ok tunnel.
            TunnelRecord(
                id="tun-0000jumpzeph-15004",
                protocol="tcp",
                service_port=15004,
                hops=["jump-01", "zephyr-01"],
                status="ok",
                carriers_present=4,
                carriers_expected=4,
                age_seconds=7200.0,
            ),
        ],
    )
    return MonitorExport(format=1, sessions=[session])


def isp_core() -> MonitorExport:
    """Build the degenerate case the layout redesign targets.

    Every element sits 0 or 1 management hops from ``jump-01``, so the
    hops-from-local axis alone cannot separate 23 elements into anything but
    a handful of columns — while the data plane underneath (border -> core
    -> aggregation -> access, plus a mobile-core side-mesh) is four tiers
    deep and richly meshed. ``ems-01`` is the metrics source for
    pe/core/mobile-core, ``ems-02`` for agg/acc: two disjoint reports-for
    stars. ``core-02`` goes dark for the back half of the session — a plain
    "down", not a cascade, since nothing hops through it.
    """
    rng = random.Random(20260713)  # noqa: S311 — deterministic dummy data, not cryptography
    hosts = [
        _host("jump-01", "jump-01", "10.70.0.1"),
        _host("ems-01", "ems-01", "10.70.0.2"),
        _host("ems-02", "ems-02", "10.70.0.3"),
        _host("pe-01", "pe-01", "10.70.1.1"),
        _host("pe-02", "pe-02", "10.70.1.2"),
        _host("core-01", "core-01", "10.70.1.11"),
        _host("core-02", "core-02", "10.70.1.12"),
        _host("mme-01", "mme-01", "10.70.1.21"),
        _host("sgw-01", "sgw-01", "10.70.1.22"),
        _host("pgw-01", "pgw-01", "10.70.1.23"),
        _host("hss-01", "hss-01", "10.70.1.24"),
        _host("agg-01_lc1", "agg-01", "10.70.2.11", board="lc1", slot=1, hop="jump-01"),
        _host("agg-01_lc2", "agg-01", "10.70.2.12", board="lc2", slot=2, hop="jump-01"),
        _host("agg-01_lc3", "agg-01", "10.70.2.13", board="lc3", slot=3, hop="jump-01"),
        _host("agg-02", "agg-02", "10.70.2.21", hop="jump-01"),
        _host("agg-03", "agg-03", "10.70.2.31", hop="jump-01"),
        _host("agg-04", "agg-04", "10.70.2.41", hop="jump-01"),
        _host("acc-01", "acc-01", "10.70.3.1", hop="jump-01"),
        _host("acc-02", "acc-02", "10.70.3.2", hop="jump-01"),
        _host("acc-03", "acc-03", "10.70.3.3", hop="jump-01"),
        _host("acc-04", "acc-04", "10.70.3.4", hop="jump-01"),
        _host("acc-05", "acc-05", "10.70.3.5", hop="jump-01"),
        _host("acc-06", "acc-06", "10.70.3.6", hop="jump-01"),
        _host("acc-07", "acc-07", "10.70.3.7", hop="jump-01"),
        _host("acc-08", "acc-08", "10.70.3.8", hop="jump-01"),
    ]
    links = [
        *_implicit_links(hosts),
        _link(
            ("pe-01", "eth0", "10.70.1.1"), ("core-01", "eth0", "10.70.1.11"), name="pe01-core01"
        ),
        _link(
            ("pe-01", "eth0", "10.70.1.1"), ("core-02", "eth0", "10.70.1.12"), name="pe01-core02"
        ),
        _link(
            ("pe-02", "eth0", "10.70.1.2"), ("core-01", "eth0", "10.70.1.11"), name="pe02-core01"
        ),
        _link(
            ("pe-02", "eth0", "10.70.1.2"), ("core-02", "eth0", "10.70.1.12"), name="pe02-core02"
        ),
        _link(
            ("core-01", "eth0", "10.70.1.11"),
            ("core-02", "eth0", "10.70.1.12"),
            name="core01-core02",
        ),
        _link(
            ("agg-01_lc1", "eth0", "10.70.2.11"),
            ("core-01", "eth0", "10.70.1.11"),
            name="agg01-core01",
        ),
        _link(
            ("agg-01_lc2", "eth0", "10.70.2.12"),
            ("core-02", "eth0", "10.70.1.12"),
            name="agg01-core02",
        ),
        _link(
            ("agg-02", "eth0", "10.70.2.21"), ("core-01", "eth0", "10.70.1.11"), name="agg02-core01"
        ),
        _link(
            ("agg-02", "eth0", "10.70.2.21"), ("core-02", "eth0", "10.70.1.12"), name="agg02-core02"
        ),
        _link(
            ("agg-03", "eth0", "10.70.2.31"), ("core-01", "eth0", "10.70.1.11"), name="agg03-core01"
        ),
        _link(
            ("agg-03", "eth0", "10.70.2.31"), ("core-02", "eth0", "10.70.1.12"), name="agg03-core02"
        ),
        _link(
            ("agg-04", "eth0", "10.70.2.41"), ("core-01", "eth0", "10.70.1.11"), name="agg04-core01"
        ),
        _link(
            ("agg-04", "eth0", "10.70.2.41"), ("core-02", "eth0", "10.70.1.12"), name="agg04-core02"
        ),
        _link(
            ("acc-01", "eth0", "10.70.3.1"),
            ("agg-01_lc3", "eth0", "10.70.2.13"),
            name="acc01-agg01",
        ),
        _link(
            ("acc-02", "eth0", "10.70.3.2"),
            ("agg-01_lc3", "eth0", "10.70.2.13"),
            name="acc02-agg01",
        ),
        _link(
            ("acc-03", "eth0", "10.70.3.3"), ("agg-02", "eth0", "10.70.2.21"), name="acc03-agg02"
        ),
        _link(
            ("acc-04", "eth0", "10.70.3.4"), ("agg-02", "eth0", "10.70.2.21"), name="acc04-agg02"
        ),
        _link(
            ("acc-05", "eth0", "10.70.3.5"), ("agg-03", "eth0", "10.70.2.31"), name="acc05-agg03"
        ),
        _link(
            ("acc-06", "eth0", "10.70.3.6"), ("agg-03", "eth0", "10.70.2.31"), name="acc06-agg03"
        ),
        _link(
            ("acc-07", "eth0", "10.70.3.7"), ("agg-04", "eth0", "10.70.2.41"), name="acc07-agg04"
        ),
        _link(
            ("acc-08", "eth0", "10.70.3.8"), ("agg-04", "eth0", "10.70.2.41"), name="acc08-agg04"
        ),
        _link(("acc-01", "eth0", "10.70.3.1"), ("acc-02", "eth0", "10.70.3.2"), name="acc-ring-1"),
        _link(("acc-03", "eth0", "10.70.3.3"), ("acc-04", "eth0", "10.70.3.4"), name="acc-ring-2"),
        _link(("acc-05", "eth0", "10.70.3.5"), ("acc-06", "eth0", "10.70.3.6"), name="acc-ring-3"),
        _link(("acc-07", "eth0", "10.70.3.7"), ("acc-08", "eth0", "10.70.3.8"), name="acc-ring-4"),
        _link(
            ("mme-01", "eth0", "10.70.1.21"), ("core-01", "eth0", "10.70.1.11"), name="mme01-core01"
        ),
        _link(
            ("sgw-01", "eth0", "10.70.1.22"), ("core-01", "eth0", "10.70.1.11"), name="sgw01-core01"
        ),
        _link(
            ("pgw-01", "eth0", "10.70.1.23"), ("core-02", "eth0", "10.70.1.12"), name="pgw01-core02"
        ),
        _link(
            ("hss-01", "eth0", "10.70.1.24"), ("core-02", "eth0", "10.70.1.12"), name="hss01-core02"
        ),
        _link(
            ("pgw-01", "eth0", "10.70.1.23"),
            ("pe-01", "eth0", "10.70.1.1"),
            name="pgw01-pe01",
            impair="core-02",
        ),
    ]
    dead = (600.0, _SPARSE_DURATION_S + _SPARSE_CADENCE_S)  # see cascade()'s boundary-tick note
    ems01_hosts = ["pe-01", "pe-02", "core-01", "core-02", "mme-01", "sgw-01", "pgw-01", "hss-01"]
    ems02_hosts = [
        "agg-01_lc1",
        "agg-01_lc2",
        "agg-01_lc3",
        "agg-02",
        "agg-03",
        "agg-04",
        "acc-01",
        "acc-02",
        "acc-03",
        "acc-04",
        "acc-05",
        "acc-06",
        "acc-07",
        "acc-08",
    ]
    metrics: list[MetricRecord] = []
    for h in hosts:
        metrics += _series(
            rng,
            h.id,
            "CPU %",
            _diurnal(rng, base=35.0, amp=20.0),
            start=BASE,
            duration_s=_SPARSE_DURATION_S,
            cadence_s=_SPARSE_CADENCE_S,
            gaps=(dead,) if h.id == "core-02" else (),
        )
    for hid in ems01_hosts:
        metrics += _series(
            rng,
            hid,
            "PSU Temp °C",
            _noisy(rng, base=42.0, jitter=2.0),
            start=BASE,
            duration_s=_SPARSE_DURATION_S,
            cadence_s=_SPARSE_CADENCE_S,
            source="ems-01",
            gaps=(dead,) if hid == "core-02" else (),
        )
    for hid in ems02_hosts:
        metrics += _series(
            rng,
            hid,
            "PSU Temp °C",
            _noisy(rng, base=39.0, jitter=2.0),
            start=BASE,
            duration_s=_SPARSE_DURATION_S,
            cadence_s=_SPARSE_CADENCE_S,
            source="ems-02",
        )
    meta = _meta(
        [
            ("CPU %", "%", "cpu", _SPARSE_CADENCE_S),
            ("PSU Temp °C", "°C", "psu-temp", _SPARSE_CADENCE_S),
        ],
        tables=False,
    )
    session = SessionRecord(
        id="2026-07-01T08-00-00-isp-core",
        label="isp-core",
        note="Short management paths (0-1 hops); deep, richly meshed data plane.",
        start=BASE,
        end=BASE + timedelta(seconds=_SPARSE_DURATION_S),
        lab=LabSnapshot(hosts=hosts, links=links),
        meta=meta,
        metrics=metrics,
        chart_map=_chart_map(meta),
        tunnels=[
            # Uncertain: a hop host was unreachable during the last scan.
            TunnelRecord(
                id="tun-0000jumpacc7-15003",
                protocol="udp",
                service_port=15003,
                hops=["jump-01", "acc-07"],
                status="uncertain",
                carriers_present=2,
                carriers_expected=4,
                age_seconds=300.0,
            ),
        ],
    )
    return MonitorExport(format=1, sessions=[session])


def drift() -> MonitorExport:
    """Three sessions across months over one evolving lab (spec §5)."""
    rng = random.Random(77)  # noqa: S311 — deterministic dummy data, not cryptography

    def lab_v1() -> LabSnapshot:
        hosts = [
            _host("chassis-a_lc1", "chassis-a", "10.20.1.11", board="lc1", slot=1),
            _host("chassis-a_sup", "chassis-a", "10.20.1.15", board="sup", slot=5),
            _host("db-01", "db-01", "10.20.3.31"),
        ]
        return LabSnapshot(hosts=hosts)

    def lab_v2() -> LabSnapshot:
        v1 = lab_v1()
        hosts = [
            *v1.hosts,
            _host("chassis-a_lc2", "chassis-a", "10.20.1.12", board="lc2", slot=2),
            _host("workers_w1", "workers", "10.20.2.21", board="w1"),
            _host("workers_w2", "workers", "10.20.2.22", board="w2"),
        ]
        links = [
            _link(
                ("workers_w1", "eth0", "10.20.2.21"), ("db-01", "eth0", "10.20.3.31"), name="app-db"
            )
        ]
        return LabSnapshot(hosts=hosts, links=links)

    def lab_v3() -> LabSnapshot:
        v2 = lab_v2()
        gw = _host("edge-gw", "edge-gw", "10.20.0.1")
        hosts = [gw] + [
            h.model_copy(
                update={
                    "slot": 3 if h.board == "lc2" else h.slot,  # board slot moved
                    "hop": "edge-gw" if h.element == "chassis-a" else h.hop,
                }
            )
            for h in v2.hosts
            if h.id != "workers_w2"  # host removed
        ]
        links = [
            *_implicit_links(hosts),
            _link(
                ("workers_w1", "eth0", "10.20.2.21"),
                ("db-01", "eth0", "10.20.3.31"),
                name="app-db",
                impair="edge-gw",
            ),  # impairment added
        ]
        return LabSnapshot(hosts=hosts, links=links)

    meta = _meta(_HOST_CHARTS[:2], tables=False)
    sessions = []
    for sid, label, start, lab in (
        (
            "2026-03-01T08-00-00-baseline",
            "baseline",
            datetime(2026, 3, 1, 8, tzinfo=timezone.utc),
            lab_v1(),
        ),
        (
            "2026-05-01T08-00-00-expanded",
            "expanded",
            datetime(2026, 5, 1, 8, tzinfo=timezone.utc),
            lab_v2(),
        ),
        (
            "2026-07-01T08-00-00-rewired",
            "rewired",
            datetime(2026, 7, 1, 8, tzinfo=timezone.utc),
            lab_v3(),
        ),
    ):
        metrics: list[MetricRecord] = []
        for h in lab.hosts:
            metrics += _series(
                rng,
                h.id,
                "CPU %",
                _diurnal(rng, base=30.0, amp=15.0),
                start=start,
                duration_s=1200.0,
                cadence_s=30.0,
            )
            metrics += _series(
                rng,
                h.id,
                "Memory MB",
                _sawtooth(lo=800.0, hi=2400.0, period_s=900.0),
                start=start,
                duration_s=1200.0,
                cadence_s=30.0,
            )
        sessions.append(
            SessionRecord(
                id=sid,
                label=label,
                start=start,
                end=start + timedelta(seconds=1200.0),
                lab=lab,
                meta=meta,
                metrics=metrics,
                chart_map=_chart_map(meta),
            )
        )
    return MonitorExport(format=1, sessions=sessions)


# --- output ----------------------------------------------------------------------


def dumps(doc: MonitorExport) -> str:
    """Minified JSON + trailing newline — the committed on-disk form."""
    return (
        json.dumps(
            doc.model_dump(mode="json", exclude_none=True),
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    )


def build_all() -> dict[str, MonitorExport]:
    """All fixture documents, keyed by file stem."""
    return {
        "kitchen-sink": kitchen_sink(),
        "minimal": minimal(),
        "drift": drift(),
        "cascade": cascade(),
        "sprawl": sprawl(),
        "isp-core": isp_core(),
    }


def main(out_dir: str) -> None:
    """Write every fixture to ``<out_dir>/<stem>.json``."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for stem, doc in build_all().items():
        (out / f"{stem}.json").write_text(dumps(doc), encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "web/fixtures")
