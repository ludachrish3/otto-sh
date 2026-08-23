"""
Fixtures local to tests/integration/host/.

The parametrized ``host1`` / ``host1_kit`` fixtures live in
:mod:`tests.conftest` (shared with the unit tree). This conftest exists
only to populate the lab into an OttoContext so the embedded hosts'
hop resolution (``config.get_host('test4')`` inside
``RemoteHost._build_hop_transport``) can find the SSH hop.

The same wiring is done in :mod:`tests.unit.host.test_hop_integration` for
multi-hop UnixHost tests.
"""

import shutil
from collections.abc import Iterator

import pytest

from otto.config.lab import Lab
from otto.context import OttoContext, set_context
from otto.host.command_frame import FRAME_CLASSES, register_command_frame
from otto.host.login_proxy import Cred
from otto.host.telnet import abort_console_transports
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode

# Private, but the lab tooling's — and already imported this way by
# tests/unit/scripts/test_lab_health.py. Reused so the hop-id index and the
# password-SSH invocation exist once for the whole repo.
from scripts.lab_health import _hop_index, _load_hosts, _run_ssh, _ssh_user_pass
from tests._fixtures._console_lock import console_access
from tests._fixtures.fd_watermark import fd_watermark_bracket
from tests._fixtures.labdata import lab_data_path

# Make repo1's custom Zephyr 2.7 dialect resolvable by the storage factory.
#
# The embedded integration tests build hosts via ``create_host_from_dict``
# directly (the raw factory path), which — unlike a full ``otto`` config load —
# does not import the SUT repo's init modules, so the ``"zephyr-inline"`` frame
# the 2.7 lab entries declare would be unregistered. Register it here by
# importing the shared ``custom_hosts`` module (the third-party-style package
# SUT repos depend on for this frame), adding its dir to the path the way
# ``Repo.add_libs_to_pythonpath`` does at config-load time.
from tests._fixtures.paths import ensure_custom_hosts_on_path
from tests.conftest import (
    _ZEPHYR_BACKEND_NE,
    BUSYBOX_BED_GROUP,
    BUSYBOX_PARAM_TOKENS,
    EMBEDDED_BACKENDS,
    embedded_param_id,
    host_data,
)

ensure_custom_hosts_on_path()
from custom_hosts.zephyr_inline import ZephyrInlineRetcodeFrame

# custom_hosts/__init__.py (tests/custom_hosts/custom_hosts/__init__.py) already
# registers this exact class as an import-time side effect of the line above —
# importing the ``custom_hosts.zephyr_inline`` submodule runs the parent
# package's ``__init__.py`` first. Only register here if that didn't happen
# (e.g. this conftest loads before custom_hosts' own __init__ has run in some
# import order), so both load orders work and neither trips the registry's
# loud-duplicate guard for what is the identical class object.
if ZephyrInlineRetcodeFrame.type_name not in FRAME_CLASSES:
    register_command_frame(ZephyrInlineRetcodeFrame.type_name, ZephyrInlineRetcodeFrame)


def _install_integration_lab() -> None:
    """Populate the active context so embedded hosts can resolve their SSH hop.

    The Zephyr backends carry ``hop="test4"``, and
    :meth:`RemoteHost._build_hop_transport` calls ``get_host(hop_id)`` to
    resolve the hop's connection details. That lookup needs the active
    :class:`~otto.context.OttoContext` populated with at least the ``test4``
    Unix host.

    Adding ``test1`` / ``test2`` / ``test3`` too keeps the lab usable by
    any cross-OS / mixed-hop test that ends up in this directory.

    Factored out of :func:`_load_lab` so the session-start bed probe
    (:func:`_probe_backend`) — which runs in ``pytest_runtest_setup``, before
    any module-scoped fixture — can populate the same lab before building
    hosts. ``set_context`` is idempotent for the same lab, so the later
    ``_load_lab`` call simply re-installs the context.
    """
    lab = Lab(name="integration_host")
    for ne in ("test1", "test2", "test3", "test4"):
        data = host_data(ne)
        lab.add_host(
            UnixHost(
                ip=data["ip"],
                element=data["element"],
                creds=[Cred(**c) for c in data["creds"]],
                board=data.get("board"),
                is_virtual=data.get("is_virtual", False),
                term=data.get("term", "ssh"),
                transfer=data.get("transfer", "scp"),
                log=LogMode.QUIET,
            )
        )
    set_context(OttoContext(lab=lab))


