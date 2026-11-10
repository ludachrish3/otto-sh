"""Reboot chaos against the Ubuntu 24.04 QEMU guest ``ubu2404`` (#985).

Tests never power-cycle a hypervisor VM (#978, #985): a warm reset of a
VirtualBox ARM64 lab VM can crash the host-side firmware and strand the VM, so
the lab VMs are never a reboot target. The Ubuntu guest under QEMU, behind
test4 in the ``chaos`` lab, is the ONLY reboot target. A reboot there is a QEMU
reset under read-only firmware: the hypervisor is never touched and the guest
comes back on its own disk.

The guest carries a shutdown-hold unit (``chaos-shutdown-hold.service``): while
``/run/chaos/shutdown_hold`` exists the guest's shutdown stalls for that many
seconds, so a scenario can keep it reachable after the reboot is issued for as
long as it needs. That makes the "never went down" leg reproducible instead of
a race.

Ubuntu rather than BusyBox because the scenarios need what BusyBox lacks: netem
(``tc``), socat for the tunnel daemons, bash, systemd ordering for the hold
unit, and ``sudo reboot`` as the ordinary reboot verb.

Records down/up/recovery timings for the deferred _confirm_recovered tuning
(todo/chaos-reboot-followups.md §3/§4).
"""

import asyncio
import contextlib
import time

import pytest

from tests._fixtures.bed_hygiene import diff_snapshots, format_hygiene_report, snapshot_host
from tests._fixtures.tunnel_bed import cli_project_root, observe_tunnel_processes
from tests.e2e.chaos._bed import (
    CHAOS_GUEST_DATA_NETDEV,
    CHAOS_GUEST_ELEMENT,
    CHAOS_GUEST_HOP,
    CHAOS_GUEST_HOST_ID,
    CHAOS_GUEST_UNIT,
    CHAOS_HOP_DATA_TAP,
    CHAOS_LAB,
    CHAOS_PAIR,
    ChaosGuestBed,
    assert_eth2_netem_free,
    chaos_guest_boot_id,
    chaos_guest_probe,
    chaos_guest_probe_text,
    closed_unix_link_target,
    end_probe_text,
    observe_guest_tunnel_processes,
    set_shutdown_hold,
    tunnel_target,
    unix_link_id,
    wait_for_new_boot,
)
from tests.integration.chaos._driver import OttoProc, spawn_otto

pytestmark = [
    pytest.mark.chaos,
    pytest.mark.stability,
    pytest.mark.integration,
    pytest.mark.hops,  # every scenario goes through test4
    pytest.mark.no_hygiene_bracket,  # after-probe would race the boot; each test brackets manually
    pytest.mark.xdist_group("chaos_lane"),
    pytest.mark.timeout(900),  # a real reboot cycle is minutes; live-bed rule: generous
]

# chaos port block (15200-15299, tests/_fixtures/tunnel_bed.py::PORT_BLOCKS).
# test_tunnel_link_chaos.py already claimed 15200/15210; this one is disjoint.
_REBOOT_TUNNEL_PORT = 15220

# A backstop against a wedged run, not the expected schedule. The last assertion
# that needs netem still on test4 lands after the guest's reboot cycle (170 s
# measured under TCG), so the backstop is sized at more than twice that; the old
# 180 would have raced it. The `finally` repair is what actually clears the link.
_LINK_IMPAIR_EXPIRE = "600"

# The shutdown hold (seconds) for the declared-window scenario: longer than the
# short down window below, shorter than the default one, so the first leg must
# time out and the second must not.
_HOLD_S = 75
_SHORT_DOWN_S = 30


def _reboot(chaos_guest: ChaosGuestBed, xdir, *flags: str) -> "tuple[int, float, OttoProc]":
    """Run ``host <guest> reboot --wait *flags``; return ``(rc, seconds, proc)``."""
    started = time.monotonic()
    p = spawn_otto(
        ["host", chaos_guest.host_id, "reboot", "--wait", *flags],
        xdir=xdir,
        target=chaos_guest.target,
    )
    rc = p.wait(timeout=800.0)
    elapsed = time.monotonic() - started
    # Recorded down/up/recovery data point (todo/chaos-reboot-followups.md
    # §3/§4); printed unconditionally so it's captured on failure/verbose runs.
    msg = f"guest reboot --wait {' '.join(flags)} returned rc={rc} in {elapsed:.1f}s"
    print(msg)  # noqa: T201 -- recorded down/up/recovery data point
    return rc, elapsed, p


def _assert_new_boot(old: str) -> str:
    """One immediate boot-id read after a SUCCESSFUL ``--wait``: it must be a new boot.

    ``--wait`` returning success means a login round-trip just passed, so one
    read suffices; a ``--wait`` that returned before the reboot fails here
    instead of being rescued by polling.
    """
    new = chaos_guest_boot_id()
    assert new != old, "reboot --wait returned success on the old boot"
    return new


def _qemu_main_pid() -> int:
    """The guest unit's QEMU MainPID on the hop (0 when the unit is not running)."""
    return int(
        end_probe_text(CHAOS_GUEST_HOP, f"systemctl show -p MainPID --value {CHAOS_GUEST_UNIT}")
    )


