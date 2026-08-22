"""Connection-drop chaos: blackhole the SSH port mid-command with otto's own
port-scoped netem, on the carrot->tomato eth2 data-plane hop (mgmt path is
guard-protected and must stay up). Asserts otto surfaces the drop, tears down
with no local orphan, and leaves no qdisc/timer behind on EITHER carrot or
tomato after ``otto link repair``. Self-healing ``--expire`` backstop on
every impairment; teardown repairs unconditionally, in a ``finally``, even on
assertion failure.

THE BUSYBOX GUEST ARM BELOW DOES NOT INJECT A DROP -- it pins otto's refusal
to inject one. The 2026-08-22 TAP move removed the old blocker exactly as
that day's open item predicted: a guest's ``eth0`` now has a far end that
lives on a lab host, ``carrot_seed:bbeth-1350 <-> bb1350_qemu:eth0`` IS
declared in ``tech1/lab.json``, and there is finally something for ``otto
link impair`` (whose only argument is a DECLARED LINK id -- there is no
host-and-netdev verb, by design) to name. Measurement then found the next
blocker, which is not about naming at all.

MEASURED ON THE LIVE BED, 2026-08-22, in this order:

1. The qdisc takes on the TAP, and it BITES the guest. ``--delay 300`` on
   ``carrot_seed/bbeth-1350`` moved carrot->guest ping RTT from 2.65ms to
   302.5ms. A port-scoped ``--port 23 --proto tcp --loss 100`` built the
   full prio/netem/u32 tree on the TAP, left ICMP at 0.96ms (so the wire and
   the guest were both fine, and only telnet was blackholed), and made
   ``otto host bb1350_qemu run`` fail with rc 1 after 134s -- stalled in
   ``Performing telnet login``, with the impairment verified still in place
   at the moment of failure. The ``--expire`` backstop then cleared the whole
   tree on schedule and the guest answered again immediately, no restart.
   Emulation at the far end changes nothing: a TCG guest's e1000 is a netdev
   peer like any other.

2. And otto REFUSES to place any of it through the declared link, from
   either end, in the lab the guest lives in. Both refusals are correct, and
   they are DIFFERENT refusals: carrot's ``bbeth-1350`` carries bb1350's
   management transit (``ensure_not_hop_transit``), and the guest's ``eth0``
   carries the guest's own management ip (``ensure_not_mgmt``). The property
   that made the link declarable is the same one that makes every impairment
   on it a self-lockout -- a guest with ONE NIC has no data plane distinct
   from its management path. The veggies scenario below only works because
   tomato still has a management eth1 to be reached on while its eth2 is
   blackholed; the guest has no eth1.

The numbers in (1) were obtained by naming the link from ``-l veggies``,
where the guest is not a loaded host and the hop-transit guard therefore
cannot see the dependent it protects. DO NOT BUILD AN ARM ON THAT ROUTE:
nothing can undo it afterwards. ``-l veggies link repair`` refuses the link
for naming a host outside the lab, ``-l busybox link repair`` refuses it with
the guard, and ``repair`` has no ``--from`` -- so the only teardown left is
the ``--expire`` daemon, and an impairment whose repair cannot run in a
``finally`` is not a chaos injection. That scoping asymmetry is reported, not
exploited.

An injecting guest arm therefore needs a product decision that does not exist
today: an override, or a guard that reasons about the SELECTOR rather than
the netdev (a ``--port 9000`` blackhole leaves telnet/23 untouched and locks
nobody out, yet is refused today, because the refusal is netdev-level). Until
then this module injects on veggies only. The remaining way to sever a guest
is to stop or reconfigure its QEMU process, which is a bed power operation,
not a chaos injection. (``tc`` itself IS present on the guests -- measured.)

Path A used (spec's "blackhole the SSH port"), not the SIGSTOP fallback.
Feasibility was checked live BEFORE writing the impairing test: carrot and
tomato share the ``192.168.1.0/24`` eth2 subnet directly (no VLAN/pepper
routing needed, unlike ``tests/e2e/test_link_impair_e2e.py``'s mgmt-only
bed), and a hop-routed ``otto host <target> run true`` (target = tomato
reached via ``hop=carrot_seed``, with its PRIMARY address resolved to its
eth2 ip) connected clean end to end. The declared ``carrot_seed:eth2 <->
tomato_seed:eth2`` link is exactly the netdev that session rides, so
``otto link impair <link> --port 22 --proto tcp --loss 100`` genuinely
blackholes it.

The surfacing mechanism observed live (see task-6-report.md for the timed
spike): with SSH-level keepalive configured on the hop-routed target
(``keepalive_interval``/``keepalive_count_max`` -- a LOCAL give-up decision
that does not require the dead peer to answer), asyncssh raises
``ConnectionLost: Server not responding to keepalive`` a few seconds after
the blackhole lands; it is not one of the exception types
``SessionManager.run_cmd`` translates into a graceful ``CommandResult``, so
it surfaces as an uncaught exception (rich traceback, non-zero exit) rather
than a "Command timed out" message. Either way the CLI process exits
non-zero well inside its own ``--timeout`` backstop, which is all this test
requires -- the exact exception class is an asyncssh/otto-internals detail,
not part of the asserted contract.
"""