@pytest.fixture(autouse=True, scope="module")
def _load_lab():
    """Make the SSH hops resolvable by the embedded host transport.

    Snapshots the OttoContext ContextVar before installing the integration
    lab and restores it on module teardown. xdist workers are long-lived
    processes, so without this restore the ``integration_host`` context would
    persist after this module finishes and leak into whatever test the worker
    runs next — e.g. a ``tests/unit/test_context.py`` case asserting a pristine
    ``try_get_context() is None``. The function-scoped ``_reset_otto_context``
    in the root conftest cannot undo this: it snapshots the *already-installed*
    module context, so the module-scoped install needs its own restore.
    """
    from otto.context import _active

    snapshot = _active.get()
    _install_integration_lab()
    yield
    _active.set(snapshot)


# ---------------------------------------------------------------------------
# Embedded bed health gate
#
# `make coverage` runs the full embedded matrix against the live Zephyr QEMU
# bed (behind the `zephyr` VM). When an instance wedges — the QEMU process
# stays alive and accepts TCP on the telnet port, but the guest emits nothing,
# so otto's readiness handshake fails with "shell never became ready"
# (diagnosed 2026-06-06 as RST-close reconnect churn against the single-client
# telnet slot; NOT e1000 net-buffer exhaustion, which earlier comments here
# claimed — see tests/firmware/zephyr/common/otto-overlay.conf for the full
# corrected diagnosis) — every test
# routed to it burns the full 15s session-open ceiling (up to 600s for the
# stability suite) before failing. Across the matrix that stalls the run past
# the Makefile's 240s outer cap, which SIGKILLs the whole pipeline.
#
# The gate is *reactive*, not speculative: the first test to hit a wedged
# console fails normally (paying the one real 15s timeout) and is recognised by
# its "shell never became ready" signature; that marks the backend, and every
# *subsequent* test targeting it fails fast instead of repeating the timeout.
# The cascade collapses from N x 15s to one real failure plus instant fast-fails.
#
# A speculative pre-probe was tried and rejected: any probe ceiling short
# enough to save time is shorter than the 15s the real connection allows, so a
# healthy-but-slow console under load false-fails — turning a slow pass into a
# wrong failure, which is worse than the original problem.
#
# It deliberately does NOT auto-restart: the wedged instance is left untouched
# for inspection, and the wedge is reported loudly (per-test reason + a
# terminal-summary banner) so a recurring sizing/leak problem can't hide as a
# silent slowdown. Recover with `make qemu-restart` (or `sudo systemctl restart
# 'zephyr-qemu-*.service'` on the zephyr VM).
#
# "Left for inspection" only helps if the report says what to inspect, and it
# did not: the 2026-08-01 incident record concluded there was no way into a
# wedged guest, and the instance was restarted un-diagnosed. There is one
# — the guest's console is muxed to the unit's stdout and therefore captured by
# systemd on the hop — so the first failure per backend now carries that
# journal tail as a report section (`_guest_console_tail`).
#
# Scope: this is the *console* wedge (the slow, cascading failure mode). A live
# console with a dead SNMP relay is a separate, fast failure the SNMP tests
# surface on their own ~4s UDP timeout, and it never trips this gate (the
# signature differs). Per-worker under `-n auto`: xdist workers are separate
# processes, so a wedged backend costs at most one real timeout per worker that
# runs its tests.
# ---------------------------------------------------------------------------

# The canonical readiness-handshake failure raised by SessionManager when a
# console never produces a ready prompt (see otto/host/session.py). Matching
# this string — rather than an exception type — also catches the wrapped form
# the concurrent-transfer tests re-raise inside an AssertionError.
_WEDGE_SIGNATURE = "shell never became ready"

