"""Leased live-bed target for the tier-3 chaos lane.

The lane's spine: lease ONE free veggies host (flock, cross-worker), prove it
reachable (fail LOUD, host-named, never skip), and expose it both as a
``ChaosTarget`` (for the otto subprocess under test) and as fresh probe
``UnixHost``s (for oracle exec over an independent connection). The tier-2
driver (`tests/integration/chaos/_driver.py`) is reused unchanged — signals
only ever go to the LOCAL otto subprocess; the bed host just runs its remote
commands.
"""

import asyncio
import contextlib
import dataclasses
from collections.abc import Iterator
from pathlib import Path

from tests._fixtures._host_pool import lease_unix_host
from tests._fixtures.labdata import host_data
from tests._fixtures.tunnel_bed import assert_reachable, build_bed_host
from tests.integration.chaos._target import ChaosTarget, make_bed_target


@dataclasses.dataclass(frozen=True)
class ChaosBed:
    element: str  # lab element name, e.g. "tomato"
    ip: str  # management ip from tech1/lab.json
    target: ChaosTarget  # aim the otto subprocess here


@contextlib.contextmanager
def leased_bed(lock_dir: Path) -> Iterator[ChaosBed]:
    """Lease a free veggies host; probe reachability; yield the bed handle."""
    with lease_unix_host(lock_dir) as element:
        ip = host_data(element)["ip"]
        asyncio.run(assert_reachable(element, ip))
        yield ChaosBed(element=element, ip=ip, target=make_bed_target(element))


@contextlib.asynccontextmanager
async def probe_host(element: str):
    """Fresh, independent ``UnixHost`` for oracle exec; always closed."""
    host = build_bed_host(element)
    try:
        yield host
    finally:
        await host.close()


def run_probe(element: str, coro_factory):
    """Run ``await coro_factory(host)`` on a fresh probe host in a fresh loop.

    Sync bridge for subprocess-driving (sync) scenario tests — mirrors the
    tier-2 suite's ``probe()`` shape but with a full UnixHost, so oracles can
    use ``exec``/``run`` semantics (sudo, timeouts, QUIET logging) instead of
    raw asyncssh.
    """

    async def _go():
        async with probe_host(element) as host:
            return await coro_factory(host)

    return asyncio.run(_go())
