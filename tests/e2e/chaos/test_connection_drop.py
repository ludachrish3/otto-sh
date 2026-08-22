"""Connection-drop chaos: blackhole the SSH port mid-command with otto's own
port-scoped netem, on the carrot->tomato eth2 data-plane hop (mgmt path is
guard-protected and must stay up). Asserts otto surfaces the drop, tears down
with no local orphan, and leaves no qdisc/timer behind on EITHER carrot or
tomato after ``otto link repair``. Self-healing ``--expire`` backstop on
every impairment; teardown repairs unconditionally, in a ``finally``, even on
assertion failure.

NO BUSYBOX GUEST ARM, and the reason is this module's injection mechanism
rather than its transport. Every impairment here is placed by ``otto link
impair``, whose only argument is a DECLARED LINK id (``src/otto/cli/link.py``
-- there is no host-and-netdev verb, by design), and a link joins two lab
hosts. A bed guest has exactly one netdev, ``eth0``, and its far end is
QEMU's in-process user-mode NAT at 10.0.2.2, which is not a lab host and
never can be: there is no second endpoint to declare, so there is no link to
name, so there is no impairment to place. The hop side is no better -- the
telnet path arrives through a hostfwd bound on carrot's own ``127.0.0.1``,
and netem on a leased host's loopback would blackhole all five guests plus
every other loopback service on it at once. (``tc`` itself IS present on the
guests -- measured. The missing piece is a link, not a tool.) The remaining
way to sever a guest would be to stop or reconfigure its QEMU process, which
is a bed power operation, not a chaos injection.

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

import json
import re
import time

import pytest

from otto.logger.mode import LogMode
from tests._fixtures.bed_hygiene import argv_pattern
from tests._fixtures.labdata import host_data
from tests._fixtures.sutrepo import make_sut_repo
from tests.e2e.chaos._bed import run_probe, veggies_link_id
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