# Backend id -> reason, populated reactively as tests fail with the wedge
# signature. Per-worker (xdist workers are separate processes).
_BED_HEALTH: dict[str, str] = {}

# Backend id -> nodeid of the test whose report carries that backend's console
# tail, so the end-of-run banner can point at it rather than reprint the
# capture once per wedged backend.
_BED_CONSOLE: dict[str, str] = {}

# Journal-tail capture settings. The window only has to cover the wedge that
# just happened, and the wedge is detected within one 15s handshake of it.
_JOURNAL_WINDOW = "10 min ago"
_JOURNAL_FAULT_LINES = 20
_JOURNAL_TAIL_LINES = 25
_JOURNAL_SSH_TIMEOUT = 20.0

# Three questions, three greps — do not merge them into one filtered tail.
#
# A bare severity filter is useless here, measured twice against the live bed.
# ``<err>`` returns 40 identical ``fs: failed to unlink path (-2)`` lines (the
# FAT driver's ENOENT, emitted by the file-ops tests doing their normal work).
# Adding the telnet signatures to the fault filter reproduced the same
# eviction from the other direction: during an ordinary run they crowd out
# everything else. So:
#
# * ``_JOURNAL_FAULT_RE`` — signatures that are never routine. One is a finding.
# * ``shell_telnet`` errors — routine INDIVIDUALLY (the todo record's version
#   differential: the healthy 3.7 fat board has logged 862 of them, the 2.7
#   board 7 before it died), so they are reported as a count per unit rather
#   than as lines. Volume, not presence, is the signal, and a count is what
#   says so.
# * a raw tail, for what the guest was doing when it went quiet.
_JOURNAL_FAULT_RE = "FATAL|ASSERT|exception|[Oo]ut of buffers|[Ss]tack overflow|panic"

_MANUAL_CAPTURE_HINT = (
    "Read it by hand with:\n"
    f"  ssh <hop> journalctl -u 'zephyr-qemu-*' --since '{_JOURNAL_WINDOW}' --no-pager"
)


@pytest.fixture(autouse=True)
def _fd_watermark(request: pytest.FixtureRequest) -> Iterator[None]:
    """Descriptor bracket for the hop tests, scoped by the ``hops`` marker.

    Catches anything the hop path still holds when the test ends: a
    ``SshHopTransport.close()`` that aborted partway through its listener loop
    and so never reached the tunnel teardown, a forward built after close by a
    caller that raced it, the zombie ``_SelectorSocketTransport`` class this
    lane's fixtures already fight, and leaked telnet console transports.

    Scoped by marker rather than by module so a hop test added elsewhere in
    this directory is covered without anyone remembering to opt in. That scope
    stops at the directory: the ``hops``-marked modules under ``tests/e2e``
    are covered by their own lane's bracket or by nothing.

    ``gc_policy="always"``, matching the other bed lanes. The eager policy's
    two collects are what makes ``on-suspicion`` tempting, but that 3.3x
    (16.1s -> 54.0s) was measured over 1426 unit tests; here N is 18 and the
    A/B came out at 10.93s with against 10.97s without — no measurable cost
    either way. With nothing to buy, take the policy that cannot be fooled by
    the previous test's garbage inflating the baseline, which matters most in
    exactly this lane's noisy heap.

    Tolerance is the authority's 4, not ``tests/unit/host``'s 0: that lane
    earned 0 by measuring a flat floor across 1426 tests, and nobody has
    measured this one, which has live SSH sessions and channels moving under
    the test. The cost is stated plainly — this bracket cannot see a retained
    leak of four descriptors or fewer.

    What it CANNOT see at all is a leak released when the host closes: every
    hop test closes its host in a ``finally``, so the verdict is taken after
    the evidence is gone. That is not hypothetical — see
    ``test_hop_transfers_do_not_accumulate_port_forwards``, which exists
    because this bracket could not see the very leak that motivated it, and
    which counts from inside the host's lifetime for that reason.
    """
    if request.node.get_closest_marker("hops") is None:
        yield
        return
    yield from fd_watermark_bracket(gc_policy="always")


