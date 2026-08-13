"""The BusyBox matrix tiers: hermetic, and stamped `busybox` by location.

This tree is the home for every tier that drives real BusyBox artifacts —
Tier 1 (dash-driven applet contracts), Tier 2 (a rootless BusyBox-only root)
and Tier 3 (a rootless loopback dropbear logging into that root). It is
deliberately NOT under ``tests/integration/``, whose conftest defines that
tree as tests that "drive the real Vagrant/QEMU bed via otto's Python API".
These drive local subprocesses and need no bed.

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

TIER 3 CARRIES A SECOND DECLARED MARKER, for a reason from the same family.
``xdist_group("busybox_tier3")`` keeps the whole dropbear tier sequential on
one worker: dropbear serves only five simultaneous PRE-AUTH connections per IP
(measured; loopback is one IP) and resets the sixth in ~0.0003s with no server
log line at all. That is also a marker this conftest declines to stamp, and
the reason is stated at :func:`pytest_collection_modifyitems` — unlike the
``busybox`` marker, an ``xdist_group`` stamp's effect depends on when this
hook runs relative to xdist's, which depends on the invocation shape. The
declaration is the mechanism; :func:`_unhonored_tier3_group` is the proof it
worked.
"""

from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from tests._fixtures.busybox import BUSYBOX_MATRIX, require_interpreter
from tests._fixtures.busybox_dropbear import LoopbackDropbear, free_port, require_dropbear
from tests._fixtures.busybox_rootfs import busybox_rootfs, require_userns

_BUSYBOX_ROOT = Path(__file__).parent

TIER3_GROUP = "busybox_tier3"
"""The xdist group every Tier 3 module declares. Read by the guard below.

Named here rather than repeated as a literal so the guard and the modules it
polices cannot drift apart — the failure that would produce is a guard which
passes because it is looking for a group nobody claims.
"""

_TIER3_FIXTURE = "tier3_dropbear"
"""The fixture whose users must be pinned. Membership is by USE, not by name.

Keyed on the fixture rather than on the file, because the property being
protected belongs to the daemon: any module that reaches it — Task 3's
session contracts, Task 4's transfer round trip, anything later — inherits
the pin requirement automatically, with nothing to remember.
"""

TIER3_RELEASE = BUSYBOX_MATRIX[-1]
"""The newest matrix row. Tier 3 asks a TRANSPORT question, not a matrix one.

Public for the same reason as :data:`TIER3_GROUP`: a contract test that pins
the banner version has to name the release the fixture actually serves, and a
second `BUSYBOX_MATRIX[-1]` written at the assertion site would keep passing
its own copy of the question. See
`tests/busybox/test_tier3_session.py`'s pinned-release proof.

Tiers 1 and 2 sweep all five releases because what they measure — applet
behaviour, codec round trips — differs per release. What Tier 3 adds is the
ssh channel between otto and that userland, which is the same channel for
every row, so sweeping here would multiply the tier's cost by five and
measure the transport five times. A release-specific finding belongs in Tier
2, where it is cheap.
"""


@pytest.fixture(scope="session", autouse=True)
def _interpreter():
    """Fail this whole tree — loudly, once, by name — when x86 artifacts cannot run.

    Every tier here drives real x86 BusyBox binaries, so a host without the
    matching qemu-user-static handler cannot run ANY of them. The binding rule
    is that unavailability is named, never skipped: a skipped BusyBox tier and
    a passing one are the same line in a summary, and that is how the coverage
    evaporates without anyone noticing.

    This lives in the conftest rather than in a test module because a
    module-local copy binds only its own file. The rootfs tier was written with
    a module-level ``pytest.mark.skipif`` instead and measured at *11 skipped*
    where the applet tier gave *18 errors* with apt instructions — same missing
    dependency, opposite verdicts, and the silent one is in the tier whose
    absence is hardest to spot. A collection-time ``skipif`` was the wrong
    instrument twice over: it also took down the tests that need no interpreter
    at all (the error-message guards), and on an x86_64 runner ``can_run``
    short-circuits to True, so it could only ever fire on the dev VM.

    Session-scoped and argument-less, so it covers every arch the matrix
    declares and reports all missing handlers in one message rather than five
    identical ENOEXECs.
    """
    require_interpreter()