import dataclasses
import json
import re
import time

import pytest

from otto.link.placement import parse_ip_addr
from otto.logger.mode import LogMode
from tests._fixtures.bed_hygiene import argv_pattern
from tests._fixtures.labdata import host_data
from tests._fixtures.sutrepo import make_sut_repo
from tests.e2e.chaos._bed import (
    BUSYBOX_CHAOS_HOST_ID,
    BUSYBOX_GUEST_NETDEV,
    BUSYBOX_TAP_NETDEV,
    busybox_link_id,
    busybox_probe_text,
    probe_text,
    run_probe,
    veggies_link_id,
)
from tests.e2e.chaos._seed import offset_in
from tests.integration.chaos._driver import spawn_otto
from tests.integration.chaos._target import ChaosTarget, make_bed_target

pytestmark = [
    pytest.mark.chaos,
    pytest.mark.stability,
    pytest.mark.integration,
    pytest.mark.hops,
    pytest.mark.xdist_group("chaos_lane"),
    pytest.mark.timeout(300),
]

_CMD = "sleep 120 # otto-chaos-drop"
_RUN_TIMEOUT = 120.0
_KEEPALIVE_INTERVAL = 2.0
_KEEPALIVE_COUNT_MAX = 3
_TAP_ADDR = "198.51.100.18"
"""Carrot's end of bb1350's /30. Read back off the live host below rather than
trusted: this address existing ON ``bbeth-1350`` is what makes the declared
link a description of a real wire instead of a plausible sentence."""
_IMPAIR_EXPIRE = 60


