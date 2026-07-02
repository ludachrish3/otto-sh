"""Per-run heap-leak canary for SNMP-wired embedded targets.

The zephyr bed degraded over WEEKS before anything failed: Zephyr 3.7's
fs-shell mount leak drained the 16 KB system heap ~16 bytes per otto host
object, and the first symptom was an unrelated SNMP assertion days later.
This canary catches that CLASS of leak within a single run: perform the
representative console workload with a FRESH host object per iteration —
the leak unit — and assert the device's heap-used watermark does not move
between iterations.

A warm-up pass absorbs legitimate one-time allocations (e.g. the first real
FAT mount k_mallocs the mount-point string, owned by the mount while it
stays mounted); the steady-state delta must then be exactly zero. The
LittleFS instances demonstrated over 5+ days that otto's exercised surface
allocates nothing per operation, so zero tolerance is realistic — loosen
only with a measured justification.
"""

from datetime import timedelta
from pathlib import Path

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

# Only SNMP-wired backends carry the heap OIDs this canary reads.
SNMP_BACKENDS = [b for b in EMBEDDED_BACKENDS if "snmp" in host_data(_BACKEND_NE[b])]

pytestmark = pytest.mark.timeout(120)


async def _heap_used(ne_name: str) -> float:
    """Read the device's current heap-used bytes over SNMP (read-only)."""
    host = create_host_from_dict(host_data(ne_name))
    collector = build_monitor_collector([host])
    try:
        await collector.run(interval=timedelta(seconds=5), duration=timedelta(seconds=0))
        series = {
            key.split("/", 1)[1]: points[-1].value
            for key, points in collector.get_series().items()
            if points and "/" in key
        }
        return series["Heap Used"]
    finally:
        await collector.close()


async def _workload(ne_name: str, tmp_path: Path, tag: int) -> None:
    """One representative console workload on a FRESH host object (the leak unit)."""
    host = create_host_from_dict(host_data(ne_name))
    try:
        res = (await host.run("kernel version", timeout=15)).only
        assert "Zephyr" in (res.value or ""), f"console not healthy on {ne_name}"

        src = tmp_path / f"canary_{tag}.bin"
        src.write_bytes(b"heap-canary")
        dest_dir = host.default_dest_dir
        put = await host.put(src_files=[src], dest_dir=dest_dir)
        assert put.is_ok, f"put failed on {ne_name}: {put.msg}"
        pulled = tmp_path / f"pulled_{tag}"
        pulled.mkdir()
        got = await host.get(src_files=[dest_dir / src.name], dest_dir=pulled)
        assert got.is_ok, f"get failed on {ne_name}: {got.msg}"
        await host.run(f"fs rm {dest_dir / src.name}", timeout=15)
    finally:
        await host.close()


@pytest.mark.parametrize("backend", SNMP_BACKENDS, ids=embedded_param_id)
class TestHeapWatermark:
    @pytest.mark.asyncio
    async def test_console_workload_does_not_leak_heap(self, backend, tmp_path):
        ne = _BACKEND_NE[backend]

        # Warm-up: absorb one-time allocations (first mount, lazy init).
        await _workload(ne, tmp_path, 0)
        baseline = await _heap_used(ne)

        # Steady state: three more passes, each with a fresh host object.
        for i in range(1, 4):
            await _workload(ne, tmp_path, i)
        after = await _heap_used(ne)

        assert after == baseline, (
            f"{ne}: heap-used watermark moved {baseline:.0f} -> {after:.0f} across "
            f"3 fresh-host workloads — something allocates per host object and "
            f"never frees (the class of bug behind the Zephyr 3.7 fs-mount leak)"
        )
