"""tests/e2e/chaos — tier-3 chaos lane fixtures.

Session-scoped bed lease: the whole lane runs against ONE leased veggies
host (plus named peers in the inherently two-host scenarios), serialized by
xdist_group("chaos_lane"). Fail-loud on host-down, never skip.
"""

from collections.abc import Iterator

import pytest

from tests._fixtures.fd_watermark import (
    _fd_watermark,  # noqa: F401 — imported fixture, registered by name
)
from tests.e2e.chaos._bed import ChaosBed, leased_bed


@pytest.fixture(scope="session")
def chaos_bed(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ChaosBed]:
    lock_dir = tmp_path_factory.getbasetemp().parent
    with leased_bed(lock_dir) as bed:
        yield bed


def _hygiene_bracket_impl(request):
    """Snapshot/diff the leased host around EVERY scenario (spec: BedHygiene).

    Sync fixture with its own asyncio.run per side, over a fresh probe host
    each time — the scenario's own connections are dead by design when the
    after-side runs. Scenarios that dirty a PEER (tunnel/link/reboot tasks)
    add their own peer-side brackets; this fixture owns the leased host only.
    Opt out (reboot module's mid-reboot cases where the after-probe would
    race the boot) with @pytest.mark.no_hygiene_bracket + a manual bracket.

    The bed lease is requested LAZILY (getfixturevalue after the opt-out
    check) so `no_hygiene_bracket` tests — the embedded console module and
    the docker module, which on the GitHub loopback venue has no bed route
    at all — never instantiate the session lease.
    """
    import asyncio

    from tests._fixtures.bed_hygiene import (
        diff_snapshots,
        format_hygiene_report,
        snapshot_host,
    )
    from tests.e2e.chaos._bed import probe_host

    if request.node.get_closest_marker("no_hygiene_bracket"):
        yield
        return

    chaos_bed = request.getfixturevalue("chaos_bed")

    async def _snap():
        async with probe_host(chaos_bed.element) as host:
            return await snapshot_host(host)

    before = asyncio.run(_snap())
    yield
    after = asyncio.run(_snap())
    leftovers = diff_snapshots(before, after)
    assert not leftovers, format_hygiene_report(chaos_bed.element, leftovers)


@pytest.fixture(autouse=True)
def _bed_hygiene_bracket(request):
    yield from _hygiene_bracket_impl(request)


@pytest.fixture
def chaos_rng():
    """Per-test seeded RNG; the printed seed is the reproduce handle."""
    import random as _random

    from tests.e2e.chaos._seed import resolve_seed

    seed = resolve_seed()
    print(f"\nchaos seed: {seed} (reproduce with OTTO_CHAOS_SEED={seed})")  # noqa: T201 — the printed seed is the reproduce handle; captured output surfaces it on failure
    return _random.Random(seed)  # noqa: S311 — reproducibility, not security (seeded PRNG for chaos offsets)