def _make_hop_target(tmp_path) -> ChaosTarget:
    """Build a tiny SUT whose ``tomato`` host is reached over eth2 via carrot.

    Mirrors ``tests/integration/chaos/_target.py::make_bed_target`` /
    ``tests/_fixtures/tunnel_bed.py::cli_sut_dir``'s shape, but this lab's
    ``tomato`` entry resolves to its DATA-PLANE address instead of its
    management ip: the real ``tech1`` lab (what ``make_bed_target``/
    ``chaos_bed`` use) gives tomato ``ip=10.10.200.12`` (mgmt, directly
    reachable, never impaired) with ``eth2`` only as a secondary
    ``interfaces`` entry -- a hop through that record would still ride mgmt.
    Here tomato's PRIMARY ``ip`` IS its eth2 address (192.168.1.12) and
    ``hop: carrot_seed`` sends the session there via
    ``RemoteHost._build_hop_transport``, which tunnels through carrot's own
    (mgmt-reachable, never-impaired) SSH connection and opens a
    ``direct-tcpip`` channel from carrot to 192.168.1.12:22 -- carrot's own
    kernel routes that over ITS eth2 interface (same ``192.168.1.0/24``
    subnet as tomato's, confirmed reachable live), which is exactly the
    netdev ``otto link impair --port 22`` scopes on both ends.

    ``keepalive_interval``/``keepalive_count_max`` are set on tomato's
    ``ssh_options`` deliberately: a bare blackholed TCP connection can sit
    silent for the OS's own multi-minute retransmission ceiling before
    either kernel gives up, but asyncssh's SSH-level keepalive is a LOCAL
    give-up decision that never needs the dead peer to answer. Confirmed
    live (task-6 spike, see task-6-report.md): with these values the client
    gives up after a handful of missed keepalive replies and raises
    ``ConnectionLost`` in ~7s -- comfortably inside the ``--timeout``
    backstop below, and the actual bound this test's "surfaces the drop"
    assertion relies on.
    """
    carrot = host_data("carrot")
    tomato = host_data("tomato")
    tech_dir = tmp_path / "hop_labdata" / "chaosdrop"
    tech_dir.mkdir(parents=True)
    (tech_dir / "lab.json").write_text(
        json.dumps(
            {
                "hosts": [
                    {
                        "ip": carrot["ip"],
                        "element": "carrot",
                        "os_type": "unix",
                        "board": "seed",
                        "valid_terms": ["ssh"],
                        "valid_transfers": ["scp", "sftp"],
                        "is_virtual": True,
                        "creds": carrot["creds"],
                        "resources": ["carrot"],
                        "labs": ["chaosdrop"],
                    },
                    {
                        "ip": tomato["interfaces"]["eth2"]["ip"],
                        "element": "tomato",
                        "os_type": "unix",
                        "board": "seed",
                        "valid_terms": ["ssh"],
                        "valid_transfers": ["scp", "sftp"],
                        "is_virtual": True,
                        "creds": tomato["creds"],
                        "resources": ["tomato"],
                        "hop": "carrot_seed",
                        "ssh_options": {
                            "keepalive_interval": _KEEPALIVE_INTERVAL,
                            "keepalive_count_max": _KEEPALIVE_COUNT_MAX,
                        },
                        "labs": ["chaosdrop"],
                    },
                ],
                "links": [],
            }
        )
    )
    sut = make_sut_repo(
        tmp_path / "hop_sut",
        name="chaosdrop_harness",
        version="0.1.0",
        extra=f"""\
[[lab.sources]]
backend = "json"
paths = ["{tech_dir}"]
""",
    )
    tomato_cred = tomato["creds"][0]
    return ChaosTarget(
        sut_dir=sut,
        lab="chaosdrop",
        host_id="tomato_seed",
        ssh_host=tomato["interfaces"]["eth2"]["ip"],
        ssh_port=22,
        ssh_username=tomato_cred["login"],
        ssh_client_key=None,
        ssh_password=tomato_cred["password"],
    )


