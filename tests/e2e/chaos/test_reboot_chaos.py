"""Reboot chaos (spec 2026-07-31 amendment, bed leg). Soft reboots on the
LEASED host only — never test4, never the boards, never the dev VM. Real bed
reboots were Chris-approved for Plan 4; the controller re-confirms before this
module runs. Records down/up/recovery timings for the deferred _confirm_
recovered tuning (todo/chaos-reboot-followups.md §3/§4).

NO BUSYBOX GUEST ARM. Two of the three scenarios are the tunnel-x-reboot and
link-x-reboot pairs, which are inherently test1+test2 (see
test_tunnel_link_chaos.py's own note on why neither half travels to a
guest). The happy-path arm is the interesting one, because it WOULD run --
the guests have a ``reboot`` applet -- and it is still declined, on what it
would actually be measuring. QEMU runs each guest with ``-no-reboot``
(Vagrantfile), so an in-guest reboot does not reboot anything: it EXITS the
qemu process, and what brings a guest back is systemd's ``Restart=always``
on test1, booting a fresh copy of the same initramfs with every byte of
guest state gone. A green ``reboot --wait`` would therefore be evidence
about a unit file on the hop, not about otto's two-phase down/up watch
recovering a host. It is also, unavoidably, a deliberate power cycle of a
shared bed guest, which this lane does not do on its own initiative -- the
bed's recovery paths (``Restart=always``, ``make qemu-restart``) exist for
wedges chaos CAUSES, not as a reboot verb to drive.

The two multi-host scenarios (tunnel/link x reboot) pin test1/
test2 directly, the same shape Task 8's test_tunnel_link_chaos.py uses
for its own inherently-two-host scenarios: no `chaos_bed` lease on either
(the module's autouse `_bed_hygiene_bracket` still leases ONE unix host
for the whole session via that fixture's own `chaos_bed` dependency, but
these two tests never touch it directly). Unlike Task 8's SIGKILL/rollback
scenarios, neither multi-host case here needs to interrupt a command
mid-flight, so there is no `_wait_for_stdout` phase-marker wait or
`COLUMNS`-widened console: each command here is let to run to completion
(rc==0 is itself proof the post-op verify passed), and only the *reboot*
step needs a wall-clock.
"""

import asyncio
import time

import pytest

from tests._fixtures.bed_hygiene import diff_snapshots, format_hygiene_report, snapshot_host
from tests._fixtures.tunnel_bed import cli_sut_dir, observe_tunnel_processes
from tests.e2e.chaos._bed import (
    assert_eth2_netem_free,
    probe_text,
    run_probe,
    tunnel_target,
    unix_link_id,
)
from tests.integration.chaos._driver import spawn_otto
from tests.integration.chaos._target import make_bed_target

pytestmark = [
    pytest.mark.chaos,
    pytest.mark.stability,
    pytest.mark.integration,
    pytest.mark.hops,  # genuinely multi-host (test1+test2), like the two other multi-host modules
    pytest.mark.no_hygiene_bracket,  # after-probe would race the boot; each test brackets manually
    pytest.mark.xdist_group("chaos_lane"),
    pytest.mark.timeout(900),  # a real reboot cycle is minutes; live-bed rule: generous
]

# chaos port block (15200-15299, tests/_fixtures/tunnel_bed.py::PORT_BLOCKS).
# Task 8's test_tunnel_link_chaos.py already claimed 15200/15210 in the same
# block; this module's own port is disjoint from both.
_REBOOT_TUNNEL_PORT = 15220

# test1's netem must outlive the test2 reboot cycle so the "half-clean"
# state (test2 OS-cleared, test1 still impaired) is genuinely observable
# afterward. With reboot.target's JobTimeoutSec fix live on the bed (Chris,
# 2026-08-02: vboxadd's D-state teardown wedge was the prior 30min-stall
# root cause; a timed real reboot now completes in ~17s, worst case bounded
# at ~90s + ~10s boot), the plan's literal `--expire 60` (same value Task 8
# uses for its own, much shorter, SIGKILL scenarios) would already comfortably
# outlast a real reboot here. Bumped to 180 anyway (Plan 4 deferred-minors
# cleanup): the expire is a backstop against a wedged/failed run leaving
# test1 impaired forever, not the expected schedule -- worst case observed
# is ~90s, so 180 buys real margin without lengthening a passing run (the
# test itself waits on `repair --all`, never on the expire firing).
_LINK_IMPAIR_EXPIRE = "180"


