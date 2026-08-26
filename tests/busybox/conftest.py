"""The BusyBox artifact tier: hermetic, and stamped `busybox` by location.

This tree drives real BusyBox artifacts through argv-level contracts — the
applet spellings otto's userland resolver depends on, measured against the
binaries themselves. It is deliberately NOT under ``tests/integration/``,
whose conftest defines that tree as tests that "drive the real Vagrant/QEMU
bed via otto's Python API". These drive local subprocesses and need no bed.

THE BEHAVIOUR TIER MOVED OUT, AND THIS ONE STAYED. The chroot-and-dropbear
harness that once shared this directory is gone; what a BusyBox userland does
under otto is now measured on five live QEMU guests in
``tests/integration/busybox_bed/``, against a real kernel and a real
transport. What could not move is what remains here: a question about the
ARTIFACTS — is the pinned binary the version it claims, does this build spell
this applet this way — which needs the downloaded file and no device at all.

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
the decorators left the tier green — the directory stamp covered for them,
which is exactly the redundancy that makes the marks look like noise.

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

import pytest

from tests._fixtures.busybox import require_interpreter

_BUSYBOX_ROOT = Path(__file__).parent


@pytest.fixture(scope="session", autouse=True)
def _interpreter():
    """Fail this whole tree — loudly, once, by name — when x86 artifacts cannot run.

    Every test here drives real x86 BusyBox binaries, so a host without the
    matching qemu-user-static handler cannot run ANY of them. The binding rule
    is that unavailability is named, never skipped: a skipped BusyBox tier and
    a passing one are the same line in a summary, and that is how the coverage
    evaporates without anyone noticing.

    This lives in the conftest rather than in a test module because a
    module-local copy binds only its own file. The since-retired rootfs tier
    was written with a module-level ``pytest.mark.skipif`` instead and measured
    at *11 skipped* where the applet tier gave *18 errors* with apt
    instructions — same missing dependency, opposite verdicts, and the silent
    one is in the tier whose absence is hardest to spot. A collection-time
    ``skipif`` was the wrong instrument twice over: it also took down the tests
    that need no interpreter at all (the error-message guards), and on an
    x86_64 runner ``can_run`` short-circuits to True, so it could only ever
    fire on the dev VM.

    Session-scoped and argument-less, so it covers every arch the matrix
    declares and reports all missing handlers in one message rather than five
    identical ENOEXECs.
    """
    require_interpreter()


def pytest_collection_modifyitems(config, items):
    """Auto-apply the ``busybox`` marker to every test under this tree.

    Idempotent and additive: an item that already declares the marker is
    unharmed, and any other marker it carries survives.

    DELIBERATELY STAMPS NOTHING ELSE, and ``xdist_group`` is the one it would
    be tempting to add. It could not be trusted to: this hook's position
    relative to xdist's own ``pytest_collection_modifyitems`` depends on the
    INVOCATION SHAPE (naming this directory on the command line makes this an
    *initial* conftest, which registers early and therefore runs LATE, after
    xdist has already read the markers), so a group stamped here would be
    honoured under ``make busybox`` and silently inert under
    ``pytest tests/busybox`` — see ``tests/conftest.py``'s note on the same
    trap. A ``pytestmark`` in the module is attached at item construction,
    before any collection hook runs, so it is shape-independent. That is why
    the live bed's own conftest declares its group ``tryfirst`` rather than
    relying on a stamp like this one.
    """
    for item in items:
        if _BUSYBOX_ROOT in item.path.parents:
            item.add_marker("busybox")


# No source-reachability hook here, on purpose. One lived at
# ``pytest_collection_finish`` guarded on ``session.items`` — which is the whole
# session's list, so a run with every busybox test deselected and 7,000 unit
# tests remaining still probed the network, and ``--collect-only`` (which
# collects items and executes none) fired it too (issue #264). The one-probe
# bound now lives in ``tests._fixtures.busybox.busybox_binary``, the consumer,
# where it fires exactly when an artifact is about to be fetched.
