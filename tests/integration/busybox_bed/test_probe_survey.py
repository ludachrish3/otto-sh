"""The inventory tier on the five live BusyBox guests, one per pinned version.

These guests are a single BusyBox binary behind ``test1``; their ``netstat``
is the BusyBox applet, their cred is root, and their ssh is dead by
construction on four of them, and a 2012 dropbear on the fifth. That gives
the survey three live facts to prove: the netstat arm parses on every
version, the owner column is read (root, so it is populated), and, on the
four with no daemon, the permanently dead, undeclared ssh is a drift
true-negative rather than a false supported.
"""

import pytest

from otto.host.survey import run_survey
from otto.host.survey.engine import dial_for
from otto.host.survey.inventory import INVENTORY_SCRIPT, parse_inventory
from otto.host.survey.verdict import DIAL_TIMEOUT_S
from scripts.build_busybox_guest_images import GUEST_TABLE

pytestmark = pytest.mark.timeout(120)

HOP_ID = "test1"
"""The hop ``hop_on_this_loop`` (tests/integration/conftest.py) rebuilds per row."""

_TELNETD_OWNERS = ("telnetd", "busybox", "udpsvd", "tcpsvd")
_SSH_PORT = 22
"""Dead on four guests, live (a real dropbear) on the fifth — see SSHD_GUESTS."""
SSHD_GUESTS = {g.element for g in GUEST_TABLE if g.sshd == "dropbear"}


@pytest.mark.asyncio
async def test_the_netstat_arm_parses_on_every_guest_version(guest):
    host, version = guest
    out = (await host.run(INVENTORY_SCRIPT)).only
    inv = parse_inventory(str(out.value), elevated=True)
    assert inv.tool == "netstat", (version, str(out.value)[:200])
    assert inv.error == ""
    assert inv.unparsed == 0, (version, str(out.value))
    telnet = [
        lis
        for lis in inv.listeners
        if lis.transport == "tcp" and lis.port == host.telnet_options.port
    ]
    assert telnet, (version, inv.listeners)
    assert telnet[0].owner in _TELNETD_OWNERS, (version, telnet[0])


@pytest.mark.asyncio
async def test_the_survey_reports_telnet_supported_and_ssh_as_the_drift_true_negative_except_bb1350(
    hop_on_this_loop, guest
):
    """The guest's cred is root, so the inventory runs elevated: no owners-incomplete notice.

    On four of the five guests, ssh and ftp are dead by construction and the
    guest's menu names neither, so they are the drift true-negatives. Both
    are declared candidates, so each is dialed from the guest's own vantage
    — the ``test1`` hop — before any login is tried; the dial is refused and
    no login is attempted.

    The row that survives on those four is the INVENTORY's, not the dial's:
    both say ``closed``, and the inventory tier outranks the dial tier. That
    is the assertion, and it is the whole point — the tier is the evidence
    that no login ran. A login-tier row would outrank both, so the only way
    this row can read ``inventory`` is if the pre-dial stopped the login.
    Before it did, this port read ``login-failed`` from a hop-forwarded
    connect whose reset the classifier took for a refused password: an
    ANSWER, which drifts and pins, on a port with no service at all.

    bb1350 declares ssh and answers it, so its ssh row is the mirror image —
    an open dial, a real login, and a ``login``-tier ``supported`` that
    outranks the inventory row. ftp keeps the inventory-tier reasoning above
    on bb1350 too; only ssh flips.
    """
    host, version = guest
    s = await run_survey(host)
    port = host.telnet_options.port
    telnet = next(v for v in s.verdicts if v.protocol == "telnet" and v.port == port)
    assert telnet.state == "supported", (version, telnet)
    assert telnet.tier == "login", (version, telnet)
    shell = next(v for v in s.verdicts if v.protocol == "shell")
    assert shell.state == "supported", (version, shell)
    dead = ["ftp"] if host.element.name in SSHD_GUESTS else ["ftp", "ssh"]
    for protocol in dead:
        row = next(v for v in s.verdicts if v.protocol == protocol)
        assert (row.state, row.tier) == ("closed", "inventory"), (version, row)
        assert row.vantage == "session:telnet", (version, row)
        assert row.detail.startswith(f"nothing bound on :{row.port}"), (version, row)
        assert protocol not in s.supported, (version, s.supported)
    if host.element.name in SSHD_GUESTS:
        # The one guest with a real 2012 dropbear: declared, dialed from the
        # hop, logged into with the guest's own root cred — supported at the
        # login tier, the opposite verdict from its four siblings.
        ssh = next(v for v in s.verdicts if v.protocol == "ssh")
        assert (ssh.state, ssh.tier) == ("supported", "login"), (version, ssh)
        assert ssh.port == _SSH_PORT, (version, ssh)
        assert "ssh" in s.supported, (version, s.supported)
    assert not any(f.startswith("owners incomplete") for f in s.footnotes), (version, s.footnotes)
    assert s.working_ports == {}
    # The pre-dial above is invisible in the merged rows, so assert it here on
    # the engine's own binding. This is its only bed coverage, and the hop-side
    # command chain it selects — the BusyBox-aware one this bed exists to
    # exercise — could regress to not-checkable on every call with every row
    # above still green.
    dial = await dial_for(host, timeout=DIAL_TIMEOUT_S)(_SSH_PORT)
    if host.element.name in SSHD_GUESTS:
        assert dial.state == "open", (version, dial)
        assert "dropbear" in (dial.banner or "").lower(), (version, dial)
    else:
        assert dial.state == "closed", (version, dial)
        assert "refused" in dial.detail.lower(), (version, dial)
    # And the OPEN half of the same binding, which nothing on this branch
    # proved: a hop-side dial reading a banner off a service that holds the
    # connection. BusyBox telnetd sends IAC negotiation and then waits for
    # the client, so the bytes are there from millisecond one and only the
    # script's own structure decides whether they are ever seen. Before the
    # reader was given its own timeout this came back
    # `open, service=None, "open, no banner within 2s"` on every hopped host,
    # which is what killed discovery and the swept-console promotion.
    live = await dial_for(host, timeout=DIAL_TIMEOUT_S)(port)
    assert live.state == "open", (version, live)
    assert live.service == "telnet", (version, live)
    assert live.banner, (version, live)