def _netem(end: "tuple[str, str]", *, expected: bool, what: str) -> None:
    """Assert netem is present (or absent) on one ``(element, netdev)`` link end."""
    elem, netdev = end
    qdisc = end_probe_text(elem, f"tc qdisc show dev {netdev}")
    has_netem = "netem" in qdisc
    if expected:
        assert has_netem, f"{elem}: expected netem present {what}; qdisc was {qdisc!r}"
    else:
        assert not has_netem, f"{elem}: netem survived {what}: {qdisc!r}"


def test_happy_path_reboot_wait_recovers(chaos_guest, tmp_path):
    """`reboot --wait` on the guest returns success only after a clean shell
    round-trip (not just TCP accept), and the guest genuinely booted afresh.
    """
    old = chaos_guest_boot_id()
    pid_before = _qemu_main_pid()
    rc, _, p = _reboot(chaos_guest, tmp_path)
    assert rc == 0, p.stderr_text()
    _assert_new_boot(old)
    # The reboot is a QEMU reset inside the same process, never a unit restart.
    pid_after = _qemu_main_pid()
    assert pid_before != 0, f"{CHAOS_GUEST_UNIT} had no MainPID before the reboot"
    assert pid_after == pid_before, (
        f"{CHAOS_GUEST_UNIT} MainPID changed across the reboot: {pid_before} -> {pid_after}"
    )
    # Independent confirmation the host is genuinely usable post-reboot.
    assert "POST-REBOOT" in chaos_guest_probe_text("echo POST-REBOOT", timeout=60)


def test_reboot_x_tunnel_survivors_reaped(chaos_guest, tmp_path):
    """A tunnel whose endpoint reboots: the daemon dies with the OS; assert
    `tunnel remove --all --yes` reconciles the (now half-)chain and the trio
    ends clean. Reboots ONLY the guest.
    """
    sut = cli_project_root(tmp_path, elements=CHAOS_PAIR)
    tun_target = tunnel_target(sut, lab=CHAOS_LAB, host=CHAOS_GUEST_HOP)
    hop_hop = f"{CHAOS_GUEST_HOP}@{CHAOS_HOP_DATA_TAP}"
    guest_hop = f"{CHAOS_GUEST_HOST_ID}@{CHAOS_GUEST_DATA_NETDEV}"
    add_xdir = tmp_path / "add"
    add_xdir.mkdir()
    add = spawn_otto(
        ["tunnel", "add", "--hosts", f"{hop_hop},{guest_hop}", "--port", str(_REBOOT_TUNNEL_PORT)],
        xdir=add_xdir,
        target=tun_target,
    )
    try:
        # Full completion: rc==0 already proves add_tunnel's own post-add verify
        # passed on BOTH ends, so the daemons are confirmed up before we reboot.
        assert add.wait(timeout=120.0) == 0, add.stderr_text()
        built = asyncio.run(observe_tunnel_processes(hosts=(CHAOS_GUEST_HOP,)))
        assert any(host_id == CHAOS_GUEST_HOP for host_id, _obs in built), (
            f"expected a {CHAOS_GUEST_HOP} tunnel daemon before reboot; observed: {built}"
        )
        assert observe_guest_tunnel_processes(), "expected a guest tunnel daemon before reboot"
        # Manual BedHygiene bracket on the guest (module marker: the autouse
        # after-probe would race the boot). Snapshot right before the reboot so
        # the diff isolates what the REBOOT itself changes.
        before = chaos_guest_probe(snapshot_host)
        reboot_xdir = tmp_path / "reboot"
        reboot_xdir.mkdir()
        old = chaos_guest_boot_id()
        rc, _, rp = _reboot(chaos_guest, reboot_xdir)
        assert rc == 0, rp.stderr_text()
        _assert_new_boot(old)
        # AFTER snapshot only now that the guest is back.
        after = chaos_guest_probe(snapshot_host)
        leftovers = diff_snapshots(before, after)
        assert not leftovers, format_hygiene_report(CHAOS_GUEST_ELEMENT, leftovers)
        rm_xdir = tmp_path / "rm"
        rm_xdir.mkdir()
        rm = spawn_otto(["tunnel", "remove", "--all", "--yes"], xdir=rm_xdir, target=tun_target)
        assert rm.wait(timeout=120.0) == 0, rm.stderr_text()
        assert not asyncio.run(observe_tunnel_processes(hosts=(CHAOS_GUEST_HOP,))), (
            "tunnel remove --all left survivors on the hop after the guest's reboot"
        )
        assert not observe_guest_tunnel_processes(), (
            "tunnel remove --all left survivors on the guest after its reboot"
        )
    finally:
        rm2_xdir = tmp_path / "rm2"
        rm2_xdir.mkdir()
        spawn_otto(["tunnel", "remove", "--all", "--yes"], xdir=rm2_xdir, target=tun_target).wait(
            timeout=120.0
        )
        assert not asyncio.run(observe_tunnel_processes(hosts=(CHAOS_GUEST_HOP,))), (
            "bed not clean after final reconciliation"
        )
        assert not observe_guest_tunnel_processes(), "bed not clean after final reconciliation"


