"""
Live SNMP-monitoring integration tests.

Exercises otto's full SNMP collection path end-to-end against the running
Zephyr test bed: ``build_monitor_collector`` builds an SNMP ``MonitorTarget``
from each agent-capable host's ``snmp`` lab block, ``MetricCollector`` polls it
over the per-instance socat relay on the zephyr VM (10.10.200.14:161NN ->
192.0.2.x:161), and the OID values are decoded + scaled through the descriptor
registry into chart series.

Scope: only the backends whose lab entry declares an ``snmp`` block — i.e. the
3.7 / 4.x instances, where the agent is built. The 2.7 instances run with the
agent off (the agent C targets the 3.0+ include layout), so they carry no
``snmp`` block and are excluded automatically by deriving the parametrization
from the lab data.

Carries both ``integration`` and ``embedded`` markers (opted into via
``pytest -m embedded``) since it needs the live QEMU instances + relays.
"""

from datetime import timedelta

import pytest

from otto.monitor.factory import build_monitor_collector
from otto.storage.factory import create_host_from_dict
from tests.conftest import (
    _ZEPHYR_BACKEND_NE as _BACKEND_NE,
)
from tests.conftest import (
    EMBEDDED_BACKENDS,
    embedded_param_id,
    host_data,
)

# Only the SNMP-wired backends (3.7 / 4.x). Derived from the lab data so a new
# host gaining (or a 2.7 host never getting) an ``snmp`` block flows through
# without editing this file.
SNMP_BACKENDS = [b for b in EMBEDDED_BACKENDS if "snmp" in host_data(_BACKEND_NE[b])]

_SNMP_PARAMS = pytest.mark.parametrize(
    "backend",
    [
        pytest.param(
            b,
            marks=[pytest.mark.embedded],
            id=embedded_param_id(b),
        )
        for b in SNMP_BACKENDS
    ],
)

pytestmark = pytest.mark.timeout(30)


def _series_by_label(collector) -> dict[str, float]:
    """Latest value per metric label, stripping the ``hostname/`` key prefix."""
    out: dict[str, float] = {}
    for key, points in collector.get_series().items():
        if "/" in key and points:
            out[key.split("/", 1)[1]] = points[-1].value
    return out


@_SNMP_PARAMS
class TestSnmpCollection:
    @pytest.mark.asyncio
    async def test_collects_sane_metrics_over_snmp(self, backend):
        """A single collection tick yields decodable, in-range metrics.

        Guards the whole stack: lab ``snmp`` block -> SnmpOptions ->
        SnmpClient/SnmpSource -> live agent over the relay -> BER decode ->
        descriptor scaling -> chart series.
        """
        host = create_host_from_dict(host_data(_BACKEND_NE[backend]))
        assert host.snmp is not None, "backend should be SNMP-wired"

        collector = build_monitor_collector([host])
        try:
            # duration=0 runs only the immediate initial collection (one GET
            # per host) — deterministic and fast, no inter-tick sleep. interval
            # is the per-tick GET budget.
            await collector.run(
                interval=timedelta(seconds=5),
                duration=timedelta(seconds=0),
            )
            metrics = _series_by_label(collector)
        finally:
            await collector.close()

        # The agent answered and every value decoded through the registry.
        assert metrics, f"no SNMP series collected from {host.name}"

        assert "Uptime" in metrics
        assert metrics["Uptime"] >= 0.0

        # CPU is scaled centi-percent -> percent; an idle shell sits low but
        # the load-bearing check is simply that it's a real percentage.
        assert "Overall CPU" in metrics
        assert 0.0 <= metrics["Overall CPU"] <= 100.0

        # k_thread_foreach counts at least the shell + idle + agent threads.
        assert "Threads" in metrics
        assert metrics["Threads"] >= 1.0

        # Heap free comes from the system heap pool (16 KB on these builds).
        # Heap used may legitimately be 0 (the k_malloc heap is barely touched),
        # so only assert free is positive.
        assert "Heap Free" in metrics
        assert metrics["Heap Free"] > 0.0

    @pytest.mark.asyncio
    async def test_uptime_advances_between_polls(self, backend):
        """Live sysUpTime advances: two reads a beat apart differ, not constant."""
        import asyncio

        host = create_host_from_dict(host_data(_BACKEND_NE[backend]))
        client_src = build_monitor_collector([host])._targets[0].snmp
        assert client_src is not None
        try:
            first = await client_src.client.get([client_src.oids[0]])
            await asyncio.sleep(1.5)
            second = await client_src.client.get([client_src.oids[0]])
        finally:
            pass

        oid = client_src.oids[0]  # sysUpTime
        assert first[oid] is not None and second[oid] is not None
        assert second[oid] >= first[oid]
        assert second[oid] > first[oid], "uptime should advance between reads"