def _eth2_qdisc(elem: str) -> str:
    return probe_text(elem, "tc qdisc show dev eth2")


def _assert_eth2_netem(elem: str, *, expected: bool, what: str) -> None:
    qdisc = _eth2_qdisc(elem)
    has_netem = "netem" in qdisc
    if expected:
        assert has_netem, f"{elem}: expected netem present {what}; qdisc was {qdisc!r}"
    else:
        assert not has_netem, f"{elem}: netem survived {what}: {qdisc!r}"


def test_happy_path_reboot_wait_recovers(chaos_bed, tmp_path):
    """`reboot --wait` on the leased host returns success only after a clean
    shell round-trip (not just TCP accept). Records the wall-clock.
    """
    started = time.monotonic()
    p = spawn_otto(
        ["host", chaos_bed.target.host_id, "reboot", "--wait"],
        xdir=tmp_path,
        target=chaos_bed.target,
    )
    rc = p.wait(timeout=620.0)
    elapsed = time.monotonic() - started
    # Recorded down/up/recovery data point (todo/chaos-reboot-followups.md
    # §3/§4); printed unconditionally so it's captured on failure/verbose runs.
    msg = f"reboot --wait recovered in {elapsed:.1f}s"
    print(msg)  # noqa: T201 -- recorded down/up/recovery data point
    assert rc == 0, p.stderr_text()
    # Independent confirmation the host is genuinely usable post-reboot.
    assert "POST-REBOOT" in probe_text(chaos_bed.element, "echo POST-REBOOT", timeout=60)


def test_reboot_x_tunnel_survivors_reaped(tmp_path):
    """A tunnel whose endpoint reboots: the daemon dies with the OS; assert
    `tunnel remove --all --yes` reconciles the (now half-)chain and the trio
    ends clean. Multi-host -> pins test1/test2 directly (no
    chaos_bed lease); reboots ONLY test2.
    """
    sut = cli_sut_dir(tmp_path)
    tun_target = tunnel_target(sut)
    add_xdir = tmp_path / "add"
    add_xdir.mkdir()
    add = spawn_otto(
        [
            "tunnel",
            "add",
            "--hosts",
            "test1,test2",
            "--port",
            str(_REBOOT_TUNNEL_PORT),
        ],
        xdir=add_xdir,
        target=tun_target,
    )
    try:
        # Full completion (not a phase-marker + signal, unlike Task 8's
        # SIGKILL scenarios): rc==0 already proves add_tunnel's own post-add
        # verify passed on BOTH ends, so the daemons are confirmed up before
        # we touch anything else.
        assert add.wait(timeout=120.0) == 0, add.stderr_text()
        built = asyncio.run(observe_tunnel_processes())
        assert any(host_id == "test2" for host_id, _obs in built), (
            f"expected a test2 tunnel daemon before reboot; observed: {built}"
        )
        # Manual BedHygiene bracket on test2 (module marker: the autouse
        # after-probe would race the boot). Snapshot right before the
        # reboot -- not at test start -- so the diff isolates what the
        # REBOOT itself changes, not what building the tunnel already did.
        before = run_probe("test2", snapshot_host)
        reboot_target = make_bed_target("test2")
        reboot_xdir = tmp_path / "reboot"
        reboot_xdir.mkdir()
        started = time.monotonic()
        rp = spawn_otto(
            ["host", "test2", "reboot", "--wait"], xdir=reboot_xdir, target=reboot_target
        )
        rc = rp.wait(timeout=620.0)
        elapsed = time.monotonic() - started
        # Recorded down/up/recovery data point; captured on failure/verbose.
        msg = f"test2 reboot --wait recovered in {elapsed:.1f}s (tunnel scenario)"
        print(msg)  # noqa: T201 -- recorded down/up/recovery data point
        assert rc == 0, rp.stderr_text()
        # AFTER snapshot only now that reboot --wait confirmed test2 is back.
        after = run_probe("test2", snapshot_host)
        leftovers = diff_snapshots(before, after)
        assert not leftovers, format_hygiene_report("test2", leftovers)
        rm_xdir = tmp_path / "rm"
        rm_xdir.mkdir()
        rm = spawn_otto(["tunnel", "remove", "--all", "--yes"], xdir=rm_xdir, target=tun_target)
        assert rm.wait(timeout=120.0) == 0, rm.stderr_text()
        assert not asyncio.run(observe_tunnel_processes()), (
            "tunnel remove --all left survivors after test2's reboot"
        )
    finally:
        rm2_xdir = tmp_path / "rm2"
        rm2_xdir.mkdir()
        spawn_otto(["tunnel", "remove", "--all", "--yes"], xdir=rm2_xdir, target=tun_target).wait(
            timeout=120.0
        )
        assert not asyncio.run(observe_tunnel_processes()), (
            "bed not clean after final reconciliation"
        )