def test_reboot_x_link_repair_idempotent(chaos_guest, tmp_path):
    """Impair the hop<->guest link, reboot the guest (its qdisc clears with the
    OS), the hop's qdisc remains; `link repair --all` is idempotent against the
    half-clean state and ends with both endpoints netem-free.

    Deliberately NOT the generic `snapshot_host`/`diff_snapshots` bracket on the
    guest: that oracle flags ANY qdisc change, and a netem qdisc clearing on the
    guest is exactly the EXPECTED event under test here, not a leftover.
    """
    hop_end = (CHAOS_GUEST_HOP, CHAOS_HOP_DATA_TAP)
    guest_end = (CHAOS_GUEST_ELEMENT, CHAOS_GUEST_DATA_NETDEV)
    link_id = unix_link_id(CHAOS_PAIR)
    # A CLOSED lab holding only the chaos pair, so `repair --all` files nothing
    # else as a failure by design (#473).
    link_target = closed_unix_link_target(tmp_path, pair=CHAOS_PAIR, lab=CHAOS_LAB)
    imp_xdir = tmp_path / "impair"
    imp_xdir.mkdir()
    imp = spawn_otto(
        ["link", "impair", link_id, "--loss", "50", "--expire", _LINK_IMPAIR_EXPIRE],
        xdir=imp_xdir,
        target=link_target,
    )
    try:
        assert imp.wait(timeout=120.0) == 0, imp.stderr_text()
        _netem(hop_end, expected=True, what="right after impair")
        _netem(guest_end, expected=True, what="right after impair")
        reboot_xdir = tmp_path / "reboot"
        reboot_xdir.mkdir()
        old = chaos_guest_boot_id()
        rc, _, rp = _reboot(chaos_guest, reboot_xdir)
        assert rc == 0, rp.stderr_text()
        _assert_new_boot(old)
        # The guest's qdisc cleared with the OS; the hop (a peer the reboot never
        # touched) still carries its netem -- the half-clean state.
        _netem(guest_end, expected=False, what="right after the guest's reboot (OS-cleared)")
        _netem(hop_end, expected=True, what="right after the guest's reboot (hop untouched)")
        rep_xdir = tmp_path / "rep"
        rep_xdir.mkdir()
        rep = spawn_otto(["link", "repair", "--all"], xdir=rep_xdir, target=link_target)
        assert rep.wait(timeout=120.0) == 0, rep.stderr_text()
        assert_eth2_netem_free(
            "repair --all against the half-clean (guest-already-clean) state",
            ends=(hop_end, guest_end),
        )
    finally:
        rep2_xdir = tmp_path / "rep2"
        rep2_xdir.mkdir()
        spawn_otto(["link", "repair", "--all"], xdir=rep2_xdir, target=link_target).wait(
            timeout=120.0
        )
        assert_eth2_netem_free("the FINAL reconciliation repair --all", ends=(hop_end, guest_end))


def test_reboot_wait_honours_the_declared_down_window(chaos_guest, tmp_path):
    """`--down-timeout` bounds the down phase, and the default (120 s) outlasts a hold.

    The guest holds its shutdown for ``_HOLD_S`` seconds, so it stays reachable
    after the reboot is issued. First leg (#979): ``--down-timeout 30`` must give
    up with "never went down" instead of waiting out the whole hold. Second leg:
    no flag, so the resolved default window applies and the same hold is waited
    out; it passes only because the down probe is stateless (#435), where a
    cached connection would keep answering through the hold.
    """
    old = chaos_guest_boot_id()
    first_xdir = tmp_path / "first"
    first_xdir.mkdir()
    second_xdir = tmp_path / "second"
    second_xdir.mkdir()
    try:
        set_shutdown_hold(_HOLD_S)
        rc, elapsed, p = _reboot(chaos_guest, first_xdir, "--down-timeout", str(_SHORT_DOWN_S))
        # A failed Result's msg goes through print_error on the rich console,
        # which is stdout (otto.cli.invoke._render_failed_result); stderr is
        # included in the failure text only as context.
        out = p.stdout_text()
        assert rc != 0, out + p.stderr_text()
        assert f"never went down within {float(_SHORT_DOWN_S)}s" in out, out + p.stderr_text()
        # Refused within the down window, not after the hold ran out.
        assert elapsed < _HOLD_S, f"short leg took {elapsed:.1f}s, not under the {_HOLD_S}s hold"
        new = wait_for_new_boot(old)
        set_shutdown_hold(_HOLD_S)
        rc2, _, p2 = _reboot(chaos_guest, second_xdir)
        assert rc2 == 0, p2.stdout_text() + p2.stderr_text()
        _assert_new_boot(new)
    finally:
        # /run is a tmpfs, so the hold dies with the reboot anyway; an interrupted
        # first leg must not hold the next scenario, and the first failure stays visible.
        with contextlib.suppress(Exception):
            set_shutdown_hold(None)