def _hop_ssh_target(backend: str) -> tuple[str, str, str] | None:
    """Resolve ``backend`` to its hop's ``(ip, login, password)``, or None.

    The embedded entries carry ``hop`` as a host *id* (``test4``) while
    :func:`host_data` keys on ``element`` (``test4``). ``scripts.lab_health``
    already owns that mapping for the lab tooling and is already imported by
    ``tests/unit/scripts/test_lab_health.py``, so this borrows it rather than
    adding a second one — a private import across that boundary is the lesser
    evil next to two hop indexes drifting apart.
    """
    ne = _ZEPHYR_BACKEND_NE.get(backend)
    hop_id = host_data(ne).get("hop") if ne else None
    if not hop_id:
        return None
    entry = _hop_index(_load_hosts(lab_data_path())).get(hop_id)
    if entry is None or not entry.get("creds"):
        return None
    login, password = _ssh_user_pass(entry["creds"])
    return entry["ip"], login, password


def _guest_console_tail(backend: str) -> str:
    """Best-effort: the wedged guest's OWN console output, from the hop's journal.

    The gate says the instance is "left for inspection". The 2026-08-01 incident
    record shows what that produced in practice: it concluded the boards'
    ``-monitor none`` / stdio-mux configuration "leaves no other window in", so
    the investigation stopped and the instance was restarted un-inspected. That
    conclusion was wrong. Muxing the console to stdio is precisely what makes it
    *durable* — systemd captures the unit's stdout, so the guest's own panic
    lines outlive both the wedge and the restart, in the hop's journal.

    It is worth the ssh round trip because those lines name the fault outright,
    and nothing else in the failure output does. The 2026-08-09 wedge was
    root-caused from them without touching the instance: on the 2.7 board,
    ``eth_e1000: Out of buffers`` following a ``shell_telnet: Failed to send``
    teardown, and — on a later run of the same board — a ``ZEPHYR FATAL ERROR``
    CPU exception in its network RX thread. Two different faults; from otto's
    side both present identically, as "shell never became ready".

    Queries the ``zephyr-qemu-*`` units as a glob rather than resolving this
    backend's own unit: no backend-to-unit mapping exists in this repo, and
    inventing one here would be a second place to keep in sync with the bed.
    Each line carries its own unit in the syslog identifier, so the tail stays
    attributable without one — and a wedge whose cause is a *neighbour* board
    stays visible, which a per-unit query would hide.

    Strictly best-effort, and deliberately so: it runs on the failure path of a
    test that has already failed for its own reasons. Every error becomes a note
    plus the manual command. A diagnostic must never change which test fails.
    """
    # ``F=$(...)`` rather than ``grep ... || echo``: a pipeline's status is its
    # LAST command's, so ``grep | tail`` exits 0 even when grep matched nothing
    # and the ``||`` fallback would never fire. Caught by running this against
    # the live bed with no faults in the window.
    unit_of_line = "awk '{split($3,a,\"[\"); print a[1]}'"
    remote = (
        f"J() {{ journalctl -u 'zephyr-qemu-*' --since '{_JOURNAL_WINDOW}' "
        "--no-pager -o short-iso; }; "
        f"F=$(J | grep -E '{_JOURNAL_FAULT_RE}' | tail -n {_JOURNAL_FAULT_LINES}); "
        f"echo '### fault lines, whole bed, since {_JOURNAL_WINDOW}:'; "
        'echo "${F:-(none: a silent wedge with no firmware fault points at the '
        'telnet slot)}"; '
        "echo; echo '### shell_telnet errors per unit (routine in ones and twos; "
        "a spike means two clients raced for one console):'; "
        f"J | grep -F 'shell_telnet' | {unit_of_line} | sort | uniq -c; "
        f"echo; echo '### last {_JOURNAL_TAIL_LINES} console lines, whole bed:'; "
        f"J | tail -n {_JOURNAL_TAIL_LINES}"
    )
    try:
        target = _hop_ssh_target(backend)
        if target is None:
            return f"No hop credentials in lab data for {backend!r}.\n{_MANUAL_CAPTURE_HINT}"
        if shutil.which("sshpass") is None:
            return f"sshpass is not installed on this runner.\n{_MANUAL_CAPTURE_HINT}"
        ip, login, password = target
        rc, out, err = _run_ssh(ip, login, password, remote, timeout=_JOURNAL_SSH_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 -- diagnostics must never mask the wedge
        return f"Capture failed ({type(exc).__name__}: {exc}).\n{_MANUAL_CAPTURE_HINT}"
    if rc != 0 or not out:
        detail = (err or "no stderr").splitlines()[-1]
        return f"ssh to the hop failed (rc={rc}: {detail}).\n{_MANUAL_CAPTURE_HINT}"
    return out


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items) -> None:
    """Serialize each embedded *device* — and the BusyBox bed — onto one worker.

    ``tryfirst`` is load-bearing, not decoration. xdist's own
    ``pytest_collection_modifyitems`` (``xdist/remote.py``) is what reads the
    ``xdist_group`` marker and annotates the nodeid with its ``@group`` suffix,
    and pluggy dispatches same-phase impls in LIFO registration order. Which
    side of xdist this conftest lands on therefore depends on WHEN it was
    registered: collected as part of a full-tree run it registers during
    collection and wins, but naming this directory on the command line
    (``pytest tests/integration/host``) makes it an *initial* conftest
    registered at startup — so it ran AFTER xdist, the markers landed too late
    to be seen, and every device's tests scattered across workers with no
    serialization at all. Measured, on this directory: full tree 86 passed with
    all 34 ``@zephyr_27_fat`` items on one worker; directory-targeted, zero
    group suffixes, 2.7 spread over four workers, 4 failed + 3 errors, and the
    guest taken down by a CPU exception in its network RX thread. Pinning the
    order removes the dependence on invocation shape, and
    :func:`_unhonored_group` re-checks the *outcome* at setup so a
    regression fails the run instead of quietly costing a board.

    The Zephyr ``shell_telnet`` backend accepts only one telnet client per
    device. Under ``-n auto`` two workers running tests against the *same*
    console make the guest log ``Telnet client already connected``; the loser's
    readiness handshake then gets no shell and surfaces as ``shell never became
    ready``. Confirmed reliably reproducible with two concurrent
    ``EmbeddedHost``s to one backend (see
    ``test_concurrent_clients_to_one_console_contend_and_recover``); a serial
    (``-n0``) run is clean, so the bug is purely concurrent same-device access.

    Each backend's tests — across every embedded file — carry one
    ``xdist_group`` keyed by the backend id, so with ``--dist loadgroup`` all
    of a device's tests land on one worker and run sequentially, never two
    clients at once. *Different* backends still parallelize across workers,
    which matters: a full one-group serialization of the whole bed measured
    >450s and would blow the Makefile's 240s cap.

    The fan-out tests (``TestConcurrentEmbeddedTransfer``) carry their own
    ``zephyr_fanout`` group (they intentionally open one client per device,
    across all devices at once). They are left in that group; the residual
    risk that a fan-out test overlaps a per-backend group on another worker is
    a narrow, known gap — see the test module note.

    The BusyBox bed takes the OPPOSITE shape: one family group
    (``busybox_bed``) covering all five guests, not one group per guest. The
    constraint there is not a single-client console — BusyBox ``telnetd``
    serves several clients — it is the CPU. The five guests are TCG (x86 on an
    aarch64 host, no KVM) and all five live on ``test1``, a two-core VM, so
    running two guests' rows on two workers does not buy parallelism: it
    timeshares the same two cores and slows both while multiplying the load
    the guests already lose to emulation. Per-guest groups would therefore
    spend the wedge risk without the run-time return that makes it worth
    paying for Zephyr. The name matches the stamp
    ``tests/integration/busybox_bed/conftest.py`` applies to the bed's own
    suite, so a whole-tree run keeps the bed suite and these rows on one
    worker too — one group for everything that touches the guests.
    """
    for item in items:
        if "embedded" in item.keywords:
            # Don't override an explicit group (e.g. the fan-out tests).
            if item.get_closest_marker("xdist_group") is not None:
                continue
            backends = _referenced_backends(item)
            if len(backends) == 1:
                item.add_marker(pytest.mark.xdist_group(backends[0]))
            continue
        # An `embedded` item is handled above even if it somehow also named a
        # guest: losing the busybox family group costs TCG throughput, while
        # losing a Zephyr device group costs the device.
        if item.get_closest_marker("xdist_group") is not None:
            continue
        if _references_busybox(item):
            item.add_marker(pytest.mark.xdist_group(_BUSYBOX_GROUP))


