"""
Fixtures local to tests/integration/host/.

The parametrized ``host1`` / ``host1_kit`` fixtures live in
:mod:`tests.conftest` (shared with the unit tree). This conftest exists
only to populate the lab into otto's configModule so the embedded hosts'
hop resolution (``configmodule.get_host('basil_seed')`` inside
``RemoteHost._build_hop_transport``) can find the SSH hop.

The same wiring is done in :mod:`tests.unit.host.test_hop_integration` for
multi-hop UnixHost tests; both follow the pattern documented in
:func:`otto.configmodule.setConfigModule`.
"""

import fcntl
import os
import sys
from pathlib import Path

import pytest

from otto.configmodule import setConfigModule
from otto.configmodule.lab import Lab
from otto.host.command_frame import register_command_frame
from otto.host.unixHost import UnixHost
from tests.conftest import (
    EMBEDDED_BACKENDS,
    _ZEPHYR_BACKEND_NE,
    embedded_param_id,
    host_data,
)

# Make repo1's custom Zephyr 2.7 dialect resolvable by the storage factory.
#
# The embedded integration tests build hosts via ``create_host_from_dict``
# directly (the raw factory path), which — unlike a full ``otto`` config load —
# does not import the SUT repo's init modules, so the ``"zephyr-inline"`` frame
# the 2.7 lab entries declare would be unregistered. Register it here by
# importing the very same class repo1's init module registers in production
# (``repo1_instructions/__init__.py``), adding the repo's pylib to the path the
# way ``Repo.addLibsToPythonpath`` does at config-load time.
_REPO1_PYLIB = Path(__file__).resolve().parents[2] / "repo1" / "pylib"
if str(_REPO1_PYLIB) not in sys.path:
    sys.path.insert(0, str(_REPO1_PYLIB))
from repo1_common.zephyr_inline import ZephyrInlineRetcodeFrame  # noqa: E402

register_command_frame(ZephyrInlineRetcodeFrame.type_name, ZephyrInlineRetcodeFrame)


def _install_integration_lab() -> None:
    """Populate the configModule so embedded hosts can resolve their SSH hop.

    The Zephyr backends carry ``hop="basil_seed"``, and
    :meth:`RemoteHost._build_hop_transport` calls ``get_host(hop_id)`` to
    resolve the hop's connection details. That lookup needs the configModule
    populated with at least the ``basil`` Unix host.

    Adding ``carrot`` / ``tomato`` / ``pepper`` too keeps the lab usable by
    any cross-OS / mixed-hop test that ends up in this directory.

    Factored out of :func:`_load_lab` so the session-start bed probe
    (:func:`_probe_backend`) — which runs in ``pytest_runtest_setup``, before
    any module-scoped fixture — can populate the same lab before building
    hosts. ``setConfigModule`` is global and idempotent, so the later
    ``_load_lab`` call simply re-sets it.
    """
    lab = Lab(name="integration_host")
    for ne in ("carrot", "tomato", "pepper", "basil"):
        data = host_data(ne)
        lab.addHost(UnixHost(
            ip=data["ip"],
            ne=data["ne"],
            creds=data["creds"],
            board=data.get("board"),
            is_virtual=data.get("is_virtual", False),
            term=data.get("term", "ssh"),
            transfer=data.get("transfer", "scp"),
            log=False,
        ))
    setConfigModule(lab=lab, repos=[])


@pytest.fixture(autouse=True, scope="module")
def _load_lab():
    """Make the SSH hops resolvable by the embedded host transport."""
    _install_integration_lab()
    yield


# ---------------------------------------------------------------------------
# Embedded bed health gate
#
# `make coverage` runs the full embedded matrix against the live Zephyr QEMU
# bed (behind the `zephyr` VM). When an instance wedges — typically e1000
# net-buffer exhaustion under sustained load: the QEMU process stays alive and
# accepts TCP on the telnet port, but the guest emits nothing, so otto's
# readiness handshake fails with "shell never became ready" — every test
# routed to it burns the full 15s session-open ceiling (up to 600s for the
# stability suite) before failing. Across the matrix that stalls the run past
# the Makefile's 240s outer cap, which SIGKILLs the whole pipeline.
#
# The gate is *reactive*, not speculative: the first test to hit a wedged
# console fails normally (paying the one real 15s timeout) and is recognised by
# its "shell never became ready" signature; that marks the backend, and every
# *subsequent* test targeting it fails fast instead of repeating the timeout.
# The cascade collapses from N×15s to one real failure plus instant fast-fails.
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


def pytest_collection_modifyitems(config, items) -> None:
    """Serialize each embedded *device*'s tests onto one xdist worker.

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
    """
    for item in items:
        if "embedded" not in item.keywords:
            continue
        # Don't override an explicit group (e.g. the fan-out tests).
        if item.get_closest_marker("xdist_group") is not None:
            continue
        backends = _referenced_backends(item)
        if len(backends) == 1:
            item.add_marker(pytest.mark.xdist_group(backends[0]))


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
    seen = {
        v for v in callspec.params.values()
        if isinstance(v, str) and v in _ZEPHYR_BACKEND_NE
    }
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
# flock is reader-preferring on Linux, so a continuously-busy reader set could
# in theory starve the exclusive waiter; in practice the per-device tests are
# short and finite, the gap between a reader releasing and the next acquiring
# lets the waiter in within a few cycles, and the fan-out's own
# ``@pytest.mark.timeout`` bounds the worst case. Readers never wait on the
# writer, so the lock cannot deadlock.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _console_access_lock(request: pytest.FixtureRequest, tmp_path_factory):
    """Serialize the fan-out console tests against the per-device tests.

    Autouse + function-scoped, and (having no dependency on ``host1``) set up
    before it, so the lock is held across the whole window ``host1`` keeps a
    console open — its setup through its ``close()`` in teardown — not just the
    test body. Non-embedded tests in this tree are a no-op.
    """
    if "embedded" not in request.node.keywords:
        yield
        return
    # ``getbasetemp().parent`` is common to every xdist worker (each worker's
    # basetemp is a child of it) and to a plain ``-n0`` run — the documented
    # pytest-xdist pattern for a cross-worker shared path. O_CREAT is safe under
    # the concurrent first-touch: the file is created once and reused.
    lock_path = tmp_path_factory.getbasetemp().parent / "zephyr_console.lock"
    # A fan-out test references no single backend (it opens all of them); a
    # per-device test names exactly one — the same signal the wedge gate uses.
    exclusive = not _referenced_backends(request.node)
    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, mode)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Fail fast when a target backend was already found wedged this run.

    Leaves the wedged instance untouched (no auto-restart) so it can be
    inspected, and reports the reason inline; a session-end banner
    (:func:`pytest_terminal_summary`) lists every wedge so it can't slip by as
    a silent slowdown.
    """
    if "embedded" not in item.keywords:
        return
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
    if len(backends) == 1:
        _BED_HEALTH.setdefault(
            backends[0], f"console wedged ('{_WEDGE_SIGNATURE}') during this run"
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
    terminalreporter.write_line(
        "Recover: `make qemu-restart`, then re-run."
    )
