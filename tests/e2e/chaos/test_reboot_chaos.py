"""Reboot chaos (spec 2026-07-31 amendment, bed leg). Soft reboots on the
LEASED host only — never basil, never the boards, never the dev VM. Real bed
reboots were Chris-approved for Plan 4; the controller re-confirms before this
module runs. Records down/up/recovery timings for the deferred _confirm_
recovered tuning (todo/chaos-reboot-followups.md §3/§4).

The two multi-host scenarios (tunnel/link x reboot) pin carrot_seed/
tomato_seed directly, the same shape Task 8's test_tunnel_link_chaos.py uses
for its own inherently-two-host scenarios: no `chaos_bed` lease on either
(the module's autouse `_bed_hygiene_bracket` still leases ONE veggies host
for the whole session via that fixture's own `chaos_bed` dependency, but
these two tests never touch it directly). Unlike Task 8's SIGKILL/rollback
scenarios, neither multi-host case here needs to interrupt a command
mid-flight, so there is no `_wait_for_stdout` phase-marker wait or
`COLUMNS`-widened console: each command here is let to run to completion
(rc==0 is itself proof the post-op verify passed), and only the *reboot*
step needs a wall-clock.
"""

import asyncio
import json
import time

import pytest

from otto.link.derive import addressing_from_dict, resolve_declared_links
from otto.logger.mode import LogMode
from tests._fixtures.bed_hygiene import diff_snapshots, format_hygiene_report, snapshot_host
from tests._fixtures.labdata import host_data, lab_data_path
from tests._fixtures.tunnel_bed import cli_sut_dir, observe_tunnel_processes
from tests.e2e.chaos._bed import run_probe
from tests.integration.chaos._driver import spawn_otto
from tests.integration.chaos._target import ChaosTarget, make_bed_target

pytestmark = [
    pytest.mark.chaos,
    pytest.mark.stability,
    pytest.mark.integration,
    pytest.mark.hops,  # genuinely multi-host (carrot+tomato), like the two other multi-host modules
    pytest.mark.no_hygiene_bracket,  # after-probe would race the boot; each test brackets manually
    pytest.mark.xdist_group("chaos_lane"),
    pytest.mark.timeout(900),  # a real reboot cycle is minutes; live-bed rule: generous
]

# chaos port block (15200-15299, tests/_fixtures/tunnel_bed.py::PORT_BLOCKS).
# Task 8's test_tunnel_link_chaos.py already claimed 15200/15210 in the same
# block; this module's own port is disjoint from both.
_REBOOT_TUNNEL_PORT = 15220

# carrot's netem must outlive the tomato reboot cycle so the "half-clean"
# state (tomato OS-cleared, carrot still impaired) is genuinely observable
# afterward. With reboot.target's JobTimeoutSec fix live on the bed (Chris,
# 2026-08-02: vboxadd's D-state teardown wedge was the prior 30min-stall
# root cause; a timed real reboot now completes in ~17s, worst case bounded
# at ~90s + ~10s boot), the plan's literal `--expire 60` (same value Task 8
# uses for its own, much shorter, SIGKILL scenarios) comfortably outlasts a
# real reboot here too -- no need to inflate it.
_LINK_IMPAIR_EXPIRE = "60"


def _tunnel_target(sut_dir) -> ChaosTarget:
    """ChaosTarget for CLI-driven tunnel commands against `cli_sut_dir`'s isolated SUT.

    Duplicated (not imported) from ``tests/e2e/chaos/test_tunnel_link_chaos.py``
    -- this codebase's own convention for these chaos test modules (see e.g.
    ``test_connection_drop.py``'s own ``_veggies_link_id``, which mirrors the
    same source "verbatim" rather than importing across test modules).
    """
    carrot = host_data("carrot")
    cred = carrot["creds"][0]
    return ChaosTarget(
        sut_dir=sut_dir,
        lab="veggies",
        host_id="carrot_seed",
        ssh_host=carrot["ip"],
        ssh_port=22,
        ssh_username=cred["login"],
        ssh_client_key=None,
        ssh_password=cred["password"],
    )