# The one xdist group every BusyBox guest row joins, imported rather than
# spelled: the bed suite's own stamp
# (``tests/integration/busybox_bed/conftest.py``) and the guard that catches a
# row collected outside the group (``tests/integration/conftest.py``) read the
# same constant, because two spellings would be two groups and two groups can
# run at once.
_BUSYBOX_GROUP = BUSYBOX_BED_GROUP


def _names_a_guest(value: object) -> bool:
    """Whether one param value names a BusyBox bed guest, at any nesting.

    Tuples are searched, not just bare strings, because the guest rows do not
    all arrive in one shape: ``host1`` / ``host1_kit`` pass a backend id as a
    scalar (``"busybox_1161"``) while ``transfer_host`` passes a
    ``(transfer, ne)`` pair (``("shell", "bb1161")``). Matching against
    :data:`~tests.conftest.BUSYBOX_PARAM_TOKENS` covers both spellings for the
    same reason the nesting is walked: a row that matched neither would collect
    UNGROUPED and put a second xdist worker on the two cores all five TCG
    guests share — silently, since nothing fails when a stamp goes missing.
    """
    if isinstance(value, str):
        return value in BUSYBOX_PARAM_TOKENS
    if isinstance(value, tuple | list):
        return any(_names_a_guest(v) for v in value)
    return False