def test_reboot_x_link_repair_idempotent(tmp_path):
    """Impair the test1<->test2 link, reboot test2 (its qdisc clears with
    the OS), test1's qdisc remains; `link repair --all` is idempotent against
    the half-clean state and ends with both endpoints netem-free.

    Unlike the tunnel scenario above, this one deliberately does NOT use the
    generic `snapshot_host`/`diff_snapshots` bracket on test2: that oracle's
    qdisc comparison flags ANY change (not just new-vs-before), and a netem
    qdisc clearing on test2 is exactly the EXPECTED event under test here,
    not a leftover. Direct `tc qdisc show` checks (the shared
    `_bed.assert_eth2_netem_free` helper) say precisely what the scenario
    means: netem gone on test2, still present on test1, until `repair --all`.
    """
    link_id = unix_link_id()
    link_target = make_bed_target("test1")
    imp_xdir = tmp_path / "impair"
    imp_xdir.mkdir()
    imp = spawn_otto(
        ["link", "impair", link_id, "--loss", "50", "--expire", _LINK_IMPAIR_EXPIRE],
        xdir=imp_xdir,
        target=link_target,
    )
    try:
        assert imp.wait(timeout=120.0) == 0, imp.stderr_text()
        _assert_eth2_netem("test1", expected=True, what="right after impair")
        _assert_eth2_netem("test2", expected=True, what="right after impair")
        reboot_target = make_bed_target("test2")
        reboot_xdir = tmp_path / "reboot"
        reboot_xdir.mkdir()
        started = time.monotonic()
        rp = spawn_otto(
            ["host", "test2", "reboot", "--wait"], xdir=reboot_xdir, target=reboot_target
        )
        rc = rp.wait(timeout=620.0)
        elapsed = time.monotonic() - started
        # Recorded down/up/recovery data point; captured on failure/verbose.
        msg = f"test2 reboot --wait recovered in {elapsed:.1f}s (link scenario)"
        print(msg)  # noqa: T201 -- recorded down/up/recovery data point
        assert rc == 0, rp.stderr_text()
        # test2's qdisc cleared with the OS; test1 (a peer the reboot never
        # touched) still carries its netem -- the half-clean state.
        _assert_eth2_netem("test2", expected=False, what="right after test2's reboot (OS-cleared)")
        _assert_eth2_netem(
            "test1", expected=True, what="right after test2's reboot (test1 untouched)"
        )
        rep_xdir = tmp_path / "rep"
        rep_xdir.mkdir()
        rep = spawn_otto(["link", "repair", "--all"], xdir=rep_xdir, target=link_target)
        assert rep.wait(timeout=120.0) == 0, rep.stderr_text()
        assert_eth2_netem_free("repair --all against the half-clean (test2-already-clean) state")
    finally:
        rep2_xdir = tmp_path / "rep2"
        rep2_xdir.mkdir()
        spawn_otto(["link", "repair", "--all"], xdir=rep2_xdir, target=link_target).wait(
            timeout=120.0
        )
        assert_eth2_netem_free("the FINAL reconciliation repair --all")