def _veggies_link_id() -> str:
    """The declared carrot_seed<->tomato_seed eth2 link's id.

    Duplicated verbatim from ``test_tunnel_link_chaos.py`` / ``test_connection_
    drop.py``: the raw ``tech1/lab.json`` has no literal ``"id"`` key on its
    ``links`` entries -- ``Link.id`` is auto-derived at load time
    (``otto.link.model.make_static_link_id``), so this replicates the SAME
    load ``otto`` itself does (``otto.link.derive.resolve_declared_links``).
    """
    data = json.loads(lab_data_path().read_text())
    hosts = dict(addressing_from_dict(h) for h in data["hosts"])
    loaded_ids = set(hosts)
    links = resolve_declared_links(data["links"], hosts, source="lab.json", loaded_ids=loaded_ids)
    link = links[0]  # tech1/lab.json declares exactly one link: carrot_seed:eth2<->tomato_seed:eth2
    assert {link.a.host, link.b.host} == {"carrot_seed", "tomato_seed"}, (
        f"expected the carrot<->tomato eth2 link at links[0], got {link!r} -- "
        "tech1/lab.json's declared links changed shape"
    )
    return link.id


def _eth2_qdisc(elem: str) -> str:
    out = run_probe(elem, lambda h: h.exec("tc qdisc show dev eth2", timeout=30, log=LogMode.QUIET))
    return out.value or ""


def _assert_eth2_netem(elem: str, *, expected: bool, what: str) -> None:
    qdisc = _eth2_qdisc(elem)
    has_netem = "netem" in qdisc
    if expected:
        assert has_netem, f"{elem}: expected netem present {what}; qdisc was {qdisc!r}"
    else:
        assert not has_netem, f"{elem}: netem survived {what}: {qdisc!r}"


def _assert_eth2_netem_free(what: str) -> None:
    for elem in ("carrot", "tomato"):
        _assert_eth2_netem(elem, expected=False, what=what)


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
    rc = p.wait(timeout=600.0)
    elapsed = time.monotonic() - started
    print(f"reboot --wait recovered in {elapsed:.1f}s")  # noqa: T201 -- the recorded down/up/recovery data point (todo/chaos-reboot-followups.md §3/§4); captured on failure/verbose
    assert rc == 0, p.stderr_text()
    # Independent confirmation the host is genuinely usable post-reboot.
    out = run_probe(
        chaos_bed.element, lambda h: h.exec("echo POST-REBOOT", timeout=60, log=LogMode.QUIET)
    )
    assert "POST-REBOOT" in (out.value or "")


def test_reboot_x_tunnel_survivors_reaped(tmp_path):
    """A tunnel whose endpoint reboots: the daemon dies with the OS; assert
    `tunnel remove --all --yes` reconciles the (now half-)chain and the trio
    ends clean. Multi-host -> pins carrot_seed/tomato_seed directly (no
    chaos_bed lease); reboots ONLY tomato_seed.
    """
    sut = cli_sut_dir(tmp_path)
    tunnel_target = _tunnel_target(sut)
    add_xdir = tmp_path / "add"
    add_xdir.mkdir()
    add = spawn_otto(
        [
            "tunnel",
            "add",
            "--hosts",
            "carrot_seed,tomato_seed",
            "--port",
            str(_REBOOT_TUNNEL_PORT),
        ],
        xdir=add_xdir,
        target=tunnel_target,
    )
    try:
        # Full completion (not a phase-marker + signal, unlike Task 8's
        # SIGKILL scenarios): rc==0 already proves add_tunnel's own post-add
        # verify passed on BOTH ends, so the daemons are confirmed up before
        # we touch anything else.
        assert add.wait(timeout=120.0) == 0, add.stderr_text()
        built = asyncio.run(observe_tunnel_processes())
        assert any(host_id == "tomato_seed" for host_id, _obs in built), (
            f"expected a tomato_seed tunnel daemon before reboot; observed: {built}"
        )
        # Manual BedHygiene bracket on tomato (module marker: the autouse
        # after-probe would race the boot). Snapshot right before the
        # reboot -- not at test start -- so the diff isolates what the
        # REBOOT itself changes, not what building the tunnel already did.
        before = run_probe("tomato", snapshot_host)
        reboot_target = make_bed_target("tomato")
        reboot_xdir = tmp_path / "reboot"
        reboot_xdir.mkdir()
        started = time.monotonic()
        rp = spawn_otto(
            ["host", "tomato_seed", "reboot", "--wait"], xdir=reboot_xdir, target=reboot_target
        )
        rc = rp.wait(timeout=600.0)
        elapsed = time.monotonic() - started
        print(f"tomato reboot --wait recovered in {elapsed:.1f}s (tunnel scenario)")  # noqa: T201 -- recorded down/up/recovery data point; captured on failure/verbose
        assert rc == 0, rp.stderr_text()
        # AFTER snapshot only now that reboot --wait confirmed tomato is back.
        after = run_probe("tomato", snapshot_host)
        leftovers = diff_snapshots(before, after)
        assert not leftovers, format_hygiene_report("tomato", leftovers)
        rm_xdir = tmp_path / "rm"
        rm_xdir.mkdir()
        rm = spawn_otto(["tunnel", "remove", "--all", "--yes"], xdir=rm_xdir, target=tunnel_target)
        assert rm.wait(timeout=120.0) == 0, rm.stderr_text()
        assert not asyncio.run(observe_tunnel_processes()), (
            "tunnel remove --all left survivors after tomato's reboot"
        )
    finally:
        rm2_xdir = tmp_path / "rm2"
        rm2_xdir.mkdir()
        spawn_otto(
            ["tunnel", "remove", "--all", "--yes"], xdir=rm2_xdir, target=tunnel_target
        ).wait(timeout=120.0)
        assert not asyncio.run(observe_tunnel_processes()), (
            "bed not clean after final reconciliation"
        )


