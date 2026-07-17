"""Package-wide bed hygiene (spec §1): reap + sweep + watermark, always on."""

import asyncio
import contextlib
import gc
from pathlib import Path

import pytest
import pytest_asyncio

from otto.config.lab import Lab
from otto.tunnel import remove_tunnel
from tests._fixtures.labdata import host_data
from tests._fixtures.tunnel_bed import (
    VEGGIES,
    assert_no_leftover_tunnel_processes,
    assert_reachable,
    build_bed_host,
)

_FD_TOLERANCE = 4


def _open_fds() -> int:
    return len(list(Path("/proc/self/fd").iterdir()))


@pytest.fixture(autouse=True)
def _fd_watermark():
    """Local-side leak bracket: the process's open-FD count must return to
    baseline (±tolerance) once the lab fixture has closed every host. Autouse
    and dependency-free, so pytest instantiates it BEFORE (and finalizes it
    AFTER) `tunnel_lab` — the bracket wraps the hosts' whole lifetime. One
    gc pass absorbs collector timing before the verdict."""
    gc.collect()
    before = _open_fds()
    yield
    gc.collect()
    after = _open_fds()
    if after > before + _FD_TOLERANCE:
        gc.collect()
        after = _open_fds()
    assert after <= before + _FD_TOLERANCE, (
        f"local fd leak across test: {before} -> {after} open fds"
    )


@pytest_asyncio.fixture
async def tunnel_lab():
    """Real ``Lab`` over the 3-VM veggies bed; host-down fails LOUD, never skips."""
    for ne in VEGGIES:
        await assert_reachable(ne, host_data(ne)["ip"])
    lab = Lab(name="tunnel_stability")
    for ne in VEGGIES:
        lab.add_host(build_bed_host(ne))
    yield lab
    await asyncio.gather(*(h.close() for h in lab.hosts.values()), return_exceptions=True)


@pytest_asyncio.fixture
async def reap_tunnels(tunnel_lab):
    """Guaranteed teardown: reap every tunnel this test created, even on failure."""
    created: list[str] = []
    yield created
    for tunnel_id in created:
        with contextlib.suppress(Exception):
            await remove_tunnel(tunnel_lab, tunnel_id)


@pytest.fixture(scope="module", autouse=True)
def _final_leftover_sweep():
    """Module-final bed hygiene: FAIL (never skip) if any tagged process
    survived. Sync fixture with its own asyncio.run — it fires after every
    per-test event loop has closed (same pattern as test_tunnel_e2e.py)."""
    yield
    asyncio.run(assert_no_leftover_tunnel_processes())