def _references_busybox(item: pytest.Item) -> bool:
    """Whether this item's parametrization names a BusyBox bed backend.

    Value-matching, like :func:`_referenced_backends` and for the same reason:
    the guest rows arrive as indirect params, and matching the value rather
    than the param name keeps a new parametrize shape from silently dropping
    out of the family group. Which guests an item names doesn't matter — they
    all share one group — so this answers yes/no.
    """
    callspec = getattr(item, "callspec", None)
    if callspec is None:
        return False
    return any(_names_a_guest(v) for v in callspec.params.values())


def _referenced_backends(item: pytest.Item) -> list[str]:
    """Embedded backend ids this item targets, read from its parametrization.

    Covers every parametrize shape in the embedded suites — ``host1`` indirect,
    the ``host1, host1_kit`` 2-tuple in the contract files, and the bare
    ``backend`` param in the SNMP tests — by matching any param *value* that is
    a known embedded backend id, regardless of the param name.
    """
    callspec = getattr(item, "callspec", None)
    if callspec is None:
        return []
    seen = {v for v in callspec.params.values() if isinstance(v, str) and v in _ZEPHYR_BACKEND_NE}
    return sorted(seen)


# ---------------------------------------------------------------------------
# Fan-out vs per-device console serialization (cross-worker reader/writer lock)
#
# The grouping above pins each *device's* tests to one worker, so two clients
# never hit one console from the per-backend suites. The fan-out tests
# (``TestConcurrentEmbeddedTransfer``) are the residual gap called out in the
# grouping note: they open *every* device at once and live in their own
# ``zephyr_fanout`` group, so under ``-n auto --dist loadgroup`` they can land
# on a different worker than a per-device group and race it for a single
# console — the loser's readiness handshake gets no shell and fails with the
# ``shell never became ready`` signature (an ``IncompleteReadError(0 bytes)``
# on the telnet stream). Reproduced reliably; a serial (``-n0``) run is clean.
#
# A cross-worker reader/writer lock closes the gap without serializing the
# whole bed (the conftest grouping note measured full one-group serialization
# at >450s, over the Makefile's 240s cap). Per-device tests take a SHARED lock
# — different devices still parallelize across workers, preserving the run time
# the per-device grouping buys — while a fan-out test takes an EXCLUSIVE lock,
# so it waits for all in-flight per-device tests to drain and blocks new ones
# for the brief window it holds every console.
#
# The lock is writer-fair (see tests/integration/host/_console_lock.py): the
# EXCLUSIVE fan-out waiter holds a turnstile gate while waiting, so SHARED
# per-device churn can't starve it. The teardown also force-aborts any console
# transport a pytest-timeout'd test left half-open (abort_console_transports),
# so one timed-out fan-out test can't wedge the bed for the rest of the run.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _console_access_lock(request: pytest.FixtureRequest, tmp_path_factory):
    """Serialize the fan-out console tests against the per-device tests.

    Autouse + function-scoped, and (having no dependency on ``host1``) set up
    before it, so the lock is held across the whole window ``host1`` keeps a
    console open — its setup through its ``close()`` in teardown. Non-embedded
    tests are a no-op.

    Uses the writer-fair :func:`console_access` lock so the EXCLUSIVE fan-out
    waiter can't be starved by SHARED per-device churn. On teardown — which runs
    even after a pytest-timeout signal aborts the test — it force-aborts any
    single-client console transport a timed-out test left half-open, *before*
    releasing the lock, so the next test finds both a free lock and a clean
    console. On a clean test the host already closed + deregistered, so the
    sweep is a no-op.
    """
    if "embedded" not in request.node.keywords:
        yield
        return
    lock_dir = tmp_path_factory.getbasetemp().parent
    # A fan-out test references no single backend (it opens all of them); a
    # per-device test names exactly one — the same signal the wedge gate uses.
    exclusive = not _referenced_backends(request.node)
    with console_access(lock_dir, exclusive=exclusive):
        try:
            yield
        finally:
            abort_console_transports()