def test_reboot_x_link_repair_idempotent(tmp_path):
    """Impair the carrot<->tomato link, reboot tomato (its qdisc clears with
    the OS), carrot's qdisc remains; `link repair --all` is idempotent against
    the half-clean state and ends with both endpoints netem-free.

    Unlike the tunnel scenario above, this one deliberately does NOT use the
    generic `snapshot_host`/`diff_snapshots` bracket on tomato: that oracle's
    qdisc comparison flags ANY change (not just new-vs-before), and a netem
    qdisc clearing on tomato is exactly the EXPECTED event under test here,
    not a leftover. Direct `tc qdisc show` checks (Task 8's
    `_assert_eth2_netem_free` pattern) say precisely what the scenario means:
    netem gone on tomato, still present on carrot, until `repair --all`.
    """
    link_id = _veggies_link_id()
    link_target = make_bed_target("carrot")
    imp_xdir = tmp_path / "impair"
    imp_xdir.mkdir()
    imp = spawn_otto(
        ["link", "impair", link_id, "--loss", "50", "--expire", _LINK_IMPAIR_EXPIRE],
        xdir=imp_xdir,
        target=link_target,
    )
    try:
        assert imp.wait(timeout=120.0) == 0, imp.stderr_text()
        _assert_eth2_netem("carrot", expected=True, what="right after impair")
        _assert_eth2_netem("tomato", expected=True, what="right after impair")
        reboot_target = make_bed_target("tomato")
        reboot_xdir = tmp_path / "reboot"
        reboot_xdir.mkdir()
        started = time.monotonic()
        rp = spawn_otto(
            ["host", "tomato_seed", "reboot", "--wait"], xdir=reboot_xdir, target=reboot_target
        )
        rc = rp.wait(timeout=600.0)
        elapsed = time.monotonic() - started
        print(f"tomato reboot --wait recovered in {elapsed:.1f}s (link scenario)")  # noqa: T201 -- recorded down/up/recovery data point; captured on failure/verbose
        assert rc == 0, rp.stderr_text()
        # tomato's qdisc cleared with the OS; carrot (a peer the reboot never
        # touched) still carries its netem -- the half-clean state.
        _assert_eth2_netem(
            "tomato", expected=False, what="right after tomato's reboot (OS-cleared)"
        )
        _assert_eth2_netem(
            "carrot", expected=True, what="right after tomato's reboot (carrot untouched)"
        )
        rep_xdir = tmp_path / "rep"
        rep_xdir.mkdir()
        rep = spawn_otto(["link", "repair", "--all"], xdir=rep_xdir, target=link_target)
        assert rep.wait(timeout=120.0) == 0, rep.stderr_text()
        _assert_eth2_netem_free("repair --all against the half-clean (tomato-already-clean) state")
    finally:
        rep2_xdir = tmp_path / "rep2"
        rep2_xdir.mkdir()
        spawn_otto(["link", "repair", "--all"], xdir=rep2_xdir, target=link_target).wait(
            timeout=120.0
        )
        _assert_eth2_netem_free("the FINAL reconciliation repair --all")
