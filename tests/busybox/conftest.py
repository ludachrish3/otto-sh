"""The BusyBox matrix tiers: hermetic, and stamped `busybox` by location.

This tree is the home for every tier that drives real BusyBox artifacts —
Tier 1 (dash-driven applet contracts) today, Tiers 2 and 3 (rootless rootfs,
rootless dropbear) later. It is deliberately NOT under ``tests/integration/``,
whose conftest defines that tree as tests that "drive the real Vagrant/QEMU bed
via otto's Python API". These drive local subprocesses and need no bed.

That is a lane decision, not a filing preference, and it was made from
measurement. While the tier briefly lived under ``tests/integration/`` it
inherited two session-autouse fixtures, one irrelevant and one harmful: the
docker-orphan sweep SSHed to the lab host and deleted containers by name
fragment on every run of a lane advertised as needing no bed (the first run
logged ``Peer address: 10.10.200.13`` and ``docker rm -f``). It also inherited
the ``integration`` auto-stamp, which put a busybox.net fetch inside
``make coverage-unix``. Overriding the one fixture that hurt would have been
inherit-then-override — it fixes the fixture that exists and silently accepts
every bed-wide fixture added later.

The stamp below is the mirror image of that lesson. Every test here needs the
``busybox`` marker, because that marker is what keeps the tier out of the
catch-all lanes (see the note above ``M_HOSTLESS`` in the Makefile): a new file
that forgot it would quietly start fetching ~5 MB from busybox.net in CI's
ordinary gates and fail them hard whenever upstream blinked. Stamping by
directory closes that by construction instead of by everyone remembering.
Tests still carry their own explicit ``@pytest.mark.busybox``, and those marks
are LOAD-BEARING — do not "simplify" them away as redundant with this stamp.
That is enforced, not merely asked for: G9b in
``tests/unit/test_tier_marker_invariants.py`` reds if any test function here
stops declaring the marker. It was an unenforced request first, and deleting
all three decorators left the tier green at 13 passed.

Three reasons, in order of how expensive they are to rediscover:

1. This hook's effect depends on collection-hook ORDER, which in this repo
   varies with the invocation shape (a directory-targeted run makes this an
   *initial* conftest). The stamp was measured working in six shapes, but that
   is evidence, not a contract; the decorator is shape-independent, so it is
   the layer that still holds in a shape nobody measured.
2. The stamp cannot survive its own file. Delete or rename this conftest and
   every test here silently rejoins the catch-all lanes — with the decorators
   present, they do not.
3. A test should say what it needs where it is written, and grep-shaped
   tooling should be able to find the tier.

Pinned by G9 in tests/unit/test_tier_marker_invariants.py.
"""

from pathlib import Path

_BUSYBOX_ROOT = Path(__file__).parent


def pytest_collection_modifyitems(config, items):
    """Auto-apply the ``busybox`` marker to every test under this tree.

    Idempotent and additive: an item that already declares the marker is
    unharmed, and any other marker it carries survives.
    """
    for item in items:
        if _BUSYBOX_ROOT in item.path.parents:
            item.add_marker("busybox")