@pytest.mark.no_hygiene_bracket  # eth2 is deliberately dirtied then repaired; brackets manually
def test_ssh_blackhole_mid_command_is_survivable_and_repairs_clean(chaos_rng, tmp_path):
    """otto blackholes tomato's SSH (port 22/tcp) on eth2 mid-command via a
    hop-routed session; asserts the run fails and ``otto link repair``
    restores an impairment-free eth2 on both endpoints.

    This scenario is inherently two-host and hop-routed, so it pins carrot
    (hop) + tomato (target) explicitly rather than using the leased
    ``chaos_bed`` element (which is arbitrary and not part of this pair),
    and brackets BedHygiene manually across both hosts instead of relying
    on the (single-host) autouse bracket.
    """
    import asyncio

    from tests._fixtures.bed_hygiene import diff_snapshots, format_hygiene_report, snapshot_host
    from tests.e2e.chaos._bed import probe_host

    link_id = veggies_link_id()
    veggies_target = make_bed_target("carrot")

    async def _snap(elem: str):
        async with probe_host(elem) as h:
            return await snapshot_host(h)

    before = {e: asyncio.run(_snap(e)) for e in ("carrot", "tomato")}
    p = None  # bound inside the try; finally must not assume it got there
    try:
        # 1) Start a long command against tomato over the carrot hop on eth2.
        run_xdir = tmp_path / "run"
        run_xdir.mkdir()
        target = _make_hop_target(tmp_path)
        p = spawn_otto(
            ["host", target.host_id, "run", _CMD, "--timeout", str(int(_RUN_TIMEOUT))],
            xdir=run_xdir,
            target=target,
        )
        # phase: session established over the hop
        p.wait_for_log(re.escape(f"| {_CMD}"), timeout=60.0)

        # the ONE deliberate sleep: seeded injection offset
        time.sleep(offset_in(chaos_rng, 2.0, 8.0))

        # 2) Blackhole SSH on eth2 with an --expire backstop (100% loss, tcp/22).
        impair_xdir = tmp_path / "impair"
        impair_xdir.mkdir()
        drop = spawn_otto(
            [
                "link",
                "impair",
                link_id,
                "--port",
                "22",
                "--proto",
                "tcp",
                "--loss",
                "100",
                "--expire",
                str(_IMPAIR_EXPIRE),
            ],
            xdir=impair_xdir,
            target=veggies_target,
        )
        assert drop.wait(timeout=60.0) == 0, drop.stderr_text()

        # 3) otto must notice the dead session and exit non-zero within a
        #    bound (channel keepalive/read timeout), never hang past the test
        #    timeout, and leave no local orphan.
        rc = p.wait(timeout=_RUN_TIMEOUT + 30.0)
        assert rc != 0, f"blackholed session should fail, not succeed\nstderr:\n{p.stderr_text()}"
        p.assert_no_process_group()
    finally:
        # Belt for the assert-failure path: `p.wait_for_log` above can raise
        # before `p` is ever signaled/reaped, leaving the local subprocess
        # running. SIGKILL it if it's still alive before doing anything else.
        if p is not None and p.proc.poll() is None:
            p.signal(9)
        # 4) Repair unconditionally, both directions, and verify clean eth2 --
        #    even on assertion failure above.
        repair_xdir = tmp_path / "repair"
        repair_xdir.mkdir()
        rep = spawn_otto(["link", "repair", link_id], xdir=repair_xdir, target=veggies_target)
        assert rep.wait(timeout=60.0) == 0, rep.stderr_text()

        # Best-effort: the abandoned foreground `sleep 120` on tomato is not
        # otto-tagged (BedHygiene's diff below can't see it) and would
        # otherwise only die on its own 120s ceiling or the connection's
        # eventual TCP-level recovery. Clear it explicitly; this probe rides
        # the mgmt path (eth1), never impaired, so it works regardless of
        # eth2's state.
        #
        # otto's `run` verb types commands into a persistent PTY shell, which
        # strips everything after a `#` before exec'ing -- so the real
        # remote argv is exactly "sleep 120", never "otto-chaos-drop"; target
        # that instead of the (invisible) trailing comment.
        run_probe(
            "tomato",
            lambda h: h.exec(
                f"pkill -f '{argv_pattern('sleep 120')}' || true", timeout=15, log=LogMode.QUIET
            ),
        )

        after = {e: asyncio.run(_snap(e)) for e in ("carrot", "tomato")}
        leftovers = []
        for e in ("carrot", "tomato"):
            leftovers += [f"{e}: {x}" for x in diff_snapshots(before[e], after[e])]
        assert not leftovers, format_hygiene_report("carrot+tomato", leftovers)


@dataclasses.dataclass(frozen=True)
class _LinkAttempt:
    """One completed ``otto link`` invocation, split the way the CLI writes it."""

    rc: int
    out: str
    """STDOUT, whitespace-collapsed to one line.

    Collapsed, not raw: ``otto.cli.invoke.fail`` renders refusals through
    ``rich.print``, which hard-wraps at the non-tty console width, so ``it
    carries the management path to`` arrives split across two lines. Matching
    raw text would pass or fail on where the wrap happened to land.

    Measured, because it is not what the stream names suggest: the DEBUG
    transcript ``spawn_otto`` forces shares THIS stream with the refusal
    (rich's console is stdout for both), and stderr carries almost nothing.
    So callers must match a distinctive sentence of otto's ANSWER here --
    never merely assert the string is non-empty, which the log alone
    satisfies."""

    diag: str
    """``rc`` plus both streams, for assertion messages only."""


def _link_attempt(xdir, target, argv: list) -> _LinkAttempt:
    """Run one ``otto link`` command to completion and report how it went."""
    xdir.mkdir()
    p = spawn_otto(argv, xdir=xdir, target=target)
    rc = p.wait(timeout=120.0)
    out = " ".join(p.stdout_text().split())
    return _LinkAttempt(
        rc=rc, out=out, diag=f"rc={rc}\n--- stdout ---\n{out}\n--- stderr ---\n{p.stderr_text()}"
    )


