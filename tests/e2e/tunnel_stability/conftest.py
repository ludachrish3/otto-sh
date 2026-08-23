"""Package-wide bed hygiene (spec §1): reap + sweep + watermark, always on."""

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio

from otto.config.lab import Lab
from otto.host.connections import teardown_step
from otto.tunnel import remove_tunnel
from tests._fixtures.fd_watermark import (
    _fd_watermark,  # noqa: F401 — imported fixture, registered by name
)
from tests._fixtures.labdata import host_data
from tests._fixtures.tunnel_bed import (
    UNIX,
    assert_bed_clean_before_module,
    assert_no_leftover_tunnel_processes,
    assert_reachable,
    build_bed_host,
)


@pytest_asyncio.fixture
async def tunnel_lab():
    """Real ``Lab`` over the 3-VM unix bed; host-down fails LOUD, never skips."""
    for ne in UNIX:
        await assert_reachable(ne, host_data(ne)["ip"])
    lab = Lab(name="tunnel_stability")
    for ne in UNIX:
        lab.add_host(build_bed_host(ne))
    yield lab
    await asyncio.gather(*(h.close() for h in lab.hosts.values()), return_exceptions=True)


@pytest_asyncio.fixture
async def reap_tunnels(tunnel_lab):
    """Guaranteed teardown: reap every tunnel this test created, even on failure.

    A raising ``remove_tunnel`` here is a product defect in the exact path this
    suite soaks, so the reap logs it (``teardown_step``, the house teardown
    shape) instead of swallowing it: the module's leftover sweep would still
    catch the CONSEQUENCE, but mis-attributed to the module with the original
    exception gone (review §5.5). Reaping continues past a failed id — the
    remaining tunnels still deserve their teardown.
    """
    created: list[str] = []
    yield created
    for tunnel_id in created:
        with teardown_step(f"tunnel_stability reap {tunnel_id}", "remove_tunnel"):
            await remove_tunnel(tunnel_lab, tunnel_id)


@pytest.fixture(scope="module", autouse=True)
def _final_leftover_sweep(request):
    """Bed hygiene bracketing each module: clean going in, clean coming out.

    The setup half proves the bed was clean before this module ran, which is
    what lets the final sweep blame *this* module rather than merely reporting
    that tagged processes exist somewhere on a shared bed (see
    test_tunnel_e2e.py's copy for the 2026-07-21 misattribution this fixes).
    It also fails fast: a soak module is minutes of bed time to spend against
    state someone else left behind. Sync fixture with its own asyncio.run — it
    fires after every per-test event loop has closed."""
    module_id = f"tests/e2e/tunnel_stability/{Path(request.node.path).name}"
    asyncio.run(assert_bed_clean_before_module(module_id))
    yield
    asyncio.run(assert_no_leftover_tunnel_processes(module_id))