def _unhonored_group(item: pytest.Item) -> str | None:
    """The group this item claimed but xdist never applied, or None if fine.

    The marker being present proves nothing: xdist reads it in its own
    ``pytest_collection_modifyitems`` and, if ours ran later, the marker is set
    but ignored and the device's tests scatter across workers. What xdist
    leaves behind when it DOES act is the ``@group`` suffix it appends to the
    nodeid, which the worker can see — so that suffix, not the marker, is the
    honest evidence.

    This guard is the regression test for the ordering fix, and it was
    positive-controlled as one: deleting ``tryfirst`` from
    :func:`pytest_collection_modifyitems` and running a single embedded test
    under ``-n2 --dist loadgroup`` errors it at setup in 0.54s, restoring the
    decorator passes it in 5.5s. Deliberately no separate unit test — a
    hand-built item would exercise a mock of xdist's behaviour rather than
    xdist's behaviour, which is the only thing in question here. One test is
    enough for the control because the guard fires before any fixture runs, so
    it costs no console contact even when it is right.

    Returns the group name rather than a bool so the caller can name it without
    looking the marker up a second time. That second lookup was the first
    version, and ``ty`` caught it: ``get_closest_marker`` is ``Mark | None``, so
    re-deriving it at the failure site risked ``None.args`` on the one code path
    that only runs when something is already wrong.
    """
    if getattr(item.config, "workerinput", None) is None:
        return None  # not under xdist — one process, nothing to serialize
    marker = item.get_closest_marker("xdist_group")
    if marker is None or not marker.args:
        return None  # ungrouped by design (nothing claimed, nothing to check)
    group = str(marker.args[0])
    return None if item.nodeid.endswith(f"@{group}") else group


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Fail fast when a target backend was already found wedged this run.

    Leaves the wedged instance untouched (no auto-restart) so it can be
    inspected, and reports the reason inline; a session-end banner
    (:func:`pytest_terminal_summary`) lists every wedge so it can't slip by as
    a silent slowdown.
    """
    if "embedded" not in item.keywords:
        return
    unhonored = _unhonored_group(item)
    if unhonored is not None:
        pytest.fail(
            "per-device console serialization is NOT in effect: this item carries "
            f"xdist_group({unhonored!r}) but xdist never applied it (no '@group' "
            "suffix on the nodeid), so two workers can drive one Zephyr console at "
            "once. That is not a flake — it takes the guest down: a 2.7 board hit a "
            "CPU exception in its network RX thread this way, and an earlier "
            "incident left a 3.7 board unreachable for hours.\n"
            "Cause: this conftest's pytest_collection_modifyitems must run BEFORE "
            "xdist's, and pluggy orders same-phase impls by registration. It is "
            "declared tryfirst for exactly this reason — if that decorator was "
            "removed, restore it rather than silencing this check.",
            pytrace=False,
        )
    # Fan-out tests carry no backend param but open every backend, so any one
    # wedged backend takes them down too.
    referenced = _referenced_backends(item) or list(EMBEDDED_BACKENDS)
    wedged = [(b, _BED_HEALTH[b]) for b in referenced if b in _BED_HEALTH]
    if wedged:
        detail = "\n".join(f"  - {embedded_param_id(b)}: {r}" for b, r in wedged)
        pytest.fail(
            "embedded bed unhealthy — an earlier test found this console wedged; "
            "skipping the 15s reconnect (no auto-restart; left for inspection):\n"
            f"{detail}\n"
            "Recover: `make qemu-restart` (or "
            "`sudo systemctl restart 'zephyr-qemu-*.service'` on the zephyr VM).",
            pytrace=False,
        )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call):
    """Mark a backend wedged when a test fails with the readiness signature.

    Only attributes when exactly one backend is implicated — a per-backend test
    pins the culprit, whereas a fan-out test references every backend and can't
    say which one went dark.

    The first failure per backend also gets the guest's console tail attached as
    a report section (see :func:`_guest_console_tail`). "Left for inspection" is
    only true if the failure output says what to inspect, and until now it did
    not. Attached to the report rather than printed so it travels with the
    failure into JUnit XML and back from an xdist worker.

    Costs one ssh round trip, bounded at ``_JOURNAL_SSH_TIMEOUT``, on the first
    failure per backend per worker — i.e. only when the bed is already broken
    and the run is already paying 15s handshake timeouts.
    """
    outcome = yield
    report = outcome.get_result()
    if report.when != "call" or not report.failed:
        return
    if "embedded" not in item.keywords:
        return
    if _WEDGE_SIGNATURE not in str(report.longrepr):
        return
    backends = _referenced_backends(item)
    if len(backends) != 1 or backends[0] in _BED_HEALTH:
        return
    backend = backends[0]
    _BED_HEALTH[backend] = f"console wedged ('{_WEDGE_SIGNATURE}') during this run"
    _BED_CONSOLE[backend] = item.nodeid
    report.sections.append(
        (
            f"Zephyr guest console: {embedded_param_id(backend)} (hop journal)",
            _guest_console_tail(backend),
        )
    )


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    """Surface any bed wedges detected this run at the top of the summary."""
    if not _BED_HEALTH:
        return
    terminalreporter.section("embedded bed health", sep="=", red=True, bold=True)
    terminalreporter.write_line(
        f"{len(_BED_HEALTH)} Zephyr backend(s) went unresponsive this run "
        "(not auto-restarted) — the first hit paid one ~15s timeout, the rest "
        "failed fast:"
    )
    for backend, reason in _BED_HEALTH.items():
        terminalreporter.write_line(f"  - {embedded_param_id(backend)}: {reason}")
        where = _BED_CONSOLE.get(backend)
        if where:
            terminalreporter.write_line(
                f"      guest console tail attached to the report for {where}"
            )
    terminalreporter.write_line(
        "Read that tail BEFORE restarting — it names the fault (e.g. 'eth_e1000: "
        "Out of buffers', 'ZEPHYR FATAL ERROR'), and it is the only place the "
        "guest's own view of the wedge appears. It survives the restart in the "
        "hop's journal, but the 10-minute window in the attached capture does not."
    )
    terminalreporter.write_line("Recover: `make qemu-restart`, then re-run.")