def _assert_tap_unimpaired(qdisc: str, why: str) -> None:
    """Assert carrot's TAP carries no otto-placed qdisc.

    Both spellings are checked, because otto places two different trees: a
    whole-link impairment is a root ``netem``, and a port-scoped one is a root
    ``prio`` whose netem lives in a band. Looking for only ``netem`` would
    still catch the scoped tree's leaf, but looking for only ``prio`` would
    miss the whole-link shape entirely — and a clean TAP is ``fq_codel``, so
    neither word is background noise.
    """
    assert "netem" not in qdisc, f"{why}: netem on carrot/{BUSYBOX_TAP_NETDEV}: {qdisc!r}"
    assert "prio" not in qdisc, f"{why}: prio root on carrot/{BUSYBOX_TAP_NETDEV}: {qdisc!r}"


@pytest.mark.no_hygiene_bracket  # the guest is not the veggies host the autouse bracket leases
def test_otto_refuses_to_blackhole_the_busybox_guests_only_wire(busybox_chaos_bed, tmp_path):
    """The guest arm: ``otto link impair`` refuses the guest's declared link
    from BOTH ends, with the two different self-lockout refusals, and places
    nothing on the TAP while doing it.

    See the module docstring for the measurement this replaced an open item
    with. The short version: the impairment mechanism works fine on a TAP
    whose peer is a TCG-emulated guest, and otto declines to use it here
    because the guest's only NIC is also its management path.

    Three attempts, and the FIRST is the load-bearing one -- which is why it
    runs first. A bare ``impair`` resolves BOTH placements and refuses on the
    first that objects,
    so it is refused twice over -- neutering either guard alone still leaves
    the other to fail the command, and ``rc != 0`` there proves only that
    SOMETHING objected. Measured: with ``ensure_not_hop_transit`` neutered the
    bare command still exits 1, on the guest end's management refusal. So each
    attempt asserts the wording of the refusal it expects, and the ``--from``
    attempts isolate one placement each, where ``rc != 0`` has exactly one
    possible author:

    * ``--from carrot_seed`` can only land on ``bbeth-1350``, which is NOT
      carrot's management interface (asserted below off carrot's live address
      table, so the claim is measured rather than remembered). Nothing but
      ``ensure_not_hop_transit`` can stop it -- it is the TAP's /30 carrying
      the guest's management transit -- and with that guard neutered this
      attempt really does blackhole the guest, which is what makes the
      assertion falsifiable rather than decorative;
    * ``--from bb1350_qemu`` lands on the guest's ``eth0``, which IS its
      management interface, so ``ensure_not_mgmt`` refuses. Getting that
      answer at all means otto reached the guest over telnet-through-the-hop
      and parsed BusyBox ``ip -o addr show`` with
      ``otto.link.placement.parse_ip_addr`` -- a POSITIVE address match is
      required to refuse, so the refusal is itself the evidence that the
      applet's output is readable.

    ``--expire 60`` is passed to commands that must never apply anything, on
    purpose: it is the backstop for exactly the failure this test exists to
    catch. If a guard regression lets the placement through, the assertions
    below go red with a live impairment on the bed's only path to a guest,
    and the ``finally`` repair is refused by the OTHER guard (see the module
    docstring on why ``repair`` cannot undo this link) -- so the expiry
    daemon is the teardown that always runs. Confirmed by running exactly
    that mutation: the impairment landed, the arm went red, and the daemon
    cleared the TAP on schedule.
    """
    link_id = busybox_link_id()  # resolved by otto's own loader, not spelled here
    target = busybox_chaos_bed.target
    carrot_mgmt = host_data("carrot")["ip"]
    scope = ["--port", "23", "--proto", "tcp", "--loss", "100", "--expire", "60"]

    def tap_qdisc() -> str:
        return probe_text("carrot", f"tc qdisc show dev {BUSYBOX_TAP_NETDEV}")

    # 0) The wire is real, and the TAP is not carrot's management interface.
    #    Both are read off the live host: they are the premises that decide
    #    which refusal each end owes, and a stale premise would let a wrong
    #    refusal read as the right one.
    addrs = parse_ip_addr(probe_text("carrot", "ip -o addr show"))
    assert _TAP_ADDR in {str(a.ip) for a in addrs.get(BUSYBOX_TAP_NETDEV, [])}, (
        f"carrot has no {_TAP_ADDR} on {BUSYBOX_TAP_NETDEV} -- the declared link "
        f"describes a wire the bed does not have: {sorted(addrs)}"
    )
    mgmt_netdevs = {n for n, ifs in addrs.items() if any(str(a.ip) == carrot_mgmt for a in ifs)}
    assert mgmt_netdevs, (
        f"carrot's management ip {carrot_mgmt} is on none of its netdevs {sorted(addrs)} -- "
        f"the management guard cannot match anywhere, so 'it did not fire here' below "
        f"would be vacuous"
    )
    assert BUSYBOX_TAP_NETDEV not in mgmt_netdevs, (
        f"carrot's management ip {carrot_mgmt} sits on {sorted(mgmt_netdevs)} -- the "
        f"self-lockout guard this arm attributes to hop transit would be the "
        f"MANAGEMENT guard instead, and the assertions below would be lying"
    )
    _assert_tap_unimpaired(
        tap_qdisc(),
        "a stranded qdisc from an earlier run was already here BEFORE this test ran",
    )

    try:
        # 1) THE ISOLATING ATTEMPT, and it goes first for that reason: carrot's
        #    TAP alone. One placement, on a netdev the management guard
        #    provably cannot match, so a non-zero exit here has exactly one
        #    author -- and a guard regression really does blackhole the guest
        #    right here, rather than being caught by the other end's refusal in
        #    the both-directions attempt below.
        tap = _link_attempt(
            tmp_path / "tap",
            target,
            ["link", "impair", link_id, "--from", "carrot_seed", *scope],
        )
        assert tap.rc != 0, (
            f"otto BLACKHOLED the bed's only path to {BUSYBOX_CHAOS_HOST_ID} -- the "
            f"hop-transit guard is the only thing standing between this command and "
            f"a live impairment on carrot/{BUSYBOX_TAP_NETDEV}:\n{tap.diag}"
        )
        assert f"refusing to impair '{BUSYBOX_TAP_NETDEV}' on 'carrot_seed'" in tap.out, tap.diag
        assert "hop transit; self-lockout" in tap.out, tap.diag

        # 2) Both directions -- what a user actually types. Refused twice over
        #    (see the docstring); this pins WHICH refusal speaks first.
        both = _link_attempt(tmp_path / "both", target, ["link", "impair", link_id, *scope])
        assert both.rc != 0, (
            f"otto IMPAIRED the guest's only wire instead of refusing:\n{both.diag}"
        )
        assert f"refusing to impair '{BUSYBOX_TAP_NETDEV}' on 'carrot_seed'" in both.out, both.diag
        assert f"it carries the management path to '{BUSYBOX_CHAOS_HOST_ID}'" in both.out, both.diag
        assert "hop transit; self-lockout" in both.out, both.diag

        # 3) The guest end, reached over telnet through the hop: a different
        #    guard, a different message, same refusal to lock otto out.
        guest = _link_attempt(
            tmp_path / "guest",
            target,
            ["link", "impair", link_id, "--from", BUSYBOX_CHAOS_HOST_ID, *scope],
        )
        assert guest.rc != 0, (
            f"otto IMPAIRED the guest's own NIC instead of refusing:\n{guest.diag}"
        )
        assert (
            f"refusing to impair '{BUSYBOX_GUEST_NETDEV}' on '{BUSYBOX_CHAOS_HOST_ID}'" in guest.out
        ), guest.diag
        assert "it is the management interface otto reaches the host through" in guest.out, (
            guest.diag
        )
    finally:
        # Best-effort, and expected to be REFUSED while the guards hold: it is
        # here for the mutation case where they do not (see the docstring).
        _link_attempt(tmp_path / "repair", target, ["link", "repair", link_id])
        _assert_tap_unimpaired(
            tap_qdisc(),
            "a REFUSED impairment still placed state -- the refusal must happen "
            "before any host is mutated",
        )
        # The guest is the thing the guards protect; prove it still answers.
        assert "GUEST-REACHABLE" in busybox_probe_text("echo GUEST-REACHABLE"), (
            f"{busybox_chaos_bed.element}: unreachable after three impairments "
            f"otto said it refused to place"
        )