@pytest.fixture(scope="session")
def tier3_dropbear() -> "Iterator[LoopbackDropbear]":
    """One rootless dropbear on loopback, logging into one BusyBox root.

    SESSION-SCOPED, and every term of that is deliberate.

    *Session* rather than module: the daemon and the root it serves cost
    ~0.3s to build and are identical for every consumer, while a
    module-scoped fixture would rebuild both for each Tier 3 file and, under
    ``-n auto``, once per worker on top of that. Session scope also moves the
    whole cost into fixture SETUP, which ``timeout_func_only = true`` puts
    outside the per-test SIGALRM — see the Tier 3 note in the coupled-budget
    block at the top of ``busybox_rootfs.py``.

    *One* rather than one per matrix row: see :data:`TIER3_RELEASE`.

    The refusals come first and in dependency order — interpreter (via this
    tree's autouse fixture), namespace, dropbear — so a machine missing a
    prerequisite is told which one by name instead of watching a daemon fail
    to start. Nothing skips.

    Teardown reaps the daemon by captured pid inside a ``finally``, and the
    kernel holds the other end of that promise via ``PR_SET_PDEATHSIG`` for
    the SIGKILLed-worker case no finalizer can cover.
    """
    require_userns()
    require_dropbear()
    with busybox_rootfs(TIER3_RELEASE) as root, TemporaryDirectory(prefix="otto-bbdrop-") as td:
        daemon = LoopbackDropbear.build(Path(td), root)
        daemon.start(free_port())
        try:
            yield daemon
        finally:
            daemon.stop()


def _unhonored_tier3_group(item: pytest.Item) -> "str | None":
    """The group a Tier 3 item claimed but xdist never applied, or None if fine.

    Lifted in shape from ``tests/integration/host/conftest.py``'s
    ``_unhonored_group``, for the same reason and against a different hazard.
    THE CITATION COVERS THIS DETECTION ONLY, not how the two report. That
    conftest calls ``pytest.fail(..., pytrace=False)``;
    :func:`pytest_runtest_setup` below raises ``pytest.UsageError``. Measured
    2026-08-13, because the names invite the wrong assumption: a
    ``UsageError`` raised from ``pytest_runtest_setup`` does NOT abort the
    session. pytest reports it as a per-item ERROR, with a conftest traceback
    attached, and runs the remaining items — so against the precedent this
    spelling is noisier rather than more decisive, ``pytrace=False`` being
    exactly what suppresses that traceback. Matching the precedent is the
    open improvement; it is a behaviour change and has not been made.
    THE MARKER BEING PRESENT PROVES NOTHING: xdist reads it in its own
    ``pytest_collection_modifyitems`` and what it leaves behind when it acts
    is the ``@group`` suffix it appends to the nodeid. That suffix is the
    honest evidence; the marker only proves we asked.

    What goes wrong without the pin is not a slow run, it is a misdiagnosis.
    Scattered across workers, each worker builds its own daemon and its own
    root — and the moment anything makes them share one (a fixed port, a
    session-shared root, a fan-out test), the sixth simultaneous pre-auth
    connection is reset in ~0.0003s with NO server log line, which reaches the
    author as a bare ``ConnectionLost`` identical to the one an ed25519-only
    host key produces. See ``MAX_UNAUTH_PER_IP``.

    Checked at setup, so it fires before the daemon fixture runs and costs
    nothing when it is right.
    """
    if getattr(item.config, "workerinput", None) is None:
        return None  # not under xdist — one process, nothing to schedule
    if _TIER3_FIXTURE not in getattr(item, "fixturenames", ()):
        return None  # does not touch the daemon
    return None if item.nodeid.endswith(f"@{TIER3_GROUP}") else TIER3_GROUP


def pytest_runtest_setup(item):
    """Fail a Tier 3 test whose xdist group was declared but never honoured."""
    unhonored = _unhonored_tier3_group(item)
    if unhonored is not None:
        raise pytest.UsageError(
            f"{item.nodeid} uses the `{_TIER3_FIXTURE}` fixture but xdist did not place "
            f"it in the `{unhonored}` group — its nodeid carries no `@{unhonored}` suffix. "
            f"Declare `pytest.mark.xdist_group({unhonored!r})` in the module's "
            f"`pytestmark`, and check that the run uses `--dist loadgroup`. Unpinned, "
            f"Tier 3 builds a daemon and a root per worker, and any daemon two workers "
            f"share silently resets the 6th concurrent pre-auth connection with no log "
            f"line at all."
        )


def pytest_collection_modifyitems(config, items):
    """Auto-apply the ``busybox`` marker to every test under this tree.

    Idempotent and additive: an item that already declares the marker is
    unharmed, and any other marker it carries survives.

    DELIBERATELY DOES NOT STAMP ``xdist_group``. It could not be trusted to:
    this hook's position relative to xdist's own
    ``pytest_collection_modifyitems`` depends on the INVOCATION SHAPE (naming
    this directory on the command line makes this an *initial* conftest, which
    registers early and therefore runs LATE, after xdist has already read the
    markers), so a group stamped here would be honoured under ``make busybox``
    and silently inert under ``pytest tests/busybox`` — see
    ``tests/conftest.py``'s note on the same trap. A ``pytestmark`` in the
    module is attached at item construction, before any collection hook runs,
    so it is shape-independent. The declaration is the mechanism;
    :func:`_unhonored_tier3_group` is the proof it worked.
    """
    for item in items:
        if _BUSYBOX_ROOT in item.path.parents:
            item.add_marker("busybox")
